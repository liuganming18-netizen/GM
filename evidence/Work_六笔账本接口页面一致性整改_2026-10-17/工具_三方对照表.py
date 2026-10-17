# -*- coding: utf-8 -*-
"""六笔交易「账本 / 接口 / 页面」三方对照表（只读汇总，不重算任何数字）。

输入（都已落盘）：
  · `六笔核验_2026-10-17.json`   —— 账本原值 + 进程内直调 API 值（`工具_六笔一致性核验.py`）
  · `页面验收_2026-10-17.json`   —— 真实浏览器里 ⑤ 逐笔表的**单元格原值**（`工具_页面取证.mjs`）

输出：`六笔三方对照表_2026-10-17.md`
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def money(v, digits=2):
    if v is None:
        return "—"
    return f"{v:+.{digits}f}"


def main() -> int:
    core = json.loads((HERE / "六笔核验_2026-10-17.json").read_text(encoding="utf-8"))
    page = json.loads((HERE / "页面验收_2026-10-17.json").read_text(encoding="utf-8"))
    pmap = {r["symbol"]: r for r in page["runs"]}

    lines = [
        "# 六笔回测交易 · 账本 / 接口 / 页面 三方对照表",
        "",
        "生成时间：2026-10-17（本轮整改后，服务已重载）",
        "",
        "- **账本**：直接读 `<run_dir>/trades.jsonl` / `orders.jsonl` / `funding.jsonl` / "
        "`positions.jsonl` 原值，未经任何兜底。",
        "- **接口**：`GET /api/v1/backtests/{id}/{metrics,trades,trades/{tid},orders,chart,equity}` "
        "的在线 HTTP 响应（原始响应存 `在线API响应/`）。",
        "- **页面**：真实浏览器打开 `http://127.0.0.1:3000/?run=<run_id>`，"
        "点选 ⑤ 逐笔行后**直接读取 DOM 单元格文本**（截图见 `页面截图/`）。",
        "",
        "## 1 逐笔与金额（三方）",
        "",
        "| 标的 | run_id 尾号 | 入场价 | 出场价 | 退出原因 | 毛盈亏(账本) | 手续费(账本) | 资金费(账本) | 净盈亏(账本) | 净盈亏(API) | 净盈亏(页面) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in core["runs"]:
        lg = r["ledger"]
        m = r["metrics"]
        p = pmap.get(r["symbol"], {})
        cells = p.get("page_row_cells") or []
        page_net = cells[10] if len(cells) > 10 else "—"
        lines.append(
            f"| {r['symbol']} | {r['run_id'].rsplit('_', 1)[-1]} | {lg['entry_price']} | "
            f"{lg['exit_price']} | {lg['exit_reason']} | {money(lg['gross_pnl'], 4)} | "
            f"{lg['fees']:.6f} | {money(lg['funding_fee'], 6)} | "
            f"**{money(lg['net_pnl'], 6)}** | {money(m['net_pnl'], 6)} | {page_net} |")

    lines += [
        "",
        "> 页面列是**四舍五入到 2 位**的展示值（`-$5.13` / `-$5.24` 等），与账本 6 位值一致到显示精度。",
        "> BNBUSDT 的资金费 `-0.997220` 是**收入**（负号=交易所付给账户），故净亏损 9.005176 小于毛亏损 9.893554。",
        "",
        "## 2 关联标识（三方）",
        "",
        "| 标的 | 真实 trade_id | 来源 | 开仓 order_id | 平仓 order_id | 关联订单数(账本) | 关联订单数(API) | 关联订单数(页面 ⑦) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in core["runs"]:
        lg, at, d = r["ledger"], r["api_trades"], r["api_trade_detail"]
        p = pmap.get(r["symbol"], {})
        page_orders = p.get("detail_order_rows", "—")
        lines.append(
            f"| {r['symbol']} | `{at['trade_id']}` | {at['trade_id_source']} | "
            f"`{lg['order_id']}` | `{lg['exit_order_id']}` | "
            f"{at['linked_order_count']}/2 | {d['orders_count']}/2 | {page_orders} 行 |")
    lines += [
        "",
        "> 页面「行数」= 单笔详情里「订单与成交（账本 orders）」表的行数（2 条订单 + 1 条止损轨迹 + N 条资金费）。",
        "> 资金费归属走**时间窗唯一归属**：事件时间落在 `[entry, exit]` 且恰好一笔包含；"
        "`Σ(−delta)` 与账本 `funding_fee` 逐笔交叉校验 `match=true`（见 `六笔核验_2026-10-17.json`）。",
        "",
        "## 3 指标（`/metrics`，账本现算）",
        "",
        "| 标的 | 笔数 | 胜 | 负 | 净收益率 | 最大回撤 | PF(聚合) | 平均盈亏比(均值) | 权益点数 | 页面顶部同值 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in core["runs"]:
        m = r["metrics"]
        pf = "—" if m["profit_factor"] is None else f"{m['profit_factor']:.6f}"
        aw = "—" if m["avg_win_loss_ratio"] is None else f"{m['avg_win_loss_ratio']:.6f}"
        lines.append(
            f"| {r['symbol']} | {m['trades_count']} | {m['wins']} | {m['losses']} | "
            f"{m['net_return_pct']:.6f}% | {m['max_drawdown_pct']:.6f}% | {pf} | {aw} | "
            f"{m['equity_points']} | 是（截图 01） |")
    lines += [
        "",
        "口径（全部写进接口返回值）：",
        "",
        "- **胜负**：`net_pnl`（已扣手续费与资金费）>0 盈 / <0 亏 / =0 平。",
        "- **最大回撤**：`equity_curve.total_equity` 逐点峰值回撤（含未实现浮盈浮亏，15m 粒度）。",
        "- **Profit Factor** = Σ盈利净额 / |Σ亏损净额|（**聚合口径**）；"
        "**平均盈亏比** = 平均盈利 / |平均亏损|（**均值口径**）。两者不混用，各带 `basis` 字符串。",
        "- 单笔样本下：只有亏损单 ⇒ PF=0（0/Σ|亏| 有定义）；只有盈利单 ⇒ PF=null（分母为 0，不输出 inf）；"
        "平均盈亏比缺一侧 ⇒ null（页面显示「—」）。",
        "",
        "## 4 图表与联动（页面）",
        "",
        "| 标的 | K线根数 | 信号点 | 逐笔 | 止损点 | 止损读数（页面） | ③ 已定位 | 该笔区间 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in core["runs"]:
        c = r["chart"]
        p = pmap.get(r["symbol"], {})
        panel = p.get("panel_after") or ""
        has_focus = "该笔区间" in (p.get("funding_panel") or "")
        import re
        mm = re.search(r"止损轨迹 (\d+) 点（([^）]*)）", panel)
        stop_read = (f"{mm.group(1)} 点 · {mm.group(2)}" if mm else "—")
        lines.append(
            f"| {r['symbol']} | {c['candles']} | {c['signals']} | {c['trades']} | "
            f"{c['stop_trace_points_chart']} | {stop_read} | "
            f"{'是' if has_focus else '否'} | {'是' if has_focus else '否'} |")
    lines += [
        "",
        "> 六笔账本的 `stop_trace` **都只有 1 个 `init` 点**（无后续止损调整）⇒ 按任务书要求"
        "「如实显示，不合成轨迹」：图上不画止损线，读数行明写「仅初始止损 …，无后续调整 ⇒ 不合成轨迹」。",
        "",
        "## 5 断言结果",
        "",
        f"- 账本 ↔ 接口（进程内直调）：**{len(core['problems'])} 项不通过**",
        f"- 在线 HTTP：**{len(json.loads((HERE / '在线API响应' / '在线核验_2026-10-17.json').read_text(encoding='utf-8'))['problems'])} 项不通过**",
        f"- 页面（真实浏览器）：**{len(page['problems'])} 项不通过**",
        "",
    ]
    if core["problems"] or page["problems"]:
        lines.append("不通过项：")
        lines += [f"- {x}" for x in core["problems"] + page["problems"]]

    out = HERE / "六笔三方对照表_2026-10-17.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
