"""公共REST统一入口：共享缓存、并发去重、预算、冷却和可追溯响应。"""

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DeferredRequest(Exception):
    def __init__(self, retry_at, reason):
        self.retry_at = retry_at
        self.reason = reason
        super().__init__(f"{reason}；最早重试时间UTC毫秒：{retry_at}")


class RejectedRequest(Exception):
    pass


@dataclass(frozen=True)
class Endpoint:
    host: str
    path: str
    params: frozenset
    required: frozenset = frozenset()
    ttl_ms: int = 5000
    # 本地工程预算记账单位，非声称交易所官方权重。
    cost: int = 10


def endpoint(host, path, params="", required="", ttl=5000):
    return Endpoint(host, path, frozenset(params.split()), frozenset(required.split()), ttl)


ENDPOINTS = {
    "binance.instruments": endpoint("fapi.binance.com", "/fapi/v1/exchangeInfo", ttl=3600000),
    "binance.candles": endpoint("fapi.binance.com", "/fapi/v1/klines",
                                "symbol interval startTime endTime limit", "symbol interval"),
    "binance.oi": endpoint("fapi.binance.com", "/fapi/v1/openInterest", "symbol", "symbol", 15000),
    "binance.funding_history": endpoint("fapi.binance.com", "/fapi/v1/fundingRate",
                                        "symbol startTime endTime limit", "symbol", 60000),
    "binance.mark": endpoint("fapi.binance.com", "/fapi/v1/premiumIndex", "symbol", "symbol"),
    "okx.instruments": endpoint("www.okx.com", "/api/v5/public/instruments",
                                "instType instId", "instType", 3600000),
    "okx.candles": endpoint("www.okx.com", "/api/v5/market/history-candles",
                            "instId bar after before limit", "instId bar"),
    "okx.oi": endpoint("www.okx.com", "/api/v5/public/open-interest",
                       "instType instId", "instType instId", 15000),
    "okx.funding": endpoint("www.okx.com", "/api/v5/public/funding-rate", "instId", "instId", 60000),
    "okx.funding_history": endpoint("www.okx.com", "/api/v5/public/funding-rate-history",
                                   "instId before after limit", "instId", 60000),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_transport(url, timeout):
    """不接收认证头，不自动改域名或跟随重定向。"""
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "MarketDataHub/0.1"})
    try:
        response = build_opener(NoRedirect).open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise RejectedRequest("响应超过16MB，本次不接收")
        return response.code, dict(response.headers), raw


class PublicGateway:
    def __init__(self, store, transport=public_transport, *, budget_units=60,
                 min_interval_ms=1000):
        # 可配置的本地工程预算，默认保留原6次/分钟；不是交易所官方权重。
        # 扩容调用方须依据端点限制设置，共享请求记录、冷却及封禁仍然生效。
        if (isinstance(budget_units, bool) or not isinstance(budget_units, int)
                or budget_units <= 0 or isinstance(min_interval_ms, bool)
                or not isinstance(min_interval_ms, int) or min_interval_ms < 0):
            raise ValueError("本地请求预算须为正整数，最小间隔须为非负整数")
        self.store = store
        self.transport = transport
        self.budget_units = budget_units
        self.min_interval_ms = min_interval_ms

    def _request(self, name, params):
        if name not in ENDPOINTS:
            raise RejectedRequest("接口未登记；本入口只支持公共只读行情")
        spec = ENDPOINTS[name]
        if set(params) - spec.params or spec.required - set(params):
            raise RejectedRequest("参数未登记或缺少必需参数，不接受密钥、签名或任意URL")
        if any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in params.values()):
            raise RejectedRequest("参数只接受字符串或整数")
        canonical = {k: str(v) for k, v in sorted(params.items())}
        if any(not v or len(v) > 128 for v in canonical.values()):
            raise RejectedRequest("参数为空或过长")
        if name == "binance.candles":
            from .store import INTERVAL_MS
            if canonical["interval"] not in INTERVAL_MS:
                raise RejectedRequest("未配置的原生周期")
        if name == "okx.candles" and canonical["bar"] not in {
                "1m", "5m", "15m", "30m", "1H", "4H", "12Hutc", "1Dutc"}:
            raise RejectedRequest("只读取已配置UTC原生周期，不合成8小时")
        numeric = {"startTime", "endTime", "after", "before", "limit"}
        for k in set(canonical) & numeric:
            if not canonical[k].isdigit():
                raise RejectedRequest("时间与数量参数须为非负整数")
            canonical[k] = str(int(canonical[k]))
        if "limit" in canonical:
            upper = 1500 if name == "binance.candles" else 1000 if name.startswith("binance.") else 300
            if not 1 <= int(canonical["limit"]) <= upper:
                raise RejectedRequest("分页数量超出本接口配置范围")
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        request_key = hashlib.sha256((name + encoded).encode()).hexdigest()
        return spec, canonical, encoded, request_key

    def get(self, name, params=None):
        spec, params, encoded, key = self._request(name, params or {})
        now = self.store.clock()
        owner = uuid.uuid4().hex
        # SQLite事务同时协调同库多实例；过期租约释放崩溃进程的占用。
        with self.store.transaction() as con:
            cached = con.execute("SELECT c.response_json,c.finished_at,c.id FROM api_cache a "
                                 "JOIN api_calls c ON a.call_id=c.id WHERE a.request_key=? AND a.expires_at>?",
                                 (key, now)).fetchone()
            if cached:
                return {"数据": json.loads(cached["response_json"], parse_float=str), "来源": "共享缓存",
                        "请求编号": cached["id"], "获取时间UTC毫秒": cached["finished_at"]}
            con.execute("INSERT OR IGNORE INTO api_scope_state(scope) VALUES (?)", (spec.host,))
            state = con.execute("SELECT * FROM api_scope_state WHERE scope=?", (spec.host,)).fetchone()
            if state["blocked"]:
                raise RejectedRequest("该来源已暂停：" + str(state["reason"]))
            if state["cooldown_until"] > now:
                raise DeferredRequest(state["cooldown_until"], "该来源处于共享冷却期")
            con.execute("DELETE FROM api_leases WHERE until_at<=?", (now,))
            lease = con.execute("SELECT until_at FROM api_leases WHERE request_key=?", (key,)).fetchone()
            if lease:
                raise DeferredRequest(lease[0], "相同请求正在处理中，稍后读取共享缓存")
            calls = con.execute("SELECT started_at FROM api_calls WHERE scope=? AND started_at>? "
                                "ORDER BY started_at", (spec.host, now - 60000)).fetchall()
            # 同一来源的所有网关实例共用请求记录；默认预算6次/分钟、间隔1秒。
            if len(calls) * spec.cost >= self.budget_units:
                raise DeferredRequest(calls[0][0] + 60001, "本地请求预算已用尽")
            if calls and calls[-1][0] + self.min_interval_ms > now:
                raise DeferredRequest(calls[-1][0] + self.min_interval_ms, "请求间隔保护")
            con.execute("INSERT INTO api_leases VALUES (?,?,?)", (key, owner, now + 30000))
            cursor = con.execute("INSERT INTO api_calls(request_key,endpoint,scope,params_json,started_at,status)"
                                 " VALUES (?,?,?,?,?,?)", (key, name, spec.host, encoded, now, "处理中"))
            call_id = cursor.lastrowid
        url = "https://" + spec.host + spec.path
        if params:
            url += "?" + urlencode(params)
        try:
            status, headers, raw = self.transport(url, 10)
            lower_headers = {k.lower(): v for k, v in headers.items()}
            saved_headers = {k: v for k, v in lower_headers.items()
                             if k in {"date", "content-type", "content-length", "retry-after"}
                             or k.startswith("x-mbx-used-weight")}
            try:
                body = json.loads(raw, parse_float=str)
            except (ValueError, UnicodeError):
                body = None
            business_code = str(body.get("code")) if isinstance(body, dict) and "code" in body else None
            reason = None
            cooldown = 0
            blocked = False
            if status in (401, 403, 418) or business_code in ("50007", "50012"):
                reason, blocked = "访问或认证被拒绝，需要人工核查后明确恢复", True
            elif status == 429 or business_code in ("50011", "50040"):
                reason, cooldown = "交易所限频，暂停此来源", self._retry_at(lower_headers, self.store.clock())
            elif status >= 500 or business_code == "50013":
                reason, cooldown = "上游暂时不可用", self.store.clock() + 5000
            # 成功业务码白名单：`None` = 币安 fapi 成功响应**不带** code 字段；
            # `"0"` = 欧易；`"000000"` = 币安 **bapi**（web 端）成功码（如公开的
            # `www.binance.bh/bapi/futures/v1/public/future/smart-money/signal/*`）。
            # 仅**追加**一个成功码，不放宽任何失败分支；已登记端点无返回值等于 "000000"，
            # 故对既有端点**零行为变化**。（聪明钱接入的唯一必要共享入口改动）
            elif not 200 <= status < 300 or body is None or business_code not in (None, "0", "000000"):
                reason = "HTTP或业务响应未成功，不写入有效缓存"
            elif name.startswith("okx.") and (not isinstance(body, dict) or business_code != "0"
                                             or not isinstance(body.get("data"), list)):
                reason = "欧易响应缺少有效业务码或data数组"
            with self.store.transaction() as con:
                owned = con.execute("SELECT owner FROM api_leases WHERE request_key=?", (key,)).fetchone()
                if not owned or owned[0] != owner:
                    raise RejectedRequest("请求租约失效，不覆盖后续请求")
                finished = self.store.clock()
                con.execute("UPDATE api_calls SET finished_at=?,status=?,http_status=?,business_code=?,"
                            "response_json=?,response_sha256=?,response_headers_json=?,reason=? WHERE id=?",
                            (finished, "成功" if reason is None else "失败", status, business_code,
                             raw.decode("utf-8", errors="replace"), hashlib.sha256(raw).hexdigest(),
                             json.dumps(saved_headers, ensure_ascii=False), reason, call_id))
                con.execute("DELETE FROM api_leases WHERE request_key=? AND owner=?", (key, owner))
                if cooldown or blocked:
                    con.execute("UPDATE api_scope_state SET cooldown_until=MAX(cooldown_until,?),"
                                "blocked=MAX(blocked,?),reason=? WHERE scope=?",
                                (cooldown, int(blocked), reason, spec.host))
                if reason is None:
                    con.execute("INSERT INTO api_cache VALUES (?,?,?) ON CONFLICT(request_key) DO UPDATE "
                                "SET call_id=excluded.call_id,expires_at=excluded.expires_at",
                                (key, call_id, finished + spec.ttl_ms))
            if reason:
                if cooldown:
                    raise DeferredRequest(cooldown, reason)
                raise RejectedRequest(reason)
            return {"数据": body, "来源": "公共REST", "请求编号": call_id, "获取时间UTC毫秒": finished}
        except (DeferredRequest, RejectedRequest) as exc:
            self._finish_aborted(key, owner, call_id)
            raise exc
        except Exception as exc:
            self._finish_aborted(key, owner, call_id)
            # 不把第三方异常全文或未知请求头写进用户日志。
            raise RejectedRequest("网络请求未完成，错误类型：" + type(exc).__name__) from None

    def _finish_aborted(self, key, owner, call_id):
        with self.store.transaction() as con:
            con.execute("DELETE FROM api_leases WHERE request_key=? AND owner=?", (key, owner))
            con.execute("UPDATE api_calls SET status='失败',finished_at=?,reason='请求中断，未缓存' "
                        "WHERE id=? AND status='处理中'", (self.store.clock(), call_id))

    @staticmethod
    def _retry_at(headers, now):
        value = headers.get("retry-after", "")
        try:
            seconds = float(value)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError()
            return max(now + 1000, now + int(seconds * 1000))
        except (ValueError, TypeError):
            try:
                return max(now + 1000, int(parsedate_to_datetime(value).timestamp() * 1000))
            except (ValueError, TypeError, OverflowError):
                return now + 60000
