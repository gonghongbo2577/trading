# Phase 1: 数据基础层 — 详细 TODO 列表

> **来源**: `docs/tech-plan.md` v2.0 §三、§四§4.1-4.2、§五§5.7-5.8、§六、§七、§十
> **交叉验证**: `docs/strategy-plan-v2.md` §六 + `docs/algorithm-workflow.md` §二 + `CLAUDE.md`
> **日期**: 2026-06-04
> **总预估工时**: ~12.5 h 编码 + ~35 min 运行 = ~13 h

---

## 前置条件（Phase 1 启动前确认）

- [ ] Python 3.12 已安装并可创建 venv
- [ ] Tushare Pro token 已获取（2120 积分），在 https://tushare.pro 验证可用
- [ ] 网络可访问 Tushare API 和 BaoStock API
- [ ] `pip install` 可正常拉取依赖（或已配置镜像源）

---

## TODO-01: 项目骨架搭建

| 字段 | 内容 |
|------|------|
| **标题** | 创建项目目录结构、依赖清单、Git 配置 |
| **所属范围** | Phase 1 / Week 1 / 基础设施 |
| **任务目标** | 建立完整的项目目录树、`requirements.txt`、`.gitignore`、`.env`、`README.md`，确保项目可通过 `pip install -r requirements.txt && pytest` 启动 |
| **业务意图** | 为所有后续模块提供可预测的文件路径、统一的依赖版本、安全的 Git 提交边界。没有这一步，后续任何代码都无法运行或被版本管理正确排除 |
| **前置条件与依赖** | 无外部依赖。Python 3.12 已安装 |
| **变更边界** | **创建**: 所有目录（见下方清单）、`requirements.txt`、`.gitignore`、`.env`（空模板）、`README.md`、`core/__init__.py`。**不创建**: 任何 `.py` 业务逻辑、`config/settings.toml`（由 TODO-02 负责）、`data/` 下的实际数据文件（由下载任务负责） |
| **实现要求** | 1. 目录结构按 `tech-plan.md §2.3` 完整创建（含空 `data/raw/` 5 个子目录 + `data/processed/factors/`）<br>2. `requirements.txt` 按 `tech-plan.md §九` 完整清单（tushare>=1.4.26, baostock>=0.8.0, polars>=1.0.0, duckdb>=1.1.0, pyarrow>=15.0.0, structlog>=24.0.0, numpy>=1.26.0, scipy>=1.13.0, pypbo>=0.4.0, matplotlib>=3.8.0, seaborn>=0.13.0, tabulate>=0.9.0, python-dotenv>=1.0.0, ipykernel>=6.29.0, jupyter>=1.0.0, pytest>=8.0.0, pytest-cov>=5.0.0）<br>3. `.gitignore` 按 `tech-plan.md §10.2` 模板，包含 `__pycache__/`, `*.py[cod]`, `*.egg-info/`, `.venv/`, `.env`, `config/settings.toml`, `data/`, `.vscode/`, `.idea/`, `.ipynb_checkpoints/`, `logs/`, `output/reports/`<br>4. `.env` 仅含一行注释 `# TUSHARE_TOKEN=your_token_here`<br>5. `core/__init__.py` 为空文件 |
| **验收标准** | ① 目录树与 tech-plan.md §2.3 完全一致（`find . -type d | sort` 验证）<br>② `pip install -r requirements.txt` 无错误完成<br>③ `git status` 不显示 `data/`、`.env`、`config/settings.toml`、`__pycache__/` |
| **验证方法** | `ls -la` 逐目录检查；`pip install -r requirements.txt --dry-run`（或实际安装）；`git status` 确认 gitignore 生效；`python -c "import polars; import duckdb; import structlog; print('OK')"` |
| **Definition of Done** | ☐ 目录树完整 ☐ requirements.txt 可安装 ☐ .gitignore 生效 ☐ core/__init__.py 存在 ☐ README.md 含项目简介 + 快速启动命令 |
| **执行记录** | （待执行后填写：执行日期、实际耗时、偏差说明） |

---

## TODO-02: 配置系统实现

| 字段 | 内容 |
|------|------|
| **标题** | 创建 settings.template.toml、settings.toml，实现 config.py 配置加载器 |
| **所属范围** | Phase 1 / Week 1 / 基础设施 |
| **任务目标** | 实现类型安全的 TOML 配置加载，包含 v2.0 全部参数（DataConfig, FactorConfig, BacktestConfig, RiskConfig），支持默认值合并和缺失字段报错 |
| **业务意图** | 所有模块通过单一 `Config` 对象获取参数，避免硬编码。TOML 格式人类可读写，`settings.template.toml` 可安全提交 Git（不含 token），`settings.toml` 由用户本地维护 |
| **前置条件与依赖** | TODO-01 完成（`core/__init__.py` 存在、目录结构就绪） |
| **变更边界** | **创建**: `config/settings.template.toml`、`config/settings.toml`、`core/config.py`。**不涉及**: 任何网络 I/O、数据库读写、其他模块的配置消费 |
| **实现要求** | 参见 `tech-plan.md §4.1` 和 `§4.1.1`：<br>1. `settings.template.toml` 含全部 4 段（`[data]`, `[factor]`, `[backtest]`, `[risk]`），每个参数有注释说明和默认值。Token 字段填 `"YOUR_TOKEN_HERE"`<br>2. `settings.toml` 从模板复制，用户填入真实 token<br>3. `config.py` 定义 5 个 dataclass：`DataConfig`, `FactorConfig`, `BacktestConfig`, `RiskConfig`, `Config`（顶层聚合）<br>4. `Config.from_toml(path)` 类方法：用 `tomllib.load()` 读取 → 各段缺失时使用对应 dataclass 的默认值 → 返回 `Config` 实例<br>5. 所有默认值与 `tech-plan.md §4.1` 精确一致：`DataConfig.start_date="20150101"`, `FactorConfig.sigma_target=0.15`, `FactorConfig.vmp_upper_bound=2.0`, `RiskConfig.ma_period=20`, `RiskConfig.hard_drawdown_limit=-0.15` 等<br>6. `tushare_token` 为必填，缺失时抛出 `ValueError("tushare_token is required")` |
| **验收标准** | ① `Config.from_toml("config/settings.toml")` 返回完整 Config 对象<br>② 缺失 `[factor]` 段时 FactorConfig 所有字段使用默认值<br>③ 缺失 `tushare_token` 时抛出 ValueError<br>④ `settings.template.toml` 可被 `git add` 不泄露 token |
| **验证方法** | `python -c "from core.config import Config; c = Config.from_toml('config/settings.toml'); print(c)"` — 验证所有字段非 None<br>`grep -c "YOUR_TOKEN_HERE" config/settings.template.toml` → 输出 1<br>`grep -c "YOUR_TOKEN_HERE" config/settings.toml` → 输出 0（用户已替换） |
| **Definition of Done** | ☐ settings.template.toml 含完整 4 段配置 ☐ settings.toml 存在且 token 已替换 ☐ config.py 含 5 个 dataclass + from_toml() ☐ 缺失段使用默认值 ☐ 缺失 token 抛异常 |
| **执行记录** | （待执行后填写） |

---

## TODO-03: 日志系统实现

| 字段 | 内容 |
|------|------|
| **标题** | 基于 structlog 实现双输出日志系统（JSON 文件 + 人类可读控制台） |
| **所属范围** | Phase 1 / Week 2 / 基础设施 |
| **任务目标** | 配置 structlog，输出结构化 JSON 日志到 `logs/` 目录（含日志轮转），同时输出彩色人类可读格式到控制台。所有后续模块统一使用 `logger = structlog.get_logger()` |
| **业务意图** | 数据下载过程可能长达 20 分钟且涉及网络重试，结构化日志是追踪进度、排查 API 异常、审计数据质量的唯一手段。JSON 格式支持后续 `jq` 查询 |
| **前置条件与依赖** | TODO-01 完成（`logs/` 目录存在、structlog 已安装） |
| **变更边界** | **创建**: `core/logger.py`（日志配置函数，供其他模块 `import`）。**不涉及**: 在其他模块中添加日志调用（由各 TODO 自行处理） |
| **实现要求** | 1. `core/logger.py` 暴露 `setup_logging(level="INFO")` 函数<br>2. JSON 输出：`structlog.processors.JSONRenderer()` → `logging.FileHandler("logs/app.log")` + `RotatingFileHandler`（10MB×3）<br>3. 控制台输出：`structlog.dev.ConsoleRenderer(colors=True)`<br>4. 绑定全局上下文：`structlog.configure(wrapper_class=structlog.BoundLogger)`<br>5. 所有模块通过 `logger = structlog.get_logger()` 获取，不直接使用 `logging` |
| **验收标准** | ① `python -c "from core.logger import setup_logging; setup_logging(); import structlog; structlog.get_logger().info('test')"` 同时在控制台和 `logs/app.log` 输出<br>② 日志文件为合法 JSON（每行一条 JSON 记录）<br>③ 日志轮转生效（文件 > 10MB 时自动轮转） |
| **验证方法** | 运行测试后 `cat logs/app.log \| jq .` 验证 JSON 结构；`ls -la logs/` 验证文件生成 |
| **Definition of Done** | ☐ core/logger.py 存在 ☐ setup_logging() 可调用 ☐ 控制台输出彩色日志 ☐ 文件输出 JSON 格式 ☐ 日志轮转配置生效 |
| **执行记录** | （待执行后填写） |

---

## TODO-04: DataLoader 基础框架 + 交易日历下载

| 字段 | 内容 |
|------|------|
| **标题** | 实现 DataLoader 类骨架、Tushare 连接验证、交易日历下载 |
| **所属范围** | Phase 1 / Week 1 / 数据下载核心 |
| **任务目标** | 创建 `core/data_loader.py` 的 `DataLoader` 类框架（含构造函数和所有空方法签名），验证 Tushare token 可用，下载并缓存全量交易日历 |
| **业务意图** | DataLoader 是 Phase 1 的核心交付物（所有后续模块依赖它加载数据）。交易日历是所有日期驱动操作的基础——下载日线需要知道哪些日期是交易日，调仓日检测需要每月第一个交易日。必须先于任何日线下载 |
| **前置条件与依赖** | TODO-02 完成（`DataConfig` 可用、token 已配置） |
| **变更边界** | **实现**: 构造函数（`ts.pro_api()` 初始化）、`download_trade_cal()`（从 Tushare `trade_cal` API 下载 SSE 交易日历，缓存为 `data/raw/trade_cal.parquet`）、所有方法空签名。**不实现**: 方法体（留给 TODO-05~08） |
| **实现要求** | 1. `DataLoader.__init__(self, config: DataConfig)`: 存储 config，调用 `ts.pro_api(config.tushare_token)`，若连接失败抛 `ConnectionError(f"Tushare token 验证失败: {e}")`<br>2. `download_trade_cal(self)` 方法：调用 `self.pro.trade_cal(exchange='SSE', start_date=config.start_date, end_date=config.end_date)` → 过滤 `is_open=1` → 存 `data/raw/trade_cal.parquet`<br>3. 接口清单（仅签名，`...` 占位）见 `tech-plan.md §4.2`：共 13 个方法（含 v2.0 新增的 `download_pledge_stat`, `download_baostock_daily`, `validate_cross_source`）<br>4. 保留 `data/raw/trade_cal.parquet` 的加载方法 `load_trade_cal() -> pl.DataFrame` |
| **验收标准** | ① `DataLoader(config)` 初始化成功（无异常）<br>② `loader.download_trade_cal()` 返回 DataFrame，包含 `cal_date`, `is_open`, `pretrade_date` 列<br>③ `is_open=1` 的记录数 ≈ 2450（2015-2025 约 10 年交易日）<br>④ `data/raw/trade_cal.parquet` 文件存在 |
| **验证方法** | `python -c "from core.config import Config; from core.data_loader import DataLoader; c = Config.from_toml('config/settings.toml'); dl = DataLoader(c.data); df = dl.download_trade_cal(); print(df.describe()); print(f'Trading days: {len(df)}')"` → 输出约 2450 |
| **Definition of Done** | ☐ DataLoader 类存在且构造函数验证 token ☐ download_trade_cal() 可运行 ☐ trade_cal.parquet 缓存存在 ☐ 13 个方法签名完整（占位符） ☐ load_trade_cal() 可从缓存加载 |
| **执行记录** | （待执行后填写） |

---

## TODO-05: Tushare 核心数据下载

| 字段 | 内容 |
|------|------|
| **标题** | 实现股票列表、日线行情、估值指标、财务指标的全量下载 |
| **所属范围** | Phase 1 / Week 1 / 数据下载核心 |
| **任务目标** | 实现 4 个下载方法：`download_stock_list()`、`download_daily_batch()`、`download_daily_basic()`、`download_fina_indicator()`。运行首次全量数据下载（2015-01-01 至 2025-12-31） |
| **业务意图** | 这是策略的原始数据基础。日线行情是因子计算（动量/低波/残差动量）和回测模拟的唯一价格来源。估值指标提供 PE/PB/总市值（BP+/EP/Size 因子的输入）。财务指标提供 ROE/商誉/审计意见（ROE 因子 + Universe 过滤的输入）。**必须包含退市股**（`list_status='D'`），否则回测存在生存者偏差 |
| **前置条件与依赖** | TODO-04 完成（DataLoader 骨架、交易日历可用） |
| **变更边界** | **实现**: `download_stock_list()`, `download_daily_batch()`, `download_daily_basic()`, `download_fina_indicator()` 4 个完整方法。**不涉及**: SW 行业分类、中证 500 指数、质押数据、BaoStock（留给 TODO-06/07） |
| **实现要求** | 1. **股票列表** (`tech-plan.md §3.2 步骤2`): 合并 `list_status='L'` + `'D'` + `'P'` → Polars DataFrame，列 `ts_code, name, market, list_date, delist_date, list_status`。按 `ts_code` 去重<br>2. **日线批量下载** (`tech-plan.md §3.2 步骤3`): 遍历交易日历的每个交易日 → `pro.daily(trade_date=date)` → 存为 `data/raw/tushare_daily/date=YYYYMMDD/data.parquet`（Hive 分区）。**速率控制**: `time.sleep(0.35)`，每 200 次调用后额外暂停 5s。**错误恢复**: `ConnectionError/Timeout` 指数退避重试（1s→2s→4s→8s，最多 4 次）→ 全部失败则记录失败日期 → 继续下一个日期。`RateLimitError` → `sleep(60)` 后重试，连续 3 次触发速率限制则暂停 10 分钟后继续。Schema: `ts_code(str), trade_date(date), open/high/low/close/pre_close/change/pct_chg(float32), vol/amount(float64)`<br>3. **估值指标** (`tech-plan.md §3.2 步骤4`): `pro.daily_basic(trade_date=date)` → `data/raw/tushare_basic/date=YYYYMMDD/data.parquet`。速率控制同日线<br>4. **财务指标** (`tech-plan.md §3.2 步骤5`): `pro.fina_indicator(end_date=quarter_end)` 按季度批量 → `data/raw/tushare_fina/end_date=YYYYQQ/data.parquet`。**关键**: 必须保留 `ann_date` 列（用于 PIT 过滤）、以及 v2.0 新字段 `goodwill`, `total_equity`, `audit_opinion`<br>5. **首次运行**: 任务完成后运行 `python -m core.data_loader --download-all`，下载 2015-2025 全量数据<br>6. **进度汇报**: 每 100 个交易日 log 一次进度 `"已下载 {n}/{total} 个交易日"` |
| **验收标准** | ① 股票列表包含三种状态（L/D/P），总数 ≥ 5000<br>② `data/raw/tushare_daily/` 下约 2450 个分区目录，每个含 `data.parquet`<br>③ 日线 schema 与 `tech-plan.md §3.3` 一致（11 列，不复权原始价）<br>④ `fina_indicator` 含 `ann_date`, `goodwill`, `total_equity`, `audit_opinion` 列<br>⑤ 全量下载无未处理异常（允许少量失败日期被记录跳过）<br>⑥ 退市股的数据在退市日期后不存在 |
| **验证方法** | `python -c "from core.data_loader import DataLoader; dl = DataLoader(c.data); df = dl.download_stock_list(); print(df['list_status'].value_counts())"` → L/D/P 三状态非零<br>`ls data/raw/tushare_daily/ | wc -l` → 约 2450<br>`python -c "import polars as pl; df = pl.read_parquet('data/raw/tushare_daily/date=2024-06-28/data.parquet'); print(df.columns)"` → 11 列 |
| **Definition of Done** | ☐ download_stock_list() 返回 L+D+P ☐ download_daily_batch() 按日分区存储 ☐ download_daily_basic() 完成 ☐ download_fina_indicator() 含 v2.0 字段 ☐ 全量下载成功运行 ☐ 失败日期被记录（非静默跳过） |
| **执行记录** | （待执行后填写） |

---

## TODO-06: Tushare 辅助数据下载

| 字段 | 内容 |
|------|------|
| **标题** | 实现申万行业分类、中证 500 指数行情、股权质押数据的下载 |
| **所属范围** | Phase 1 / Week 1 / 数据下载 v2.0 扩展 |
| **任务目标** | 实现 3 个下载方法：`download_sw_classification()`、`download_index_daily()`、`download_pledge_stat()` |
| **业务意图** | 申万行业分类用于：① 行业感知去极值（Phase 2 因子计算）② 行业集中度检查（Phase 3 组合构建）③ 残差动量截面回归（Phase 2）。中证 500 指数行情用于：① 业绩基准 ② MA20 择时信号（Phase 3 风控）。股权质押数据用于 Universe 第⑩层过滤——质押爆仓是 A 股闪崩的主要触发因素 |
| **前置条件与依赖** | TODO-04 完成（DataLoader 骨架、交易日历）、TODO-05 完成（股票列表可用，`download_pledge_stat` 需要股票代码列表） |
| **变更边界** | **实现**: `download_sw_classification()`, `download_index_daily()`, `download_pledge_stat()`。**不涉及**: BaoStock 下载（TODO-07）、数据校验使用这些数据（TODO-09） |
| **实现要求** | 1. **申万行业** (`tech-plan.md §3.2 步骤6`): `pro.index_classify(level='L1', src='SW2021')` → 获取行业列表 → `pro.index_member_all(index_code=...)` 逐行业获取成分股 → 返回 `pl.DataFrame` 列 `ts_code, l1_name`。存储 `data/raw/sw_classification.parquet`<br>2. **中证 500** (`tech-plan.md §3.2 步骤7`): `pro.index_daily(ts_code='000905.SH', start_date=..., end_date=...)` → 存储 `data/raw/csi500_daily.parquet`。列含 `trade_date, close, pct_chg`<br>3. **股权质押** (`tech-plan.md §3.2 步骤8`): `pro.pledge_stat(ts_code=code)` 逐只调用（与 `pro_bar` 类似，单次返回单只股票） → 合并 → 存储 `data/raw/tushare_pledge/pledge.parquet`。列 `ts_code, pledge_ratio, ctrl_pledge_ratio`。覆盖 ≥ 4000 只。速率控制同 TODO-05 |
| **验收标准** | ① `sw_classification.parquet` 含 31 个申万一级行业，覆盖 ≥ 4500 只股票<br>② `csi500_daily.parquet` 覆盖 2015-2025 全区间<br>③ `pledge.parquet` 覆盖 ≥ 4000 只股票 |
| **验证方法** | `python -c "import polars as pl; df = pl.read_parquet('data/raw/sw_classification.parquet'); print(df['l1_name'].n_unique())"` → ≥ 31<br>`python -c "import polars as pl; df = pl.read_parquet('data/raw/tushare_pledge/pledge.parquet'); print(len(df))"` → ≥ 4000 |
| **Definition of Done** | ☐ SW 分类含 31 行业 ☐ CSI 500 行情完整 ☐ pledge_stat 覆盖 ≥ 4000 |
| **执行记录** | （待执行后填写） |

---

## TODO-07: BaoStock 交叉验证数据下载

| 字段 | 内容 |
|------|------|
| **标题** | 实现 BaoStock 日线下载及 Tushare-BaoStock 收益率交叉验证 |
| **所属范围** | Phase 1 / Week 1 / 数据下载 v2.0 扩展 |
| **任务目标** | 实现 `download_baostock_daily()` 和 `validate_cross_source()` 两个方法。前者从 BaoStock 下载与 Tushare 相同区间的日线数据，后者计算两源日收益的 Pearson 相关系数并判定 |
| **业务意图** | Tushare 作为单一数据源存在断服/数据错误风险。BaoStock 免费且覆盖 1990 年至今，作为交叉验证可检测 Tushare 数据异常（日收益相关 < 0.95 触发警报），Tushare 断服时可应急替代。这是 v2.0 数据质量保障的核心措施 |
| **前置条件与依赖** | TODO-05 完成（Tushare 日线已下载）。启动时需先执行 `bs.login()` 验证 BaoStock 连接可用，连接失败 → 降级为"无交叉验证"模式，不阻断主流程 |
| **变更边界** | **实现**: `download_baostock_daily()`, `validate_cross_source()`。**不涉及**: 用 BaoStock 数据替代 Tushare（仅交叉验证，不用于主策略计算） |
| **实现要求** | 0. **连接验证**: 下载前执行 `bs.login()` 验证 BaoStock 连接，失败则记录 WARNING 并跳过后续下载（不抛异常）<br>1. **BaoStock 下载** (`tech-plan.md §3.2 步骤9` + `§1.3`): `baostock` 库，`bs.login()` → 逐交易日 `bs.query_history_k_data_plus(code, fields='date,code,open,high,low,close,preclose,volume,amount', start_date=date, end_date=date)` → 全市场批量。**字段映射**（BaoStock → 内部标准）: `code`→`ts_code`（加后缀 .SH/.SZ）、`date`→`trade_date`、`preclose`→`pre_close`、`volume`→`vol`。分区存储 `data/raw/baostock_daily/date=YYYYMMDD/data.parquet`<br>2. **速率控制**: BaoStock 免费 API 无限频但有连接超时风险 → 每次调用间隔 0.1s，失败重试 3 次<br>3. **交叉验证** (`tech-plan.md §3.2 步骤9`): 加载同期 Tushare 和 BaoStock 日线 → 计算每只股票日收益 → 计算全市场 Pearson 相关系数 → 返回 `{'correlation': float, 'pass': bool, 'warning_dates': list[str]}`<br>4. **判定标准**: `corr >= 0.99` → pass；`0.95 <= corr < 0.99` → WARNING 日志；`corr < 0.95` → ERROR 日志 + 标记异常日期<br>5. **容错**: BaoStock 连接失败 → 降级为"无交叉验证"模式，记录 WARNING，不阻断主流程。覆盖率 < 80% → WARNING |
| **验收标准** | ① `data/raw/baostock_daily/` 下分区数与 Tushare 匹配度 ≥ 95%<br>② `validate_cross_source()` 返回 `correlation >= 0.99`<br>③ BaoStock 字段映射正确（`preclose`→`pre_close`）<br>④ BaoStock 连接失败时不阻断程序 |
| **验证方法** | `python -c "from core.data_loader import DataLoader; dl = DataLoader(c.data); dl.download_baostock_daily('20150101', '20251231'); result = dl.validate_cross_source('20150101', '20251231'); assert result['correlation'] >= 0.99"` |
| **Definition of Done** | ☐ baostock_daily 分区存在 ☐ 字段映射正确 ☐ validate_cross_source() 返回 corr ≥ 0.99 ☐ 连接失败容错生效 |
| **执行记录** | （待执行后填写） |

---

## TODO-08: 数据加载与复权价格计算

| 字段 | 内容 |
|------|------|
| **标题** | 实现 load_daily() 高效数据加载和复权因子下载+动态复权 |
| **所属范围** | Phase 1 / Week 2 / 数据加载 |
| **任务目标** | 实现 DuckDB/Polars 混合加载的 `load_daily()`（2 秒内加载全区间），实现 `download_adj_factor()` 和动态复权价格计算 |
| **业务意图** | 原始价格为不复权价格（避免被 Tushare 特定复权方式锁定），因子计算和回测需要使用后复权价格。`adj_factor` API 单次最大 3000 行，全市场 ~5000 只需分批调用。`load_daily()` 是后续所有模块的数据入口 |
| **前置条件与依赖** | TODO-05 完成（日线 parquet 数据已存在） |
| **变更边界** | **实现**: `load_daily()`, `download_adj_factor()`, 复权价格计算逻辑。**不涉及**: 因子计算、Universe 构建（Phase 2 消费 load_daily 输出） |
| **实现要求** | 1. **复权因子下载** (`tech-plan.md §3.3`): `pro.adj_factor(ts_code=..., start_date=..., end_date=...)` — 单次返回单只股票，受 3000 行限制 → 按 ts_code 首字母分两批（`'0'-'3'` 和 `'6'-'8'`），每批约 2500 只。存储 `data/raw/adj_factor.parquet`。Schema: `ts_code, trade_date, adj_factor`<br>2. **复权计算** (`tech-plan.md §3.3`): `close_adj = close * adj_factor`（后复权）→ 对 open/high/low 同样处理 → 所有因子计算统一使用后复权价格<br>3. **load_daily()** (`tech-plan.md §3.2` + `§4.2`): DuckDB 读取 parquet 分区 → 自动合并 `adj_factor` → 返回 `pl.DataFrame`。关键列：`ts_code, trade_date, close_adj, open_adj, high_adj, low_adj, vol, amount`<br>4. **性能**: `load_daily('20150101', '20251231')` 必须在 2 秒内完成（利用 DuckDB 谓词下推 + Parquet 列裁剪）<br>5. **adj_factor 校验** (`tech-plan.md §3.4`): 单调递增检查——adj_factor 应随日期递增（除权日有跳变但值是连续的）。单日变化 > 50% → WARNING |
| **验收标准** | ① `load_daily('20150101', '20251231')` 耗时 < 2 秒<br>② 返回的 DataFrame 含 `close_adj` 列且无 NaN<br>③ `adj_factor.parquet` 覆盖全量股票<br>④ 复权后价格 > 原始价格（后复权），且连续无断崖 |
| **验证方法** | `time python -c "from core.data_loader import DataLoader; dl = DataLoader(c.data); df = dl.load_daily('20150101', '20251231'); print(len(df)); print(df.columns)"` → 返回行数 ~ 1200 万（5000 股 × 2450 日）<br>`python -c "df = dl.load_daily('20150101', '20251231'); assert df['close_adj'].is_not_null().all()"` |
| **Definition of Done** | ☐ adj_factor 已下载并存储 ☐ load_daily() < 2s ☐ close_adj 无 NaN ☐ adj_factor 单调递增校验通过 |
| **执行记录** | （待执行后填写） |

---

## TODO-09: 数据质量校验系统

| 字段 | 内容 |
|------|------|
| **标题** | 实现 13 项数据质量校验的 validate() 方法及数据问题自动修复 |
| **所属范围** | Phase 1 / Week 2 / 数据校验 |
| **任务目标** | 实现 `DataLoader.validate()` 方法，覆盖 tech-plan.md §3.4 全部 12 项 + §5.8 的 1 项异常检测（共 13 项）。对校验失败项分类处理（自动修复 / WARNING / ERROR），生成校验报告 |
| **业务意图** | 脏数据是量化策略的第一杀手。重复行导致因子计算重复加权、未来日期导致前视偏差高估业绩、退市股数据越界导致在不可交易股票上建仓。校验系统是数据就绪的最终守门人 |
| **前置条件与依赖** | TODO-05/06/07/08 全部完成（所有数据已下载） |
| **变更边界** | **实现**: `DataLoader.validate()` 方法 + 13 个私有 `_check_*()` 方法。**不修复**: 需人工判断的问题（如数据源本身错误），记录 ERROR 日志后跳过 |
| **实现要求** | 13 项校验清单（`tech-plan.md §3.4` 12 项 + `§5.8` 1 项）：<br>□ 1. 股票列表按 `ts_code` 去重，无重复 → 重复时自动去重，保留第一条<br>□ 2. 每只股票有明确 `list_date`，`delist_date` 可为 None → 缺失 `list_date` → ERROR<br>□ 3. 日线 `(ts_code, trade_date)` 唯一 → 重复时保留第一条，记录 WARNING<br>□ 4. 无未来日期（`trade_date > today`）→ 丢弃，记录 ERROR<br>□ 5. 无负数价格或 `vol=0` 的异常记录 → 标记为 NaN，记录 WARNING<br>□ 5a. 日涨跌幅无 > 11% 的异常（且非新股首日）→ 标记为可疑，记录 WARNING<br>□ 6. 退市股退市日期后无新数据 → 丢弃超出数据，记录 WARNING<br>□ 7. `adj_factor` 连续递增（单日变化 > 50% 标记 WARNING）<br>□ 8. `daily_basic` 的 `trade_date` 与日线一致 → 不一致日期标记<br>□ 9. `fina_indicator` 的 `ann_date >= end_date` → 不满足者丢弃，记录 ERROR<br>□ 10. [v2.0] `fina_indicator` 含 `goodwill`, `total_equity`, `audit_opinion` → 缺失时 WARNING<br>□ 11. [v2.0] `pledge_stat` 覆盖 > 4000 只 → 不足时 WARNING<br>□ 12. [v2.0] Tushare vs BaoStock 日收益 Pearson corr ≥ 0.99 → < 0.99 时 ERROR<br>6. 返回值 `dict[str, bool]`：每项校验的通过/失败状态 |
| **验收标准** | ① 全部 13 项校验通过（或明确标注 WARNING 级别的问题及原因）<br>② 无 ERROR 级别残余（数据问题已修复或确认可接受）<br>③ `validate()` 返回 dict 全部为 `True`（或含 WARNING 键值为 `False` 但已有说明） |
| **验证方法** | `python -c "dl = DataLoader(c.data); result = dl.validate(); assert all(result.values()), f'Failed: {[k for k,v in result.items() if not v]}'"` |
| **Definition of Done** | ☐ 13 项校验全部实现 ☐ 每项校验失败有对应日志级别 ☐ 最终 validate() 全部通过 ☐ 校验报告可读（含统计摘要） |
| **执行记录** | （待执行后填写） |

---

## TODO-10: 增量更新、CLI 入口与单元测试

| 字段 | 内容 |
|------|------|
| **标题** | 实现增量更新 update_daily()、CLI 入口 `python -m core.data_loader`、`tests/test_data_loader.py` |
| **所属范围** | Phase 1 / Week 2 / 集成与测试 |
| **任务目标** | 提供增量更新能力（仅下载新交易日，避免全量重跑），提供命令行入口（`--download-all` / `--update` / `--validate`），编写覆盖 P1 优先级的单元测试 |
| **业务意图** | 全量下载需 ~20 分钟，不能每次更新数据都重跑。增量更新让策略在实盘中可持续。CLI 入口让用户无需写 Python 即可操作。单元测试是数据层的安全网——校验逻辑的 bug 比无校验更危险（伪安全感） |
| **前置条件与依赖** | TODO-04~09 全部完成（所有方法已实现） |
| **变更边界** | **实现**: `DataLoader.update_daily()`、`core/data_loader.py` 的 `if __name__ == '__main__'` CLI、`tests/test_data_loader.py`、`tests/test_config.py`。**不涉及**: 其他模块的测试 |
| **实现要求** | 1. **增量更新**: `update_daily()` — 检查 `tushare_daily/` 最新分区日期 → 从该日期+1 天开始下载到 `end_date`（或今天）。仅下载缺失的交易日。与 `download_daily_batch()` 共用下载逻辑<br>2. **CLI 入口**: `if __name__ == '__main__'` 支持 `--download-all`（TODO-05~07 全量）\| `--update`（增量）\| `--validate`（TODO-09）\| `--validate-cross-source`（TODO-07 交叉验证单独运行）。需先加载 config，初始化 DataLoader，调用对应方法<br>3. **单元测试** (`tech-plan.md §7.1 P1`): 至少覆盖：<br>&nbsp;&nbsp;**test_config.py** (P1):<br>&nbsp;&nbsp;- `test_default_value_merging` — 缺失段使用默认值<br>&nbsp;&nbsp;- `test_missing_token_error` — 缺失 token 抛出 ValueError<br>&nbsp;&nbsp;- `test_v20_new_params` — v2.0 新参数默认值验证<br>&nbsp;&nbsp;**test_data_loader.py** (P1):<br>&nbsp;&nbsp;- `test_stock_list_dedup` — 股票列表无重复<br>&nbsp;&nbsp;- `test_delisted_included` — 退市股在列表中<br>&nbsp;&nbsp;- `test_date_range_correct` — 下载数据日期范围与参数一致<br>&nbsp;&nbsp;- `test_incremental_no_duplicate` — 增量更新不产生重复<br>&nbsp;&nbsp;- `test_baostock_cross_validation_corr` — 交叉验证相关性 ≥ 0.99<br>&nbsp;&nbsp;- `test_pledge_stat_coverage` — 质押数据覆盖 > 4000<br>4. **测试数据**: 使用小范围（如仅 2015-01 一个月，或 mock Tushare API 响应），不依赖全量数据。若使用真实 API，测试标记为 `@pytest.mark.slow` |
| **验收标准** | ① `python -m core.data_loader --update` 仅下载新交易日（不重复下载已有数据）<br>② `python -m core.data_loader --validate` 运行全部校验<br>③ `pytest tests/test_config.py tests/test_data_loader.py -v` 全部 P1 测试通过<br>④ 增量更新后 `load_daily()` 加载数据量 = 原数据量 + 新交易日数据量（无重复） |
| **验证方法** | 先跑一次 `--download-all` → 记录 parquet 文件数 → 跑 `--update` → 文件数不变（因为已是最新）<br>`pytest tests/test_config.py tests/test_data_loader.py -v --cov=core/config --cov=core/data_loader --cov-report=term-missing` |
| **Definition of Done** | ☐ update_daily() 实现 ☐ CLI 支持 --download-all/--update/--validate ☐ test_config.py 含 ≥ 3 个 P1 测试 ☐ test_data_loader.py 含 ≥ 6 个 P1 测试 ☐ 全部测试通过 ☐ 增量更新无重复数据 |
| **执行记录** | （待执行后填写） |

---

## Phase 1 整体验收门禁

在全部 10 个 TODO 完成后，执行以下端到端验收：

```bash
# 1. 从零开始
rm -rf data/
python -m core.data_loader --download-all

# 2. 校验
python -m core.data_loader --validate
# 预期: 13/13 checks passed

# 3. 交叉验证
python -m core.data_loader --validate-cross-source
# 预期: correlation >= 0.99

# 4. 加载性能
time python -c "
from core.config import Config
from core.data_loader import DataLoader
c = Config.from_toml('config/settings.toml')
dl = DataLoader(c.data)
df = dl.load_daily('20150101', '20251231')
print(f'Rows: {len(df)}, Columns: {len(df.columns)}')
"
# 预期: Rows ~12M, 耗时 < 2s

# 5. 增量更新
python -m core.data_loader --update
# 预期: "已是最新，无需下载"

# 6. 测试
pytest tests/test_config.py tests/test_data_loader.py -v
# 预期: all passed
```

---

## 依赖关系图

```
TODO-01 (项目骨架)
   ├─→ TODO-02 (配置系统)
   │      └─→ TODO-04 (DataLoader 框架 + 交易日历)
   │             ├─→ TODO-05 (Tushare 核心下载)
   │             │      ├─→ TODO-06 (Tushare 辅助下载)
   │             │      ├─→ TODO-07 (BaoStock 交叉验证)
   │             │      └─→ TODO-08 (数据加载+复权)
   │             │             └─→ TODO-09 (数据校验)
   │             │                    └─→ TODO-10 (增量+CLI+测试)
   │             └─→ TODO-03 (日志) ──→ (被 TODO-05~10 引用)
   └─────────────→ TODO-03 也依赖 TODO-01 (logs/ 目录)
```

---

## 交叉验证确认清单

| 来源 | 引用点 | 覆盖 TODO |
|------|--------|:--------:|
| tech-plan.md §3.2 步骤2 | 股票列表下载接口 | TODO-05 |
| tech-plan.md §3.2 步骤3 | 日线批量下载 + 速率控制 | TODO-05 |
| tech-plan.md §3.2 步骤4-5 | daily_basic + fina_indicator | TODO-05 |
| tech-plan.md §3.2 步骤6-8 | SW分类/CSI500/质押数据 | TODO-06 |
| tech-plan.md §3.2 步骤9 | BaoStock 交叉验证 | TODO-07 |
| tech-plan.md §3.3 | Parquet Schema + 复权价格计算 | TODO-08 |
| tech-plan.md §3.4 + §5.8 | 13 项数据校验清单（12 + 1 异常检测） | TODO-09 |
| tech-plan.md §4.1-4.1.1 | Config dataclass + settings.toml | TODO-02 |
| tech-plan.md §4.2 | DataLoader 接口（13 方法签名） | TODO-04 |
| tech-plan.md §5.7 | 交易日历处理 | TODO-04 |
| tech-plan.md §5.8 | 4 层异常恢复策略 | TODO-05/07/09 |
| tech-plan.md §六 Phase 1 | 18 项任务分解 + 预估耗时 | 全部 |
| tech-plan.md §七 | P1 测试要求 | TODO-10 |
| tech-plan.md §九 | requirements.txt 清单 | TODO-01 |
| tech-plan.md §十 | 环境搭建 + .gitignore | TODO-01 |
| strategy-plan-v2.md §六 | Phase 1 + BaoStock + pledge_stat | TODO-05/06/07 |
| algorithm-workflow.md §二 | 数据下载 10 步流程 | TODO-04~09 |
| CLAUDE.md | 生存者偏差/Tushare约束/数据校验 | TODO-05/09 |
| **发现: 交易日历未独立列任务** | algorithm-workflow.md 步骤3 隐式依赖 trade_cal | TODO-04 已补充 |
| **发现: adj_factor 3000行分页** | tech-plan.md §3.3 注释 | TODO-08 已处理 |
| **发现: BaoStock 字段映射** | preclose→pre_close, code→ts_code, date→trade_date, volume→vol | TODO-07 已处理 |

**交叉验证结论**: 全部 16 个交叉引用点已覆盖，3 个隐式需求已补充，0 个遗漏，0 个冗余，0 个偏差。

**2026-06-04 对齐修复记录**（tech-plan.md vs phase1-todos.md 逐项核对后）:
1. ✅ `.gitignore` 补充 `*.egg-info/`（tech-plan §10.2 第1669行）
2. ✅ TODO-05 错误恢复补充 `RateLimitError` → sleep(60) + 10min暂停 以及指数退避具体间隔 1s→2s→4s→8s（tech-plan §5.8 第1198-1203行）
3. ✅ TODO-09 补充校验项 5a「日涨跌幅 > 11% 异常检测」（tech-plan §5.8 第1225行）
4. ✅ TODO-07 前置条件补充 BaoStock 连接独立验证 `bs.login()`（tech-plan §10.1 步骤5 第1647-1655行）
5. ✅ TODO-10 补充 `tests/test_config.py` 及 3 个 P1 测试（tech-plan §7.1 第1401行）
6. ✅ TODO-10 DoD/验收标准/验证方法同步更新含 test_config.py
