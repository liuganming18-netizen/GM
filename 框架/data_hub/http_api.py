"""按需启动的本机只读数据接口，不触发上游采集。"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import INTERVAL_MS


def read_route(store, url):
    parsed = urlsplit(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path == "/api/health":
        return {"状态": "可用", "模式": "本机只读"}
    if parsed.path == "/api/data/status":
        return store.status()
    if parsed.path != "/api/candles":
        raise LookupError("接口不存在")
    required = {"exchange", "market", "symbol", "timeframe"}
    optional = {"start", "end", "limit", "source_id"}
    if required - set(params) or set(params) - required - optional:
        raise ValueError("参数缺失或不在只读接口参数表中")
    if any(len(v) != 1 or not v[0] for v in params.values()):
        raise ValueError("参数必须唯一且非空")
    args = {k: v[0] for k, v in params.items()}
    if args["timeframe"] not in INTERVAL_MS:
        raise ValueError("未配置的原生周期")
    for key in optional & set(args):
        args[key] = int(args[key])
        if args[key] < 0:
            raise ValueError("读取范围与数量不得为负数")
    for key in required:
        if len(args[key]) > 128:
            raise ValueError("查询标识过长")
    candles = store.candles(**args)
    return {"数据": candles, "来源": "统一数据库", "实际返回根数": len(candles),
            "提示": "按实际存储返回；空列表不自动下载，不生成替代数据。",
            "顺序": "开盘时间升序；限量时读取区间内最新记录"}


def serve(store, port=8787):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                body, status = read_route(store, self.path), 200
            except LookupError as exc:
                body, status = {"错误": str(exc)}, 404
            except (ValueError, TypeError) as exc:
                body, status = {"错误": str(exc)}, 400
            except Exception:
                body, status = {"错误": "数据库读取失败"}, 500
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            # 不输出URL全文；同源前端通过其后台代理访问，不开启跨域凭据。
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"本机只读数据接口：http://127.0.0.1:{port}；停止请按Ctrl+C")
        server.serve_forever()
