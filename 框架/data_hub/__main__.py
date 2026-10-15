"""显式本机命令；默认没有网络、后台调度、认证或交易行为。"""

import argparse
import json
from .store import MarketStore
from .gateway import PublicGateway, RejectedRequest, DeferredRequest


def main():
    parser = argparse.ArgumentParser(description="统一行情数据库与公共只读接口管理")
    # P3 切流（2026-10-13）：CLI 默认库改为新热库 market_v2.sqlite3（旧库已退役）
    parser.add_argument("--db", default="data/market_v2.sqlite3", help="统一数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="建立数据库结构")
    sub.add_parser("status", help="查看入库覆盖及接口状态")
    server = sub.add_parser("serve", help="按需启动本机只读接口，不自动下载或采集")
    server.add_argument("--port", type=int, default=8787)
    imp = sub.add_parser("import-binance", help="导入一份完整且有校验和的币安合约归档")
    for field in ("zip", "checksum", "symbol", "timeframe", "source-url"):
        imp.add_argument("--" + field, required=True)
    candles = sub.add_parser("candles", help="按窗口读取已有原生K线")
    candles.add_argument("--exchange", required=True)
    candles.add_argument("--market", required=True)
    candles.add_argument("--symbol", required=True)
    candles.add_argument("--timeframe", required=True)
    candles.add_argument("--start", type=int, default=0)
    candles.add_argument("--end", type=int)
    candles.add_argument("--limit", type=int, default=500)
    candles.add_argument("--source-id", type=int, help="绑定已有文件版本，避免历史修订改变读取结果")
    request = sub.add_parser("request", help="通过共享网关发起一次公共REST，不自动循环")
    request.add_argument("--endpoint", required=True)
    request.add_argument("--params", default="{}", help="只接受登记过的公开查询参数JSON")
    request.add_argument("--online", action="store_true", help="明确允许本次网络请求")
    args = parser.parse_args()
    store = MarketStore(args.db)
    try:
        if args.command in ("init", "status"):
            result = store.status()
        elif args.command == "serve":
            from .http_api import serve
            if not 1024 <= args.port <= 65535:
                raise ValueError("端口须为1024至65535")
            serve(store, args.port)
            return
        elif args.command == "import-binance":
            result = store.import_binance_zip(args.zip, args.checksum, args.symbol, args.timeframe, args.source_url)
        elif args.command == "candles":
            result = store.candles(args.exchange, args.market, args.symbol, args.timeframe,
                                   args.start, args.end, args.limit, args.source_id)
        else:
            if not args.online:
                parser.error("未指定--online，不进行外部请求")
            params = json.loads(args.params)
            if not isinstance(params, dict):
                raise ValueError("查询参数应为JSON对象")
            result = PublicGateway(store).get(args.endpoint, params)
    except (ValueError, RejectedRequest, DeferredRequest) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
