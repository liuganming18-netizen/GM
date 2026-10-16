#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W1 · 测试文件外部路径改写（把旧外部工程路径 → 工程内推导）。

只改路径常量与 sys.path 注入，不改任何断言。用法：python 工具_W1_测试改写.py [--apply]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/Users/boos/Desktop/威科夫因子共振趋势")
APPLY = "--apply" in sys.argv
T = ROOT / "框架" / "backend" / "tests"

# ── 规则 1：`PROJ = Path("<外部工程>")` + sys.path 注入 ────────────────────────
R1_OLD = '''PROJ = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统")
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "logs"))'''
R1_NEW = '''# W1（2026-10-16）：原指向工程外路径（已删）。现按工程布局推导：
#   HERE = 框架/backend；FRAMEWORK = 框架/（含 `data_hub` 包）；PROJECT_ROOT = 工程根。
FRAMEWORK = HERE.parent
PROJECT_ROOT = HERE.parent.parent
PROJ = FRAMEWORK
sys.path.insert(0, str(FRAMEWORK))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "logs"))'''

R2_OLD = '''PROJ = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统")
sys.path.insert(0, str(PROJ))
sys.path.insert(0, str(HERE))'''
R2_NEW = '''# W1（2026-10-16）：原指向工程外路径（已删）⇒ 改按工程布局推导。
FRAMEWORK = HERE.parent
PROJECT_ROOT = HERE.parent.parent
PROJ = FRAMEWORK
sys.path.insert(0, str(FRAMEWORK))
sys.path.insert(0, str(HERE))'''

R3_OLD = '''PROJ = Path("/Users/boos/WorkBuddy AI/加密货币趋势判断系统")
sys.path.insert(0, str(ROOT / "logs"))
sys.path.insert(0, str(ROOT / "历史下载"))
sys.path.insert(0, str(PROJ))'''
R3_NEW = '''# W1（2026-10-16）：原指向工程外路径（已删）⇒ 改按工程布局推导。
PROJ = ROOT.parent.parent
sys.path.insert(0, str(ROOT / "logs"))
sys.path.insert(0, str(ROOT / "历史下载"))
sys.path.insert(0, str(ROOT.parent))'''

# ── 规则 4：`PROJECT = "<外部工程>"` ─────────────────────────────────────────
R4_OLD = '''PROJECT = "/Users/boos/WorkBuddy AI/加密货币趋势判断系统"'''
R4_NEW = '''# W1（2026-10-16）：原指向工程外路径（已删）⇒ 改按工程布局推导。
FRAMEWORK = str(HERE.parent)          # 框架/（含 data_hub 包）
PROJECT = str(HERE.parent.parent)     # 工程根（DATA_HUB_PROJECT 语义）'''

R4B_OLD = '''for _p in (str(HERE), PROJECT, str(HERE / "logs")):'''
R4B_NEW = '''for _p in (str(HERE), FRAMEWORK, PROJECT, str(HERE / "logs")):'''

R4C_OLD = '''for p in (str(HERE), PROJECT):'''
R4C_NEW = '''for p in (str(HERE), FRAMEWORK, PROJECT):'''

R4D_OLD = '''for _p in (str(HERE), PROJECT):'''
R4D_NEW = '''for _p in (str(HERE), FRAMEWORK, PROJECT):'''

# ── 规则 5：FIXDIR（旧 OKX 成交量离线修复目录）────────────────────────────────
R5_OLD = '''FIXDIR = Path("/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/"
              "OKX成交量离线修复_2026-10-11")'''
R5_NEW = '''# W1（2026-10-16）：原指向工程外工作区（已删）。该批修复脚本**未随工程迁移**
# ⇒ 这里给出工程内正式位置（可用 `OKX_FIX_DIR` 覆盖）；目录缺失时相关用例报错，
# **不静默跳过、不退回旧路径**。
FIXDIR = Path(os.environ.get("OKX_FIX_DIR") or (
    Path(__file__).resolve().parents[3] / "数据" / "研究输入" / "外部迁移_2026-10-16" /
    "OKX成交量离线修复_2026-10-11"))'''

RULES = [(R1_OLD, R1_NEW), (R2_OLD, R2_NEW), (R3_OLD, R3_NEW),
         (R4_OLD, R4_NEW), (R4B_OLD, R4B_NEW), (R4C_OLD, R4C_NEW), (R4D_OLD, R4D_NEW),
         (R5_OLD, R5_NEW)]


def main() -> int:
    n = 0
    for p in sorted(T.glob("test_*.py")):
        s = p.read_text(encoding="utf-8")
        o = s
        for a, b in RULES:
            if a in s:
                s = s.replace(a, b)
                print(f"[rule] {p.name}: 命中")
        if s != o:
            n += 1
            if APPLY:
                p.write_text(s, encoding="utf-8")
            print(f"[{'wrote' if APPLY else 'dry'}] {p.name}")
    print(f"\n共 {n} 个测试文件{'已改写' if APPLY else '待改写'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
