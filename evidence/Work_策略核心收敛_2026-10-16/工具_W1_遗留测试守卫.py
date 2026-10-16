#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W1（2026-10-16）· 给「回归旧工作区遗留件」的测试模块插入显式 SKIP 守卫。

背景
────
以下 6 个测试模块回归的是**旧工作区遗留件**（`_pool_gate` / `_pool_restfill` /
`OKX成交量*修复*` 目录下的脚本）。这些件未随工程迁移，且属施工单**禁止恢复**项
⇒ 组件缺失时模块级导入即 `FileNotFoundError` / `ModuleNotFoundError`，
导致 pytest **整个套件无法收集**（不是测试失败，是收集中断）。

处理（fail-closed，不静默）
──────────────────────────
在「docstring 之后、`__future__` 导入之后、其余导入之前」插入守卫块（带起止标记）：
组件缺失 ⇒ `pytest.skip(..., allow_module_level=True)`。
· 显式 SKIP ≠ PASS：结果里会列出 skip 原因，不会被当成通过；
· 组件若将来被正式迁入，守卫自动失效，测试照常运行。

幂等：带 `W1-GUARD-BEGIN/END` 标记；重复运行先摘除旧块再重插。

用法：`python 工具_W1_遗留测试守卫.py --apply`
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TESTS = ROOT / "框架" / "backend" / "tests"

MIG_REL = ("数据", "研究输入", "外部迁移_2026-10-16")

BEGIN = "# ── W1-GUARD-BEGIN（2026-10-16）遗留组件守卫（显式 SKIP，非 PASS）──"
END = "# ── W1-GUARD-END ──"
OLD_MARKER = "# ── W1（2026-10-16）遗留组件守卫"

# 模块名 → (需要探测的文件相对 MIG 的路径, 需要探测的顶层模块名)
TARGETS = {
    "test_okx_volume_fix_isolation.py": (
        (("OKX成交量离线修复_2026-10-11", "04_okx_volume_fix.py"),), ()),
    "test_okx_volume_fix_rectification.py": (
        (("OKX成交量离线修复整改_2026-10-11", "04_okx_volume_fix_v2.py"),), ()),
    "test_okx_volume_exec_v3.py": (
        (("OKX成交量执行验收整改_2026-10-11", "04_okx_volume_fix_v3.py"),), ()),
    "test_okx_volume_mapping.py": ((), ("_pool_restfill",)),
    "test_rest_import_contract.py": ((), ("_pool_restfill",)),
    "test_pool_gate.py": ((), ("_pool_gate",)),
    "test_history_integrity.py": ((), ("_pool_restfill", "_pool_pipeline")),
}


def _strip_guard(src: str) -> str:
    """摘除既有守卫块（幂等重插）。兼容首版无 END 标记的旧块。"""
    lines = src.splitlines(keepends=True)
    out, mode = [], None          # mode: None | "new" | "old"
    for ln in lines:
        if mode is None:
            if BEGIN in ln:
                mode = "new"
                continue
            if OLD_MARKER in ln:
                mode = "old"
                continue
            out.append(ln)
            continue
        # 处于跳过模式
        if mode == "new":
            if END in ln:
                mode = None
            continue
        if mode == "old":
            if ln.strip() == ")":       # 旧块以 skip(...) 的收尾 ")" 结束
                mode = None
            continue
    return "".join(out)


def _insert_index(src: str) -> int:
    """返回插入行号（0-based）：docstring 与 `__future__` 导入之后。"""
    tree = ast.parse(src)
    idx = 0
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        idx = 1
    while idx < len(body):
        st = body[idx]
        if isinstance(st, ast.ImportFrom) and st.module == "__future__":
            idx += 1
            continue
        break
    if idx == 0:
        return 0
    return body[idx - 1].end_lineno or 0


def _guard_text(files, modules) -> str:
    if files:
        probes = "(\n    " + ",\n    ".join(
            'MIG_W1 / "{}" / "{}"'.format(*p) for p in files) + ",\n)"
    else:
        probes = "()"
    mods = repr(tuple(modules))
    return f'''{BEGIN}
# 本模块回归的是**旧工作区遗留件**（未随工程迁移，且属施工单禁止恢复项）。
# 组件缺失 ⇒ **显式 SKIP**：既不 PASS、也不静默 fallback（fail-closed 纪律）。
# 若遗留件将来被正式迁入，守卫自动失效，测试照常运行。
import importlib.util as _ilu_w1
from pathlib import Path as _PathW1

import pytest as _pytest_w1

MIG_W1 = _PathW1(__file__).resolve().parents[3].joinpath({", ".join(repr(x) for x in MIG_REL)})
_missing_w1 = [str(p) for p in {probes} if not p.exists()]
_missing_w1 += [m for m in {mods} if _ilu_w1.find_spec(m) is None]
if _missing_w1:
    _pytest_w1.skip(
        "W1: 旧工作区遗留组件未迁移且禁止恢复 ⇒ 显式 SKIP（非 PASS）: "
        + ", ".join(_missing_w1),
        allow_module_level=True,
    )
{END}
'''


def main(apply: bool) -> int:
    changed = 0
    for name, (files, modules) in TARGETS.items():
        p = TESTS / name
        if not p.exists():
            print(f"[跳过] 不存在: {p}")
            continue
        raw = p.read_text(encoding="utf-8")
        src = _strip_guard(raw)
        n = _insert_index(src)
        lines = src.splitlines(keepends=True)
        out = "".join(lines[:n]) + _guard_text(files, modules) + "".join(lines[n:])
        # 语法自检
        try:
            ast.parse(out)
        except SyntaxError as exc:
            print(f"[语法错误·未写入] {name}: {exc}")
            continue
        if apply and out != raw:
            p.write_text(out, encoding="utf-8")
        print(f"[{'写入' if apply else '预演'}] {name}  插入点=第 {n} 行")
        changed += 1
    print(f"\n共 {changed} 个文件{'已改写' if apply else '待改写'}。")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
