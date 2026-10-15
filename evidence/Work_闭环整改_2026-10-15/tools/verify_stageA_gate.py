#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段A 只读验证：冻结参数 + 服务端新开仓总闸（不改任何账本）。"""
import json
import sys

BACKEND = "/Users/boos/Desktop/威科夫因子共振趋势/框架/backend"
sys.path.insert(0, BACKEND)

import paper_engine as PE  # noqa: E402

out = {
    "approved_params": PE.approved_params(),
    "gate": PE.new_entry_gate(),
}
print(json.dumps(out, ensure_ascii=False, indent=1))
