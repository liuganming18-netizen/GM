#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开扩展字段的**本机可读快照**（纯文件读写；**不做任何网络调用**）。

为什么是「文件快照」而不是新库/新表/新进程：
  · 用户口径：复用既有统一网关与**现有采集工作器**、合理周期获取并保存**可读快照**、
    本机 API 读取结果；**维持「前端读取不触发外部采集」**。
    本模块只承担「采集器写 / API 读」的落盘与取回，**不新增库、不新增表、
    不新增端口、不新增采集进程**。
  · 行情库（`market.sqlite3`）正被历史全池补缺与实时采集**并发写入**，本机实测会出现
    `database is locked`；快照走文件可**完全避开**写库争锁，不给历史侧添压。
  · 文件即「可读快照」：**单标的单 JSON**，人可直接打开核对数值、来源与时点。

写入约定（采集器侧 `write`）：
  · 原子替换（唯一临时文件 + `os.replace`，同目录同文件系统 ⇒ 原子），
    并发读者**永远看到完整 JSON**，不会读到半个文件。
读取约定（API 侧 `read` / `freshness`）：
  · `read` 只读一个标的小文件；缺失返回 `None`（**这不是错误**，是本机快照未覆盖）。
  · `freshness` 按**当前时刻**重算 `age_seconds` 与 `status`（ok/stale），
    并给出「数据时点 / 是否过期」说明；**不发起任何网络请求**。
索引（`_index.json`）：
  · `write_index` / `read_index` 记录最近一轮刷新的时间、目标数、成功/失败数、请求数、
    用时，供「负载 / 新鲜度」验收核对。
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP_DIR = Path(os.environ.get("PUBLIC_EXTRAS_SNAPSHOT_DIR")
                or (HERE / "快照" / "public_extras"))
INDEX_NAME = "_index.json"
# 快照超过该年龄即标记 stale（默认 900s = 3 个刷新周期；刷新周期 300s）。
DEFAULT_STALE_AFTER_S = int(os.environ.get("PUBLIC_EXTRAS_STALE_AFTER_S", "900"))

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_INDEX_LOCK = threading.Lock()


def iso(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return None


def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _safe(text):
    return _SAFE_RE.sub("_", str(text or ""))[:64]


def path_for(exchange, market_id):
    """单标快照路径：`<dir>/<exchange>__<market_id>.json`。"""
    return SNAP_DIR / f"{_safe(exchange)}__{_safe(market_id)}.json"


def _atomic_write(path, obj):
    """原子写 JSON：唯一临时文件 + `os.replace`（同目录 ⇒ 原子替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write(exchange, market_id, payload, refreshed_at_ms=None):
    """写入单标快照（原子）。返回落盘路径。

    `payload` 即采集器归一化结果（与 `/api/v1/.../public-extras` 同形）；
    另打 `snapshot_refreshed_at_ms` / `snapshot_refreshed_at` 两个**快照自身**的时点，
    便于读取侧计算新鲜度（**不改动**业务字段语义）。
    """
    ts = int(refreshed_at_ms if refreshed_at_ms is not None else _now_ms())
    body = dict(payload or {})
    body["snapshot_refreshed_at_ms"] = ts
    body["snapshot_refreshed_at"] = iso(ts)
    body["snapshot_exchange"] = exchange
    body["snapshot_market_id"] = market_id
    path = path_for(exchange, market_id)
    _atomic_write(path, body)
    return path


def read(exchange, market_id):
    """读单标快照；缺失或损坏返回 None（**不抛错、不联网**）。"""
    path = path_for(exchange, market_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_symbols():
    """已落盘快照的 `(exchange, market_id)` 列表（供验收核对，不联网）。"""
    out = []
    try:
        names = sorted(os.listdir(SNAP_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name == INDEX_NAME:
            continue
        stem = name[:-5]
        if "__" not in stem:
            continue
        ex, mid = stem.split("__", 1)
        out.append((ex, mid))
    return out


def freshness(payload, now_ms=None, stale_after_s=None):
    """按**当前时刻**重算新鲜度。返回 `{"status", "age_seconds", "note"}`。

    · `age_seconds` = 现在 − **快照写入时点**（不是交易所指标的事件时点）；
    · 交易所指标自身的时点在各字段的 `event_time` / `usd_event_time`，一并写进 note，
      避免把「快照何时写」误读成「指标何时发生」。
    · `status`：无写入时点 → `unknown`；`age <= 阈值` → `ok`；否则 → `stale`（过期）。
    """
    now = int(now_ms if now_ms is not None else _now_ms())
    limit = int(stale_after_s if stale_after_s is not None else DEFAULT_STALE_AFTER_S)
    ts = payload.get("snapshot_refreshed_at_ms") if isinstance(payload, dict) else None
    if ts is None:
        return {"status": "unknown", "age_seconds": None,
                "note": "快照缺写入时点，无法判定新鲜度"}
    age = max(0.0, round((now - int(ts)) / 1000.0, 1))
    status = "ok" if age <= limit else "stale"
    # 指标自身时点（真实「数据时点」）：持仓量多空比 / OI 名义额
    ls = ((payload.get("long_short") or {}).get("top_position_long_short_ratio") or {})
    oi = payload.get("open_interest") or {}
    bits = []
    if ls.get("event_time"):
        bits.append(f"持仓量多空比数据时点 {ls['event_time']}")
    if oi.get("usd_event_time"):
        bits.append(f"总持仓名义额数据时点 {oi['usd_event_time']}")
    point = ("；" + "；".join(bits)) if bits else ""
    if status == "ok":
        note = (f"快照 {iso(ts)} 写入（{age:.0f}s 前，阈值 {limit}s）{point}")
    else:
        note = (f"**快照已过期**：{iso(ts)} 写入（{age:.0f}s 前，阈值 {limit}s）"
                f"{point}；采集器未按周期刷新到该标的，数值仅供参照")
    return {"status": status, "age_seconds": age, "note": note}


def write_index(stats):
    """写刷新索引（最近一轮统计），供负载/新鲜度验收核对。"""
    body = dict(stats or {})
    body.setdefault("updated_at_ms", _now_ms())
    body.setdefault("updated_at", iso(body["updated_at_ms"]))
    with _INDEX_LOCK:
        _atomic_write(SNAP_DIR / INDEX_NAME, body)
    return body


def read_index():
    """读刷新索引；缺失返回 {}。"""
    try:
        with open(SNAP_DIR / INDEX_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


if __name__ == "__main__":
    import sys
    idx = read_index()
    syms = list_symbols()
    print(json.dumps({"dir": str(SNAP_DIR), "symbols": len(syms), "index": idx},
                     ensure_ascii=False, indent=1))
    if len(sys.argv) > 2:
        ex, mid = sys.argv[1], sys.argv[2]
        payload = read(ex, mid)
        print(json.dumps({"payload": payload, "freshness": (freshness(payload)
                                                           if payload else None)},
                         ensure_ascii=False, indent=1))
