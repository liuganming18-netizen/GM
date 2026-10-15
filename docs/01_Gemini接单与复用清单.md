# 01 · Gemini 接单与复用清单（第一阶段：工程与数据库底座 + 数据迁移准备）

- **接单与落盘时间**：2026-10-14 14:35（北京时间 / GMT+8）
- **实施方**：Gemini（负责新项目底层框架改造、数据库底座构建与可核验存量数据迁移）
- **协调与独立验收方**：Codex（全程只读独立审核，不直接改码）
- **唯一新项目根路径**：`/Users/boos/Desktop/威科夫因子共振趋势/`
- **当前阶段状态**：`ROUND_1_ACCEPTED_AND_AUDITED`（首轮接单清单与源目标迁移表已落盘；跨目录读取权限阻塞已精确登记，待同时挂载源目录后立即执行源码收敛与在线一致迁移）

## 一、接单实查结果（目录现状与前序残留核对）

1. **新项目根目录实查**：
   - `/Users/boos/Desktop/威科夫因子共振趋势/` 已存在且当前账号可写。
   - 根目录已有文件：`施工总纲.md`（8 KB）、`派工_第一阶段_工程与数据库底座.md`（5 KB）。
   - 既有五类顶层目录（均已存在，沿用且不删除重建）：
     - `框架/`（内含前序已停止 Work 任务留下的空目录骨架：`框架/config/`、`框架/scripts/`、`框架/src/wyckoff_framework/api/`，当前 0 个代码文件，直接沿用该包结构）
     - `策略/`（空目录，0 文件）
     - `数据/`（内含空子目录 `数据/contracts/`，0 文件）
     - `回测日志/`（空目录，0 文件）
     - `UI界面/`（空目录，0 文件）
     - 另含 `docs/`、`evidence/`（接单前均为空目录）。

2. **本窗口工具与权限通道实查（如实报告，不伪造运行态）**：
   - **目标目录读写通道（`client:list_dir` / `client:view_file` / `client:write_to_file`）**：对 `/Users/boos/Desktop/威科夫因子共振趋势/` **100% 正常可用**。
   - **源目录读取通道阻塞（P0 权限阻塞，需用户在 UI 补勾）**：
     - 实测读取 `/Users/boos/WorkBuddy AI/加密货币趋势判断系统/docs` 返回：`Cannot read directory: /bin/ls failed with exit code 1: ls: /Users/boos/WorkBuddy AI/加密货币趋势判断系统/docs: Operation not permitted`。
     - **原因**：用户在 `Files & folders`（Connected Folders）中选择连接 `/Users/boos/Desktop` 时，原先连接的 `/Users/boos/WorkBuddy AI` 被取消勾选（当前沙箱授权列表仅剩 `[/Users/boos/Desktop]`）。
     - **解除方式**：请在 `Files & folders` 中**同时勾选** `/Users/boos/Desktop` 与 `/Users/boos/WorkBuddy AI`（两个目录并存），即可在同一会话内从旧工程只读提取源码与在线备份数据并写入桌面新工程。
   - **前序已采集的旧工程精确快照证据（12:15–13:35 会话期间已实读核验）**：
     - 在 `/Users/boos/WorkBuddy AI` 授权生效期间，本窗口已实读并核对了 `market_repository.py`、`回测资源改造_2026-10-14`、`模拟实盘生产接通_2026-10-14`、`热库抢救_2026-10-14`、`K线OI资金费率全量下载_2026-10-14`、`Git代码基线保护_2026-10-14` 及 `加密货币趋势判断系统/{data,data_hub,ops,backups,docs}` 的真实文件清单与 SHA。

## 二、四方写锁与边界确认（严格遵守，零越界）

| 角色 / 作用域 | 路径范围 | 本任务（Gemini）权限 | 遵守承诺 |
|---|---|---|---|
| **新项目工程与数据底座** | `/Users/boos/Desktop/威科夫因子共振趋势/**` | **读写（唯一主写锁）** | 沿用 `框架/策略/数据/回测日志/UI界面/docs/evidence` 结构，不删既有目录 |
| **旧永久数据根与运维包** | `/Users/boos/WorkBuddy AI/加密货币趋势判断系统/{data,data_hub,ops,backups,docs}` | **纯只读（`mode=ro`）** | 不修改原热库、不改 `ops` 定时配置、不解除 `rescued/` 或分片的不可变保护 |
| **旧后端与恢复/下载任务** | `/Users/boos/WorkBuddy AI/2026-10-03-16-11-14/outputs/网站逆向研究/**` | **纯只读** | Work 原恢复、数据保护、Git 候选保留原写锁；不重启已取消的下载，不另开冷档重建，不重启健康 `realtime_collector` / `adapter(:8787)` / `live_worker` |
| **前端工作树** | `.../网站逆向研究/前端实现-AIStudio/**` | **纯只读** | 保留 Claude 写锁，不改 UI 布局、不改旧前端 Git 索引、不向 `GM.git` 的 `main` 分支强推 |

## 三、模块复用与技术取舍清单

| 层级 | 选用/复用源模块 | 复用与改造方案 | 明确取舍与纠正项 |
|---|---|---|---|
| **1. 配置、注册表、任务状态与 CLI** | `全工程统一管理_隔离施工` 下的 `unified/{config,registry,taskstore,runner,cli}` + `加密货币趋势判断系统/ops/keepalive_check.py` | 收敛至 `框架/src/wyckoff_framework/{config,registry,taskstore,runner,cli}.py`；CLI 提供配置/环境状态、数据库库存、覆盖/缺口状态、真实进程状态查询 | **纠正 3 处过时设计**：① 彻底清零 `.codex/worktrees/90b1` 与 `c9d7` 等 50+ 处硬编码路径；② 废除旧版“拒绝 Parquet / 拒绝 15m”限制，纳入 `1d→15m` 7 周期契约与 `5m` K线历史冻结（`FROZEN_HISTORY_TFS=("5m",)`，OI 原生 5m 不冻结）；③ 任务状态必须校验真实 PID + FD + 水位增长，进程消失立即标 `failed`/`blocked`，绝不假标 `running` |
| **2. 热库 Schema 与只读连接守卫** | `加密货币趋势判断系统/data_hub/{schema.sql,store.py,gateway.py}` | 收敛至 `框架/src/wyckoff_framework/db/` 与 `数据/contracts/`；所有查询与研究连接强制 `file:...?mode=ro` + `PRAGMA query_only=ON` | 固定真实 26 表 + 1 视图（`candles` 视图 = `candle_versions JOIN candle_heads`）Schema；运行环境 SQLite 若低于 3.51.3（或未含 WAL-reset 修复回补 3.50.7），生产写者入口保持硬禁用，第一阶段仅开放只读查询与在线备份核验 |
| **3. 冷热混合仓储（看板查询层）** | `后端实现/market_repository.py` | 收敛至 `框架/src/wyckoff_framework/repository/market_repository.py`；点查优先 SQLite 热层（保证每周期 `latest200`），冷层走 DuckDB over Parquet（ZSTD） | **改造冷层分片发现**：将原本地 `Path.is_dir()` + `d.glob("*.parquet")` 升级为受管理的 `_catalog.json` / Manifest 解析 + 物理文件存在性核验；缺失分片如实返回 `missing` 状态，绝不靠旧 catalog 假报完整，也绝不用 0 或空串冒充 |
| **4. 四臂回测输入与主执行引擎** | `后端实现/strategies/{wx_execution,wx_donchian_wyckoff,wx_config}.py` + `回测资源改造_2026-10-14/src/{ra_store,ra_input,ra_engine,ra_arm,ra_equiv}.py` + `后端实现/paper_engine.py` | 完整迁入 `策略/`，不遗漏 `ra_*` 生成器与核验器；回测层继续使用 PyArrow 列式分块（8192 行/块、320 MiB 解码预算、单次构建 `InputStore` 供 3a/3b/3c/3d 顺序复用） | **不把回测迁回 Freqtrade，也不在回测热循环套 DuckDB**；Freqtrade 2026.9 仅作环境与外部对照；保留 1h 信号、15m 执行、15m 成交额 1% 参与率上限、同刻 `base` 优先合卡、`order_request_ms` 真实首次请求墙钟锚（防请求前旧价回填 `entry_ts < requested_at`）及 `E_missing = E_truth \ E_charged` 迟到资金费差集补记（`late_backfill=1`） |
| **5. 隔离只读 API 服务（端口 18787）** | FastAPI + Uvicorn + Pydantic（复用 `data_hub/http_api.py` 与 `后端实现/{public_market_read,live_api}.py` 只读契约） | 收敛至 `框架/src/wyckoff_framework/api/`，绑定独立端口 `18787`（避开生产 `8787` / `8791` 与前端 `3000`） | 严格审查 `import` 副作用：启动 `18787` API 绝不触发采集、绝不写行情库、绝不启动模拟盘撮合；通过 FastAPI lifespan 管理只读连接池与并发信号量背压 |
| **6. 在线备份与存量数据迁移** | `加密货币趋势判断系统/ops/{hot_snapshot,protect_immutable,version_manifest}.py` + SQLite `backup()` API | 活动 SQLite（`market_v2.sqlite3` 及 `paper_3a~3d.sqlite3`）通过官方在线备份 API 迁入 `/Users/boos/Desktop/威科夫因子共振趋势/数据/`（或复用 `backups/hot/20261014T131200Z` 已校验一致快照做二次对账复制）；冷档 Parquet 与原始 ZIP 增量校验复制 | 严守磁盘剩余空间 `>= 12 GiB` 红线；分批迁移、逐批校验文件数/字节/SHA256/`PRAGMA quick_check`/行数/最大时间戳水位；原件只读保留，不删、不覆盖 |

## 四、环境与依赖实查基线（对照总纲）

- **已核实 Python 环境现状**：
  1. `default` 环境：Python 3.13、SQLite `3.50.4`、DuckDB `1.5.6`、PyArrow `26.0.0`、websockets `17.2`（缺 FastAPI / Uvicorn / Pydantic）。
  2. `freqtrade` 环境（`/Users/boos/.workbuddy-ai/binaries/python/envs/freqtrade`）：Python `3.13.12`、Freqtrade `2026.9`、SQLite `3.50.4`、FastAPI `0.143.0`、Uvicorn `0.54.0`、Pydantic `2.14.0`、PyArrow `26.0.0`、CCXT `4.5.85`（缺 DuckDB）。
  3. 系统 `/usr/bin/python3`：SQLite `3.54.0`（在 `热库抢救_2026-10-14/verify_rescued_db.py` 中已实测可用，满足 `>= 3.51.3` 的 WAL-reset 修复版本要求）。
- **本工程环境策略**：
  - 在 `框架/pyproject.toml` 与依赖锁文件中显式声明并锁定版本约束，不污染或覆盖现有 `default` 与 `freqtrade` 活动环境；
  - 记录运行解释器的真实 `sqlite3.sqlite_version`，凡 `sqlite_version < 3.51.3`（且非 3.50.7+ 回补版）的解释器在代码层硬性锁死 `read_only=True`，禁止开启生产写者。

## 五、重复故障计数与防御登记

| 历史/当前故障项 | 累计出现次数 | 根因状态 | 本阶段防御措施 |
|---|---:|---|---|
| 1. `.codex/worktrees` 路径硬编码导致清理后断流 | **2 次**（10-14 09:03 删库 + 50+ 处源码残留） | 已查明（客户端 worktree 自动清理 + 代码硬编码） | 新框架 `config.py` 启动即扫描路径，含 `.codex/worktrees` 直接抛异常拒绝启动 |
| 2. 跨目录 Connected Folders 切换导致单边失权 | **1 次**（本轮切 `/Users/boos/Desktop` 后 `/Users/boos/WorkBuddy AI` 返回 EPERM） | 已查明（UI 文件夹授权只选了单个目录） | 如实报告阻塞，不盲目全盘乱搜，待双目录同时勾选后执行跨目录复制 |
| 3. 旧恢复窗口 Bash `true` 返回 exit 137 | **未知**（继承自前序窗口记录） | **原因未知（保持未知）** | 优先使用文件专用工具（`view_file` / `write_to_file` / `list_dir`），任何后台进程统一走 `spawn_detached.py`，不用 `nohup` |
| 4. Git 基线遗漏四臂主引擎 `ra_*` | **1 次**（`Git代码基线保护_2026-10-14/evidence/11_补充核查_四臂主引擎遗漏.md` 已记录） | 已查明（白名单仅拷了 `后端实现/`，漏拷 `回测资源改造_2026-10-14/src/ra_*.py`） | 本项目 `docs/模块迁移表.md` 已将 `ra_store/ra_input/ra_engine/ra_arm/ra_equiv` 全量纳入 `策略/` 必迁清单 |
| 5. 原件（ZIP）与正式入库数据混报为“已恢复” | **1 次** | 已查明 | 在库存 CLI 与 `18787` 接口中将 `raw_zip`、`parquet_cold`、`sqlite_hot` 三层分开统计并标注缺口，不混为一谈 |

## 六、第一阶段推进时间表与下一步动作

1. **已完成（本轮交付）**：
   - `docs/01_Gemini接单与复用清单.md`（本文件）
   - `docs/模块迁移表.md`（含代码模块旧→新映射表 + 数据源→目标分批迁移与容量核算表）
   - `数据/contracts/frozen_data_contract.json`（冻结 549 池、7+1 周期契约、四类时间戳语义、六态缺失枚举与表结构契约）
   - `框架/pyproject.toml` 与基础包骨架初始化（零外部源依赖即可先落盘的部分）
2. **待用户一键解除的权限阻塞（耗时 < 1 分钟）**：
   - 请在左侧 **Files & folders（Connected Folders）** 中，保留 `/Users/boos/Desktop` 的同时，**把 `/Users/boos/WorkBuddy AI` 也勾上**。
3. **双目录连通后立即完成（预计 15–25 分钟）**：
   - 提取并重构 `unified`、`data_hub`、`market_repository`、`wx_*` + `ra_*` + `paper_engine` 源码落入 `框架/` 与 `策略/`（彻底清零 worktree 硬编码）；
   - 将已校验一致的 `market_v2.sqlite3`（4.32 GiB）、`paper_3a~3d.sqlite3`、Parquet 冷档分片及可核验原件按批次迁入 `/Users/boos/Desktop/威科夫因子共振趋势/数据/`，产出逐批 SHA256 与水位对账单；
   - 启动 `18787` 只读 FastAPI 服务与统一 CLI，采集真实 PID、日志与接口返回证据，交付 `docs/02_Gemini第一阶段实施回执.md`（状态 `READY_FOR_CODEX_ACCEPTANCE`）。
