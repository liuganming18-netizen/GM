#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号与持仓接通（2026-10-13）· **实盘只读账户读取**（需 ccxt ⇒ 用 freqtrade 解释器运行）。

职责：读取交易所账户的**余额/权益/持仓**，写入 `live/live_signals.sqlite3:live_account_snapshot`，
由只读 API 提供给前端刷新。

**硬约束（只读）**：
  · 只调用 ccxt 的**只读**方法（`fetch_balance` / `fetch_positions` / `load_markets`）；
  · 运行期守卫：任何 `create_order`/`cancel_order`/`set_leverage`/`transfer` 等写方法被调用即抛错；
  · 不输出、不落盘、不记录 Key/Secret/Passphrase（`redact()` 只输出「已配置/未配置」与来源名）；
  · 不改交易所 API 权限、不提交任何真实订单。

凭据来源（**只读**，按优先级）：
  1) 环境变量：`BINANCE_API_KEY`/`BINANCE_API_SECRET`、`OKX_API_KEY`/`OKX_API_SECRET`/`OKX_PASSPHRASE`；
  2) 本机文件：`后端实现/live/credentials.json`（形如 `{"binance":{"key":...,"secret":...}}`）。

状态枚举（与 `live_store.ACC_STATUSES` 一致）：
  connected / connected_no_positions / not_configured / auth_failed / stale /
  reader_unavailable / error
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import live_store as L

LIVE_DIR = L.LIVE_DIR
CRED_FILE = Path(os.environ.get("LIVE_CRED_FILE", str(LIVE_DIR / "credentials.json")))
CRED_ENV = {
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET", None),
    "okx": ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"),
}
# 交易所默认参数（只读；`options.defaultType` 仅影响读哪个账户类型，不改变权限）
EXCHANGE_OPTS = {
    "binance": {"options": {"defaultType": "future"}},
    "okx": {"options": {"defaultType": "swap"}},
}

_WRITE_METHODS = (
    "create_order", "create_orders", "cancel_order", "cancel_orders", "cancel_all_orders",
    "edit_order", "set_leverage", "set_margin_mode", "set_position_mode", "transfer",
    "withdraw", "create_market_buy_order", "create_market_sell_order",
    "create_limit_buy_order", "create_limit_sell_order", "add_margin", "reduce_margin",
)


class RealOrderForbidden(RuntimeError):
    pass


def install_readonly_guard(ex) -> None:
    """把写方法替换为抛错桩（**防御性**：本模块本就不调用写方法）。"""
    def _boom(name):
        def _f(*_a, **_k):
            raise RealOrderForbidden(f"实盘只读读取器禁止调用写方法：{name}")
        return _f
    for m in _WRITE_METHODS:
        if hasattr(ex, m):
            try:
                setattr(ex, m, _boom(m))
            except Exception:                             # noqa: BLE001
                pass


def _load_credentials() -> dict:
    creds = {}
    if CRED_FILE.exists():
        try:
            data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
            for ex, v in (data or {}).items():
                if isinstance(v, dict) and v.get("key") and v.get("secret"):
                    creds[ex.lower()] = {"key": v["key"], "secret": v["secret"],
                                         "password": v.get("password") or v.get("passphrase"),
                                         "source": "file"}
        except Exception:                                 # noqa: BLE001
            pass
    for ex, (ke, se, pe) in CRED_ENV.items():
        k, s = os.environ.get(ke), os.environ.get(se)
        p = os.environ.get(pe) if pe else None
        if k and s and (pe is None or p):
            creds.setdefault(ex, {"key": k, "secret": s, "password": p, "source": "env"})
    return creds


def credential_report() -> dict:
    """**不输出任何密钥内容**：只报「哪个交易所、缺哪一类配置」。"""
    creds = _load_credentials()
    out = {}
    for ex, (ke, se, pe) in CRED_ENV.items():
        c = creds.get(ex)
        missing = []
        if not (c and c.get("key")):
            missing.append(ke)
        if not (c and c.get("secret")):
            missing.append(se)
        if pe and not (c and c.get("password")):
            missing.append(pe)
        out[ex] = {"configured": not missing, "missing": missing,
                   "source": (c or {}).get("source")}
    return out


def _proxies():
    px = {}
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(k):
            px["https" if "S" in k or "s" in k[:5] else "http"] = os.environ[k]
    return px or None


def _import_ccxt():
    try:
        import ccxt                                    # noqa: PLC0415
        return ccxt
    except Exception:                                  # noqa: BLE001
        return None


def _positions_of(ex, raw) -> list:
    out = []
    for p in raw or []:
        try:
            contracts = p.get("contracts")
            if contracts in (None, 0, 0.0):
                continue
            out.append({
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "contracts": contracts,
                "contractSize": p.get("contractSize"),
                "notional": p.get("notional"),
                "entryPrice": p.get("entryPrice"),
                "markPrice": p.get("markPrice"),
                "unrealizedPnl": p.get("unrealizedPnl"),
                "liquidationPrice": p.get("liquidationPrice"),
                "leverage": p.get("leverage"),
                "marginMode": p.get("marginMode"),
            })
        except Exception:                              # noqa: BLE001
            continue
    return out


def read_exchange(ex: str, cred: dict) -> dict:
    ccxt = _import_ccxt()
    if ccxt is None:
        return {"exchange": ex, "account_label": "default", "status": "reader_unavailable",
                "detail": "本机未安装 ccxt（只读读取器不可用）；请用 freqtrade 环境运行",
                "error_class": "ModuleNotFoundError", "positions": []}
    try:
        klass = getattr(ccxt, ex)
    except AttributeError:
        return {"exchange": ex, "account_label": "default", "status": "error",
                "detail": f"ccxt 不支持交易所 {ex}", "error_class": "UnsupportedExchange",
                "positions": []}
    params = {"apiKey": cred["key"], "secret": cred["secret"], "enableRateLimit": True,
              "timeout": 20000}
    if cred.get("password"):
        params["password"] = cred["password"]
    params.update(EXCHANGE_OPTS.get(ex, {}))
    px = _proxies()
    if px:
        params["proxies"] = px
    ex_obj = klass(params)
    install_readonly_guard(ex_obj)
    read_at = L.now_ms()
    try:
        ex_obj.load_markets()
        bal = ex_obj.fetch_balance()
        try:
            raw_pos = ex_obj.fetch_positions()
        except Exception:                              # noqa: BLE001
            raw_pos = []
        total = bal.get("total") or {}
        free = bal.get("free") or {}
        used = bal.get("used") or {}
        quote = "USDT"
        equity = total.get(quote)
        avail = free.get(quote)
        usedm = used.get(quote)
        positions = _positions_of(ex_obj, raw_pos)
        status = "connected" if positions else "connected_no_positions"
        return {"exchange": ex, "account_label": "default", "status": status,
                "detail": ("已连接；当前有持仓" if positions else "已连接；当前无持仓"),
                "error_class": None, "equity_usd": equity, "available_usd": avail,
                "used_margin_usd": usedm, "positions": positions,
                "source": f"ccxt.{ex}.fetch_balance/fetch_positions", "read_at": read_at}
    except Exception as exc:                           # noqa: BLE001
        name = type(exc).__name__
        msg = str(exc)[:300]
        auth = any(k in name.lower() or k in msg.lower() for k in
                   ("auth", "api key", "signature", "invalid key", "unauthorized", "forbidden",
                    "passphrase", "401", "403", "permission"))
        return {"exchange": ex, "account_label": "default",
                "status": "auth_failed" if auth else "error",
                "detail": f"读取失败：{name}: {msg}",
                "error_class": name, "positions": [], "read_at": read_at}
    finally:
        try:
            ex_obj.close()
        except Exception:                              # noqa: BLE001
            pass


def read_and_store() -> dict:
    """读取全部已配置交易所并落快照（未配置的也写一条 `not_configured`，便于前端如实展示）。"""
    creds = _load_credentials()
    rep = credential_report()
    con = L.live_conn()
    results = {}
    try:
        for ex in CRED_ENV:
            if ex not in creds:
                row = {"exchange": ex, "account_label": "default", "status": "not_configured",
                       "detail": ("未配置只读凭据：缺 " + "、".join(rep[ex]["missing"])),
                       "error_class": None, "equity_usd": None, "available_usd": None,
                       "used_margin_usd": None, "positions": [], "source": None,
                       "read_at": L.now_ms()}
            else:
                row = read_exchange(ex, creds[ex])
            con.execute(
                "INSERT INTO live_account_snapshot(exchange,account_label,read_at,status,detail,"
                "error_class,equity_usd,available_usd,used_margin_usd,positions_json,source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(exchange,account_label) DO UPDATE SET "
                "read_at=excluded.read_at, status=excluded.status, detail=excluded.detail,"
                "error_class=excluded.error_class, equity_usd=excluded.equity_usd,"
                "available_usd=excluded.available_usd, used_margin_usd=excluded.used_margin_usd,"
                "positions_json=excluded.positions_json, source=excluded.source",
                (row["exchange"], row["account_label"], row["read_at"], row["status"],
                 row["detail"], row["error_class"], row["equity_usd"], row["available_usd"],
                 row["used_margin_usd"], json.dumps(row["positions"], ensure_ascii=False, default=str),
                 row.get("source")))
            results[ex] = {k: row[k] for k in ("status", "detail", "error_class")}
        con.commit()
    finally:
        con.close()
    return {"credentials": rep, "results": results}


if __name__ == "__main__":
    print(json.dumps({"credentials": credential_report(),
                      "ccxt": (_import_ccxt() is not None),
                      "read": read_and_store()}, ensure_ascii=False, indent=1, default=str))
