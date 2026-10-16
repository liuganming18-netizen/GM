# WORK-04B 阶段 4 & 5：旧策略与旧执行资产物理删除清单 (Deletion Manifest)

- **记录时间**：2026-10-16
- **删除总文件数**：13

| 文件路径 | 文件大小 (Bytes) | SHA-256 校验和 | 状态与原因 |
|:---|:---:|:---|:---|
| `策略/WyckoffMaster/core.py` | 29489 | `8728051762d21c065c88b9ba...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/execution.py` | 58863 | `405e2d8e5a57977d4f5acdfc...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/params.py` | 5759 | `7406f767b8c9b52afa2e1fff...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/factors.py` | 6483 | `ae81838357f3af2a4eeb48b9...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/segments.py` | 4216 | `1e1cbb1409a8a328856728d5...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/series.py` | 3902 | `2adec648634bc90c07604545...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/test_execution.py` | 31405 | `03e61f501cf764f943e7c75b...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/verify.py` | 33477 | `b72ae76ced516f8c00eee6c2...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `策略/WyckoffMaster/experiments.py` | 5662 | `5b44e0eda8a69836bba65947...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `框架/backend/strategies/wx_donchian_wyckoff.py` | 30780 | `d6c8812da00e7f4ad22cfcf9...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `框架/backend/strategies/strategy_a.py` | 6882 | `97f6aa562793eda633a0c73f...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `框架/backend/strategies/strategy_b.py` | 7224 | `37b6426c3d0598505d0c2bbd...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |
| `框架/backend/exec_bridge.py` | 6113 | `739ed563782a6afdfce52fd8...` | WORK-04B 阶段4: 旧策略已退役收敛物理删除 |

**待删除文件总大小**：224.86 KB (230255 字节)

### 受保护资产确认（严禁删除）：
- `策略/WyckoffMaster/_legacy_archive_v31/`：不可篡改历史封存 ✅
- `docs/evidence/`：历史审计报告与证据链 ✅
- `策略/WyckoffMaster/trading_core/`：Shared Trading Core 唯一共享交易核心 ✅
- `策略/WyckoffMaster/oi_resonance_v1/`：新策略唯一实现 ✅
- `PRE_DELETE_GOLDEN (bt_20261016T094027Z_1cd723e2)`：基准证据 ✅
---

## 二、旧策略回测产物清理清单 (阶段 5)

- **目标目录**：
- **对应策略族**：旧 WyckoffMaster v3.1 / 阶梯对比 M0~M4
- **清理文件数**：130 个文件 (含 ledgers, state, adapters, engine 等)
- **清理总大小**：255.74 MB (268160452 字节)
- **清理原因**：旧策略回测产物已彻底退役，不参与新系统任何指标计算与 UI 展示。
- **新策略回测产物状态**：新  完好保留，独立受保护。
