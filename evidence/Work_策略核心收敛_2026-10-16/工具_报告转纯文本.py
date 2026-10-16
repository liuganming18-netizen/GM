#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把交付报告 Markdown 转成「整段可复制」的纯文本（去 Markdown 语法）。

规则
────
· 标题  `#`/`##`/`###`  → 去号，改为「标题 + 下划线」或加序号前缀
· 强调  `**x**` / `*x*`  → 去星号
· 行内码 `` `x` ``        → 去反引号
· 表格  改为逐行 `  · 列1 ｜ 列2 ｜ 列3`（竖线保留为全角分隔，纯文本可读）
· 代码围栏 ``` 行 → 删除（内容保留）
· 引用  `>`  → 去号，缩进两格
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "交付_Work①_策略核心收敛_2026-10-16.md"
DST = HERE / "交付_Work①_策略核心收敛_2026-10-16.txt"


def strip_inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", s)
    s = s.replace("`", "")
    return s


def main() -> int:
    lines = SRC.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        st = ln.strip()

        if st.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            out.append("    " + ln if ln.strip() else "")
            i += 1
            continue

        # 表格块
        if st.startswith("|") and i + 1 < len(lines) and set(lines[i + 1].strip()) <= set("|-: "):
            header = [c.strip() for c in st.strip("|").split("|")]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                cells = [strip_inline(c) for c in cells]
                # 首列作键，其余以「键=值」串起
                if len(cells) >= 2 and cells[0]:
                    rest = "；".join(
                        f"{header[j] if j < len(header) else j}={cells[j]}"
                        for j in range(1, len(cells)) if cells[j])
                    out.append(f"  · {cells[0]}" + (f" ｜ {rest}" if rest else ""))
                else:
                    out.append("  · " + " ｜ ".join(c for c in cells if c))
                i += 1
            out.append("")
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", st)
        if m:
            lvl, text = len(m.group(1)), strip_inline(m.group(2))
            if lvl == 1:
                out.append("")
                out.append("=" * 72)
                out.append(text)
                out.append("=" * 72)
            elif lvl == 2:
                out.append("")
                out.append("─" * 72)
                out.append(text)
                out.append("─" * 72)
            else:
                out.append("")
                out.append(f"【{text}】")
            i += 1
            continue

        # 引用
        if st.startswith(">"):
            body = strip_inline(st.lstrip("> ").rstrip())
            out.append("  " + body if body else "")
            i += 1
            continue

        # 列表
        m = re.match(r"^(\s*)[-*]\s+(.*)$", ln)
        if m:
            out.append(m.group(1) + "  • " + strip_inline(m.group(2)))
            i += 1
            continue

        m = re.match(r"^(\s*)(\d+)\.\s+(.*)$", ln)
        if m:
            out.append(f"{m.group(1)}  {m.group(2)}) " + strip_inline(m.group(3)))
            i += 1
            continue

        out.append(strip_inline(ln) if ln.strip() else "")
        i += 1

    text = "\n".join(out)
    # 收尾清理：跨行粗体残留、分隔线、多余空行
    text = text.replace("**", "")
    text = re.sub(r"(?m)^\s*-{3,}\s*$", "", text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    DST.write_text(text.strip() + "\n", encoding="utf-8")
    print(f"已生成 {DST.name}：{len(text)} 字符 / {text.count(chr(10)) + 1} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
