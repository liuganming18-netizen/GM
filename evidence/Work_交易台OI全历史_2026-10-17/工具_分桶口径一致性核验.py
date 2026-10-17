#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分桶口径一致性核验（只读；不改库、不启服务、不写工程数据）。

背景：`period=auto` 的「自适应金字塔」有**三条**互不相干的实现路径：
  ① 热层 SQL  —— `adapter._oi_spread_expr()`（SQLite，绑定参数版，用 `/`）
  ② 冷层 SQL  —— `adapter._oi_spread_sql()`（DuckDB，字面量版，用 `//`）
  ③ Python 侧 —— `adapter._pyramid_size()`（合并热/冷后二次分桶）
三者必须**逐档同语义**，否则热段与冷段会出现不同桶宽（表现为密度突变或空洞）。

本脚本对同一组时间戳断言三路给出的**桶号完全一致**，并把每个档位边界打印出来供人读。
采样点覆盖：现在、每档阈值 ±1 秒、以及 400 天前 / 3000 天前。

用法：
    /Users/boos/.workbuddy-ai/binaries/python/envs/default/bin/python \
    工具_分桶口径一致性核验.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
BACKEND = os.path.join(ROOT, "框架", "backend")
for p in (os.path.join(ROOT, "框架"), BACKEND, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import adapter  # noqa: E402

STEPS = adapter.OI_SPREAD_STEPS
FAILURES: list[str] = []


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILURES.append(msg)


def main():
    now = adapter.now_ms()
    expr_hot, args_hot = adapter._oi_spread_expr({"limit": ["4000"]})
    expr_cold = adapter._oi_spread_sql(now)

    print("=" * 78)
    print("分桶表达式")
    print("=" * 78)
    print(f"  热层 SQL（SQLite，绑定参数）: {expr_hot}")
    print(f"  冷层 SQL（DuckDB，字面量）  : {expr_cold}")
    print(f"  档位                        : {STEPS}")
    print()

    # 采样点：每档阈值 ±1 秒 + 现在 + 远期
    samples = [now, now - 1000]
    for age, _ms in STEPS[:-1]:
        samples += [now - age - 1000, now - age + 1000]
    samples += [now - 400 * 86_400_000, now - 3000 * 86_400_000]

    import duckdb
    print("=" * 78)
    print(f"A) 三路桶号一致性（{len(samples)} 个采样点）")
    print("=" * 78)
    with duckdb.connect() as d:
        for ts in samples:
            b_py = ts // adapter._pyramid_size(ts, now)
            con = sqlite3.connect(":memory:")
            # ⚠ 参数顺序：CASE 里的 `?` 在前、子查询的 ts 在最后（SQLite 按出现顺序绑定）
            b_hot = con.execute(f"SELECT {expr_hot} FROM (SELECT ? AS ts)",
                                args_hot + [ts]).fetchone()[0]
            con.close()
            b_cold = d.execute(f"SELECT {expr_cold} FROM (SELECT ? AS ts)", [ts]).fetchone()[0]
            check(b_hot == b_cold == b_py,
                  f"ts={ts} 热={b_hot} 冷={b_cold} py={b_py} 桶宽={adapter._pyramid_size(ts, now)//60000}分")

    print()
    print("=" * 78)
    print("B) 档位边界（人读）")
    print("=" * 78)
    for age, ms in STEPS[:-1]:
        edge = now - age
        print(f"  阈值 {age // 86_400_000:>4} 天：边界内 "
              f"{adapter._pyramid_size(edge + 1, now) // 60000:>5} 分 / 边界外 "
              f"{adapter._pyramid_size(edge - 1, now) // 60000:>5} 分")
    print(f"  最外档（无阈值）  ："
          f"{adapter._pyramid_size(now - 5000 * 86_400_000, now) // 60000:>5} 分")

    print()
    print("=" * 78)
    print("C) 冷层接入状态（含兜底来源）")
    print("=" * 78)
    st = adapter.repo_status()
    for k, v in st.items():
        print(f"  {k}: {v}")
    check(st["接入"] is True, "冷层已接入（本机默认部署）")

    out = {
        "生成时间": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "档位": [[a, m] for a, m in STEPS],
        "热层SQL": expr_hot,
        "冷层SQL": expr_cold,
        "采样点数": len(samples),
        "冷层状态": {k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
                    for k, v in st.items()},
        "失败": FAILURES,
    }
    with open(os.path.join(HERE, "对账_分桶口径一致性_2026-10-17.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print()
    print("结论:", "全部一致 ✓" if not FAILURES else f"{len(FAILURES)} 项不一致 ✗")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
