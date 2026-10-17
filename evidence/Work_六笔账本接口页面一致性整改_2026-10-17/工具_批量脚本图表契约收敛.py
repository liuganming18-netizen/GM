# -*- coding: utf-8 -*-
"""把项目根 7 个批量脚本里各自复制的 `generate_chart_payload()` 换成共享契约调用。

只替换该函数体；其余一行不动。替换前先备份到 `<file>.bak_20261017`。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
FILES = [
    "run_batch_round3_50.py", "run_batch_round4_all_remain.py",
    "run_batch_round5_100.py", "run_batch_round6_100.py",
    "run_clean_batch_test.py", "run_expand_batch_test.py",
    "run_random_50_batch_test.py",
]

NEW_BODY = '''def generate_chart_payload(symbol: str, run_dir: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame, initial_equity: float):
    """统一的 chart 序列化契约（实现见 框架/backend/backtest_chart.py）。

    原实现在每个脚本里各写一份，并写死 signals=[] / funding_events=[] /
    benchmark.return_pct=0.0 / trade_id=null，stop_trace 用 `ts`、candles 用数组元组、
    json.dump 无 indent ⇒ 磁盘上出现**两套 chart 格式**（2026-10-17 查实）。
    现全部收敛到共享实现：K 线为对象、trade_id 取账本真实标识（positions.jsonl）、
    stop_trace 用官方 `t`、信号与资金费取自本 run 的 candidates.jsonl / funding.jsonl、
    基准如实标注为纯价格口径（net_return_pct 留空）。
    """
    _backend = os.path.join(PROJECT_ROOT, "框架", "backend")
    if _backend not in sys.path:
        sys.path.insert(0, _backend)
    import backtest_chart as _BC
    return _BC.generate_chart_payload(symbol, run_dir, df_1h, df_15m, initial_equity)


'''

PAT = re.compile(r"^def generate_chart_payload\(.*?\n(?:(?:[ \t].*)?\n)*", re.M)


def main() -> int:
    changed = []
    for name in FILES:
        p = ROOT / name
        src = p.read_text(encoding="utf-8")
        m = PAT.search(src)
        if not m:
            print(f"  ✗ {name}: 未找到 generate_chart_payload")
            continue
        if "_BC.generate_chart_payload" in m.group(0):
            print(f"  = {name}: 已是共享实现，跳过")
            continue
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak_20261017"))
        new = src[:m.start()] + NEW_BODY + src[m.end():]
        p.write_text(new, encoding="utf-8")
        changed.append(name)
        print(f"  ✓ {name}: 已替换（旧函数 {len(m.group(0))} 字符 → {len(NEW_BODY)} 字符）")
    print(f"changed={len(changed)}")
    return 0 if changed else 1


if __name__ == "__main__":
    sys.exit(main())
