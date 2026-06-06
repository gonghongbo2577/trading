# 详细技术方案与实施计划书

> **版本**: v2.0
> **日期**: 2026-06-04
> **状态**: 详细设计（对齐 strategy-plan-v2.md v2.0）
> **基于**: 策略方案 strategy-plan-v2.md v2.0 + algorithm-workflow.md v2.0
> **代码行数预估**: 核心模块 ~1200 行 + 测试 ~600 行 = ~1800 行 Python
> **v2.0 变更**: 14 项系统性变更（因子重构、VMP全扩展、IC引导权重、10层Universe、MA20择时、Walk-Forward+PBO+DSR回测、BaoStock双源验证等）

---

## 摘要

本文档是策略方案 v2.0 的工程实现层设计，定义完整的技术架构、模块接口、数据流、实施顺序和测试策略。所有技术决策均基于交叉验证，标注了证据来源。

**v2.0 核心变化**（相对 v1.3 tech-plan）：
- 因子模块重写：BP+ 复合估值、残差动量、ln Size 变换、VMP 全因子扩展、IC 衰减引导权重
- Universe 从 7 层扩展到 10 层（+审计意见、商誉、质押过滤）
- 择时参数校准：MA60→MA20、恢复加入 3 日迟滞
- 熔断收紧：绝对回撤 25%→15%
- 回测方法升级：Walk-Forward + PBO + DSR 替代单次分割
- 数据源增强：Tushare + BaoStock 双源交叉验证
- 现金管理：闲置资金 GC001 国债逆回购

---

## 一、技术选型（已验证）

### 1.1 核心技术栈

| 层面 | 选型 | 版本 | 依据 |
|------|------|------|------|
| **语言** | Python | **3.12** | pandas/numpy/scipy 全部支持；3.11 支持 2025-10 到期；3.13 的 statsmodels 不兼容 |
| **数据存储** | Parquet + Zstd | — | 比 CSV 快 3-5 倍读取，存储省 3 倍空间，原生列式存储支持谓词下推 |
| **数据 ETL** | DuckDB | ≥1.1 | 内存占用最低（~64 MB vs pandas 700+ MB），SQL 查询，支持溢出到磁盘 |
| **因子计算** | Polars | ≥1.0 | 比 pandas 快 3-7 倍（groupby 截面操作），惰性 API，原生多线程 |
| **回测引擎** | 自定义事件驱动 | — | 现有框架（backtrader/zipline）对 5000 只股票需数小时，自行实现 600-1000 行即可 |
| **配置管理** | TOML | `tomllib`（内置） | Python 3.11+ 内置，零依赖，无 YAML 隐式类型陷阱 |
| **日志** | structlog | ≥24.0 | 结构化 JSON 日志 + 可读控制台输出 |
| **主数据源** | Tushare Pro | ≥1.4.26 | 2120 积分，已确认覆盖全部所需 API |
| **[v2.0] 备选数据源** | **BaoStock** | ≥0.8.0 | 免费、无需注册；日线 1990 至今；用于交叉验证和 Tushare 断服应急 |

### 1.2 关键验证：Tushare API 调用策略

**已验证事实**：`pro_bar` 一次只能查询一只股票，不可批量。

| 下载策略 | API 调用次数 | 时间（200次/分钟） | 可行性 |
|---------|------------|-------------------|--------|
| ❌ `pro_bar` 逐只股票 | 5000+ 次 | 25+ 分钟 | 可行但不推荐 |
| ✅ `pro.daily()` 按交易日 | ~2450 次（10年） | ~12 分钟 | **推荐** |

**结论**：使用 `pro.daily(trade_date='YYYYMMDD')` 按交易日批量下载全市场日线，每次调用返回全市场当日数据（≤6000 行）。复权数据用 `adj_factor` API 补充，或对持仓股单独调用 `pro_bar`。

### [v2.0] 1.3 BaoStock 备选数据源策略

BaoStock 作为免费的交叉验证数据源，在以下场景使用：
- **日线交叉验证**：Tushare vs BaoStock 日收益相关系数 ≥ 0.99（合格），< 0.95 触发数据质量警报
- **应急替代**：Tushare 服务中断时，BaoStock 提供日线 + 财务数据
- **安装**：`pip install baostock`，无需 token，`bs.login()` 即可使用
- **限制**：复权方式为涨跌幅复权法（与 Tushare 现金红利复权存在系统性差异），仅用于交叉验证不用于主策略

---

## 二、系统架构

### 2.1 整体数据流

```
┌─────────────┐    ┌──────────────┐    ┌───────────────┐    ┌──────────────┐
│  Tushare    │───>│  data_loader │───>│  Parquet      │───>│  DuckDB/     │
│  API        │    │  下载 + 清洗  │    │  分区存储      │    │  Polars      │
└─────────────┘    └──────────────┘    └───────────────┘    │  数据加载     │
                                                            └──────┬───────┘
┌─────────────┐    ┌──────────────┐                                  │
│  BaoStock   │───>│  data_loader │───> cross validation ────────────┤
│  API (备选) │    │  交叉验证     │                                  │
└─────────────┘    └──────────────┘                                  │
                                                                     │
┌─────────────┐    ┌──────────────┐    ┌───────────────┐              │
│  输出报告    │<───│  backtest    │<───│  factors.py   │<─────────────┘
│  + 图表      │    │  回测引擎     │    │  因子计算      │
└─────────────┘    └──────┬───────┘    └───────────────┘
                          │
                   ┌──────┴───────┐
                   │  risk.py     │
                   │  风控模块     │
                   └──────────────┘
```

### 2.2 模块依赖关系

```
                    config/settings.toml
                           │
                           ▼
                    ┌──────────────┐
                    │ data_loader  │  ← 无依赖，最先实现
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
       ┌──────────┐ ┌──────────┐
       │ universe │ │ factors  │  ← 依赖 data_loader
       └────┬─────┘ └────┬─────┘
            │             │
            └──────┬──────┘
                   ▼
            ┌──────────┐
            │ backtest │  ← 依赖 factors + universe
            └────┬─────┘
                   │
         ┌─────────┼─────────┐
         ▼         ▼         ▼
   ┌─────────┐ ┌──────┐ ┌──────────┐
   │  risk   │ │ perf │ │ report   │  ← 依赖 backtest
   └─────────┘ └──────┘ └──────────┘
```

### 2.3 目录结构（最终形态）

```
trading/
├── config/
│   ├── settings.toml         # 主配置（token、资金、风控参数）
│   └── settings.template.toml # 配置模板（不含 token，可提交 git）
├── data/
│   ├── raw/                   # 原始下载（按数据源存放）
│   │   ├── tushare_daily/     # 日线（按日期分区 parquet）
│   │   ├── tushare_basic/     # daily_basic
│   │   ├── tushare_fina/      # 财务指标
│   │   ├── tushare_pledge/    # [v2.0] 股权质押数据
│   │   └── baostock_daily/    # [v2.0] BaoStock 日线交叉验证
│   └── processed/             # 清洗后因子数据
│       └── factors/           # 因子值（按日期分区 parquet）
├── core/
│   ├── __init__.py
│   ├── config.py              # 配置加载器
│   ├── data_loader.py         # Tushare + BaoStock 数据下载与本地缓存
│   ├── universe.py            # 可投资池构建（[v2.0] 十层过滤）
│   ├── factors.py             # 因子计算（Polars）[v2.0 重写]
│   ├── backtest.py            # 事件驱动回测引擎 [v2.0 Walk-Forward]
│   ├── risk.py                # 风控模块（仓位择时、熔断）[v2.0 MA20]
│   ├── portfolio.py           # 组合构建与订单生成 [v2.0 +GC001]
│   ├── performance.py         # 绩效分析（收益归因、指标计算）
│   ├── report.py              # 报告生成（图表 + 表格）
│   └── validation.py          # [v2.0] PBO + DSR 统计检验
├── strategies/
│   └── multi_factor.toml      # 多因子策略参数配置
├── research/
│   ├── 01_factor_ic.ipynb     # 单因子 IC 分析
│   ├── 02_factor_corr.ipynb   # 因子相关性矩阵
│   ├── 03_backtest_result.ipynb # 回测结果分析
│   └── 04_walk_forward.ipynb  # [v2.0] Walk-Forward 路径分析
├── output/
│   ├── reports/               # 回测报告（HTML/PDF）
│   └── charts/                # 图表输出
├── logs/                      # 运行日志（JSON 格式，日志轮转）
├── docs/
│   ├── strategy-plan-v2.md    # 策略方案文档 v2.0
│   ├── tech-plan.md           # 本文档 v2.0
│   └── algorithm-workflow.md  # 算法流图 v2.0
├── tests/                     # 单元测试
│   ├── test_data_loader.py
│   ├── test_universe.py
│   ├── test_factors.py
│   ├── test_backtest.py
│   └── test_risk.py
├── .env                       # Tushare token（gitignore）
├── .gitignore
├── requirements.txt           # Python 依赖
└── README.md
```

---

## 三、数据层设计（Phase 1 核心）

### 3.1 数据下载流程

```
步骤 1: 验证 Tushare token + BaoStock 连接
步骤 2: 下载全量股票列表（含退市股）
步骤 3: 按交易日批量下载日线（Tushare）
步骤 4: 下载 daily_basic（估值指标）
步骤 5: 下载 fina_indicator（财务指标）
步骤 6: 下载申万行业分类
步骤 7: 下载中证 500 指数行情（用于基准和择时）
步骤 8: [v2.0] 下载 pledge_stat（股权质押数据）
步骤 9: [v2.0] 下载 BaoStock 日线（相同区间，用于交叉验证）
步骤 10: 数据质量校验（含 [v2.0] Tushare vs BaoStock 日收益相关性校验）
```

### 3.2 各步骤详细设计

#### 步骤 2：全量股票列表

```python
# 伪代码接口
def download_stock_list(pro) -> pl.DataFrame:
    """
    返回列: ts_code, name, market, list_date, delist_date, list_status
    合并 list_status in ('L', 'D', 'P') 三部分
    """
    listed = pro.stock_basic(list_status='L')
    delisted = pro.stock_basic(list_status='D')
    suspended = pro.stock_basic(list_status='P')
    return pl.concat([listed, delisted, suspended])
```

#### 步骤 3：按交易日批量下载日线

```python
def download_daily_by_date(pro, start_date: str, end_date: str, output_dir: str):
    """
    核心策略: 遍历每个交易日，调用 pro.daily(trade_date=date)
    每次返回全市场 ~5000 行，存为一个 parquet 文件
    
    分区: data/raw/tushare_daily/date=YYYYMMDD/data.parquet
    
    速率控制: time.sleep(0.35) 每调用一次（200次/分钟 = 300ms间隔，留余量）
    
    10 年跨度约 2450 个交易日 → ~2450 次调用 → ~14 分钟
    """
```

#### 步骤 4-5：估值和财务数据

```python
# daily_basic: 按交易日批量，与日线类似
def download_daily_basic(pro, start_date, end_date, output_dir):
    """分区: data/raw/tushare_basic/date=YYYYMMDD/data.parquet"""

# fina_indicator: 按报告期批量下载
def download_fina_indicator(pro, start_date, end_date, output_dir):
    """
    按 end_date 季度批量获取
    保留 ann_date 字段用于 Point-in-Time 过滤
    [v2.0] 额外保留字段: goodwill (商誉), total_equity (净资产), audit_opinion (审计意见)
    分区: data/raw/tushare_fina/end_date=YYYYQQ/data.parquet
    """
```

#### 步骤 6：申万行业分类

```python
def download_sw_classification(pro) -> pl.DataFrame:
    """
    返回: ts_code, l1_name (申万一级行业)
    使用 index_classify(level='L1', src='SW2021') + index_member_all()
    [v2.0] 行业分类用于: ① 行业感知去极值 ② 行业集中度检查 ③ 残差动量截面回归
    """
```

#### [v2.0] 步骤 8：股权质押数据

```python
def download_pledge_stat(pro, ts_codes: list[str]) -> pl.DataFrame:
    """
    返回: ts_code, pledge_ratio (总质押比例), ctrl_pledge_ratio (控股股东质押比例)
    使用 pledge_stat API（2120积分权限）
    过滤条件: 总质押比例 ≥ 50% OR 控股股东质押 ≥ 80% → 排除
    存储: data/raw/tushare_pledge/pledge.parquet（每月更新）
    """
```

#### [v2.0] 步骤 9：BaoStock 日线交叉验证

```python
def download_baostock_daily(start_date: str, end_date: str, output_dir: str):
    """
    使用 baostock 免费 API 下载相同区间的日线数据
    字段: date, code, open, high, low, close, preclose, volume, amount
    分区: data/raw/baostock_daily/date=YYYYMMDD/data.parquet
    
    交叉验证方法:
      corr = pearson(tushare_returns, baostock_returns)
      预期: corr ≥ 0.99（日收益层面）
      若 corr < 0.95 → 触发数据质量警报
    """
```

### 3.3 Parquet 分区与存储格式

#### 分区策略

```
data/raw/tushare_daily/
  date=2015-01-05/
    data.parquet          ← 一个文件，约 5000 行 × 11 列
  date=2015-01-06/
    data.parquet
  ...
  date=2025-12-31/
    data.parquet
  
  → 约 2500 个分区目录，每个约 200-500 KB
  → 总磁盘占用约 500-800 MB（Zstd 压缩）

data/raw/baostock_daily/   # [v2.0]
  date=2015-01-05/
    data.parquet
  ...
  → 结构与 tushare_daily 一致，用于交叉验证
  → 总磁盘占用约 300-500 MB（字段更少）
```

#### 日线 Parquet Schema

| 列名 | 类型 | 说明 |
|------|------|------|
| ts_code | str | 股票代码（如 000001.SZ） |
| trade_date | date | 交易日期 |
| open | float32 | 开盘价（不复权原始价） |
| high | float32 | 最高价 |
| low | float32 | 最低价 |
| close | float32 | 收盘价 |
| pre_close | float32 | 前收盘价 |
| change | float32 | 涨跌额 |
| pct_chg | float32 | 涨跌幅（%） |
| vol | float64 | 成交量（手） |
| amount | float64 | 成交额（千元） |

> **不复权价格**：原始价格 + 独立存储的复权因子（`adj_factor` API），因子计算时动态复权，避免数据源锁定 pro_bar。
> 
> **注意**：`adj_factor` API 单次最大返回 3000 行，全市场约 5000 只股票需按 ts_code 首字母分两批调用（如 `'0'-'3'` 和 `'6'-'8'`），或按 trade_date 分批。

#### 复权价格计算

```python
# 不复权价格 + 复权因子 = 后复权价格
df = df.join(adj_factor_df, on=['ts_code', 'trade_date'])
df = df.with_columns(
    (pl.col('close') * pl.col('adj_factor')).alias('close_adj')
)
```

#### [v2.0] 处理后因子值 Parquet Schema

分区策略：`data/processed/factors/date=YYYYMMDD/data.parquet`（与日线对齐）

| 列名 | 类型 | 说明 |
|------|------|------|
| ts_code | str | 股票代码 |
| trade_date | date | 交易日期（分区键） |
| **[v2.0]** bp_raw | float32 | BP 原始值 = 1/pb |
| **[v2.0]** ep_raw | float32 | EP 原始值 = 1/pe_ttm |
| **[v2.0]** cfp_raw | float32 | CFP 原始值 = 经营现金流/总市值 |
| roe_raw | float32 | ROE 原始值（年化，PIT 过滤后） |
| **[v2.0]** res_mom_raw | float32 | 残差动量原始值（截面回归残差 ε_i） |
| iv60_raw | float32 | IV60 原始值 = 1/std(60日收益) |
| **[v2.0]** size_raw | float32 | Size 原始值 = **-ln(总市值)** |
| z_bp | float32 | BP Z-score |
| z_ep | float32 | EP Z-score |
| z_cfp | float32 | CFP Z-score |
| **[v2.0]** z_value | float32 | BP+ 复合估值 Z-score（子因子等权平均） |
| z_roe | float32 | ROE Z-score |
| **[v2.0]** z_res_mom | float32 | 残差动量 Z-score |
| z_iv60 | float32 | IV60 Z-score |
| z_size | float32 | Size Z-score |
| **[v2.0]** w_vmp_value | float32 | 价值因子 VMP 缩放系数（≤2.0） |
| **[v2.0]** w_vmp_roe | float32 | ROE VMP 缩放系数 |
| **[v2.0]** w_vmp_mom | float32 | 动量 VMP 缩放系数 |
| **[v2.0]** w_vmp_iv60 | float32 | IV60 VMP 缩放系数 |
| **[v2.0]** w_vmp_size | float32 | Size VMP 缩放系数 |
| **[v2.0]** sigma_60d_value | float32 | 价值因子 60 日已实现波动率（年化） |
| **[v2.0]** sigma_60d_roe | float32 | ROE 因子 60 日已实现波动率 |
| **[v2.0]** sigma_60d_mom | float32 | 动量因子 60 日已实现波动率 |
| **[v2.0]** sigma_60d_iv60 | float32 | IV60 因子 60 日已实现波动率 |
| **[v2.0]** sigma_60d_size | float32 | Size 因子 60 日已实现波动率 |
| **[v2.0]** ic_weight_value | float32 | 价值因子 IC 衰减权重 |
| **[v2.0]** ic_weight_roe | float32 | ROE IC 衰减权重 |
| **[v2.0]** ic_weight_mom | float32 | 动量 IC 衰减权重 |
| **[v2.0]** ic_weight_iv60 | float32 | IV60 IC 衰减权重 |
| **[v2.0]** ic_weight_size | float32 | Size IC 衰减权重 |
| **[v2.0]** composite_score | float32 | 综合得分 `Σ(ic_weight_i × w_vmp_i × z_i) / Σ(ic_weight_i × w_vmp_i)` |
| in_universe | bool | 该日是否在可投资池中 |

### 3.4 数据质量校验清单

开发时必须实现的校验函数：

```python
def validate_data():
    """
    □ 股票列表去重（按 ts_code），无重复
    □ 每只股票有明确的 list_date 和 delist_date（或 None）
    □ 日线每个 (ts_code, trade_date) 唯一
    □ 无未来日期数据（trade_date ≤ 今天）
    □ 无负数价格或成交量为 0 的异常记录
    □ 退市股退市日期后无新数据
    □ 复权因子连续递增（无跳跃，除权日当天有跳变但值是连续的）
    □ daily_basic 的 trade_date 与日线一致
    □ fina_indicator 的 ann_date ≥ end_date（公告日不早于截止日）
    □ [v2.0] fina_indicator 含 goodwill, total_equity, audit_opinion 字段
    □ [v2.0] pledge_stat 数据覆盖全市场（> 4000 只股票）
    □ [v2.0] Tushare vs BaoStock 日收益 Pearson 相关系数 ≥ 0.99
    """
```

---

## 四、核心模块接口设计

### 4.1 config.py — 配置加载器

```python
"""
功能: 加载 TOML 配置，合并默认值，暴露类型安全的配置对象
输入: config/settings.toml
输出: Config dataclass
"""
import tomllib
from dataclasses import dataclass

@dataclass
class DataConfig:
    tushare_token: str
    raw_dir: str = "data/raw"
    processed_dir: str = "data/processed"
    start_date: str = "20150101"
    end_date: str = "20251231"
    # [v2.0] BaoStock 配置
    use_baostock_validation: bool = True   # 是否启用 BaoStock 交叉验证
    baostock_raw_dir: str = "data/raw/baostock_daily"

@dataclass
class FactorConfig:
    """[v2.0] 因子计算参数（全因子VMP + IC引导权重）"""
    # VMP 波动率缩放（全部5因子）
    sigma_target: float = 0.15            # 目标年化波动率（Wang & Li 2024）
    mom_lookback: int = 60                # 波动率回看交易日（Moreira & Muir 2017）
    vmp_upper_bound: float = 2.0          # [v2.0] VMP缩放上限（允许适度放大）
    
    # IC 衰减引导权重
    ic_lookback_months: int = 24          # [v2.0] IC 回看窗口（月）
    ic_half_life_months: int = 6          # [v2.0] IC 衰减半衰期（月），默认6月；策略支持因子特异的4-12月调节
    ic_weight_deviation_max: float = 0.5  # [v2.0] IC 权重偏离等权上限（±50%）

@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_rate: float = 0.00025     # 万2.5
    min_commission: float = 5.0           # 最低5元
    stamp_tax_rate: float = 0.0005        # 万5（卖出）
    transfer_fee_rate: float = 0.00001    # 万0.1（沪市）
    slippage_bps: float = 0.10            # 滑点 10bps
    # [v2.0] Walk-Forward 回测参数
    walk_forward_train_years: int = 3     # 训练窗口（年）
    walk_forward_test_years: int = 1      # 验证窗口（年）
    pbo_threshold: float = 0.30           # PBO 阈值（< 30% 为合格）

@dataclass
class RiskConfig:
    max_single_weight: float = 0.10
    max_industry_weight: float = 0.30
    max_star_market_weight: int = 2       # [v2.0] 科创板最大持仓数（/10）
    # Market timing (v2.0 dual-condition with calibrated params)
    ma_period: int = 20                   # [v2.0] 均线周期（MA60→MA20，长城证券2016）
    vol_window: int = 20                  # 波动率计算窗口
    vol_percentile: float = 0.50          # 波动率阈值（> 60日历史中位数）
    hysteresis_down: int = 5              # 降仓迟滞天数
    hysteresis_up: int = 3                # [v2.0] 恢复迟滞天数（华泰金工2026）
    reduced_position_ratio: float = 0.50
    # Circuit breakers
    alpha_drawdown_threshold: float = -0.10  # 6个月超额 < -10%
    early_warning_threshold: float = -0.03   # 3个月超额 < -3%
    hard_drawdown_limit: float = -0.15       # [v2.0] 绝对回撤 > 15%（原25%）
    # [v2.0] Cash management
    enable_gc001: bool = True                 # 闲置资金自动申购 GC001

def load_config(path: str = "config/settings.toml") -> Config:
    """加载配置，缺失字段使用默认值。
    返回包含 DataConfig, FactorConfig, BacktestConfig, RiskConfig 的 Config 对象
    """

@dataclass
class Config:
    """顶层配置聚合"""
    data: DataConfig
    factor: FactorConfig      # [v2.0] 全因子VMP + IC权重
    backtest: BacktestConfig  # [v2.0] Walk-Forward参数
    risk: RiskConfig          # [v2.0] MA20 + 恢复迟滞 + 15%熔断

    @classmethod
    def from_toml(cls, path: str) -> 'Config':
        """从 TOML 文件加载，缺失段使用默认值"""
        ...
```

### 4.1.1 settings.toml 完整模板

```toml
# config/settings.toml — 多因子策略完整配置 v2.0
# 复制此文件为 settings.toml 并填入实际值

[data]
tushare_token = "YOUR_TOKEN_HERE"   # ⚠️ 必填，从 https://tushare.pro 获取
raw_dir = "data/raw"
processed_dir = "data/processed"
start_date = "20150101"
end_date = "20251231"
# [v2.0] BaoStock 交叉验证
use_baostock_validation = true
baostock_raw_dir = "data/raw/baostock_daily"

[factor]                            # [v2.0] 全因子VMP + IC引导权重
sigma_target = 0.15                 # 目标年化波动率（Wang & Li 2024）
mom_lookback = 60                   # VMP波动率回看交易日（Moreira & Muir 2017）
vmp_upper_bound = 2.0               # VMP缩放上限（允许适度放大）
ic_lookback_months = 24             # IC回看窗口
ic_half_life_months = 6             # IC衰减半衰期
ic_weight_deviation_max = 0.5       # IC权重偏离等权上限（±50%）

[backtest]
initial_capital = 100000.0
commission_rate = 0.00025           # 万2.5
min_commission = 5.0                # 最低5元/笔
stamp_tax_rate = 0.0005             # 万5（卖出）
transfer_fee_rate = 0.00001         # 万0.1（沪市双向）
slippage_bps = 0.10                 # 滑点 10bps
walk_forward_train_years = 3        # [v2.0] Walk-Forward 训练窗口
walk_forward_test_years = 1         # [v2.0] Walk-Forward 验证窗口
pbo_threshold = 0.30                # [v2.0] PBO 合格阈值

[risk]
max_single_weight = 0.10
max_industry_weight = 0.30
max_star_market_weight = 2          # [v2.0] 科创板最大持仓数/10
ma_period = 20                      # [v2.0] 均线周期（MA60→MA20）
vol_window = 20                     # 波动率计算窗口
vol_percentile = 0.50               # 波动率阈值（>60日历史中位数）
hysteresis_down = 5                 # 降仓迟滞天数
hysteresis_up = 3                   # [v2.0 新增] 恢复迟滞天数
reduced_position_ratio = 0.50
alpha_drawdown_threshold = -0.10    # 6月超额<-10% → STOP
early_warning_threshold = -0.03     # 3月超额<-3% → WARNING
hard_drawdown_limit = -0.15         # [v2.0] 绝对回撤>15% → STOP（原25%）
enable_gc001 = true                 # [v2.0] 闲置资金自动申购GC001
```

### 4.2 data_loader.py — 数据下载器

```python
"""
功能: Tushare + BaoStock 数据下载、本地缓存、增量更新、双源交叉验证
对外接口:
  - download_all(pro, config) → None  首次全量下载
  - update_daily(pro, config) → None  增量更新（仅下载新交易日）
  - load_daily(start, end) → pl.DataFrame  加载本地数据
  - [v2.0] validate_cross_source() → dict  Tushare vs BaoStock 交叉验证
"""

class DataLoader:
    def __init__(self, config: DataConfig):
        self.config = config
        self.pro = ts.pro_api(config.tushare_token)

    def download_stock_list(self) -> pl.DataFrame: ...
    def download_daily_batch(self, start: str, end: str) -> None: ...
    def download_daily_basic(self, start: str, end: str) -> None: ...
    def download_fina_indicator(self, start: str, end: str) -> None: ...
    def download_sw_classification(self) -> pl.DataFrame: ...
    def download_index_daily(self, index_code: str, start: str, end: str) -> pl.DataFrame: ...
    
    # [v2.0] 新增方法
    def download_pledge_stat(self, ts_codes: list[str]) -> pl.DataFrame:
        """下载股权质押数据，返回质押比例"""
        ...
    
    def download_baostock_daily(self, start: str, end: str) -> None:
        """下载 BaoStock 日线用于交叉验证"""
        ...
    
    def validate_cross_source(self, start: str, end: str) -> dict:
        """Tushare vs BaoStock 日收益 Pearson 相关系数校验
        返回: {'correlation': 0.998, 'pass': True, 'warning_dates': []}
        """
        ...

    def load_daily(self, start: str, end: str) -> pl.DataFrame:
        """从 parquet 加载日线数据，自动合并复权因子"""
        ...

    def validate(self) -> dict[str, bool]:
        """返回各校验项通过/失败状态"""
        ...
```

### 4.3 universe.py — 可投资池 [v2.0 十层过滤]

```python
"""
功能: [v2.0] 十层过滤构建可投资池
输入: 日线数据 + 股票列表 + 财务数据 + 行业分类 + 质押数据
输出: 每个调仓日的可投资股票代码列表

过滤层:
  ① ST/*ST（退市风险警示股）
  ② 上市 < 1 年（次新股无足够历史数据）
  ③ [v2.0] 科创板非CSI 300/500/1000成分股（放宽原完全排除）
  ④ PE_TTM ≤ 0（亏损股）
  ⑤ 20日日均成交额 < 2000万（流动性不足）
  ⑥ 股价 < 2 元（面值退市风险）
  ⑦ 净资产 ≤ 0（资不抵债）
  ⑧ [v2.0 新增] 最近年度被出具非标审计意见
  ⑨ [v2.0 新增] 商誉/净资产 > 30%
  ⑩ [v2.0 新增] 股权质押比例 ≥ 50% 或 控股股东质押 ≥ 80%
"""

class UniverseBuilder:
    def __init__(self, config):
        self.filters = [
            self._filter_st,
            self._filter_ipo,
            self._filter_star_market,      # [v2.0] 放宽：指数成分股可纳入
            self._filter_negative_pe,
            self._filter_low_liquidity,
            self._filter_low_price,
            self._filter_negative_equity,
            self._filter_audit_opinion,    # [v2.0 新增]
            self._filter_goodwill,         # [v2.0 新增]
            self._filter_pledge,           # [v2.0 新增]
        ]

    def build(self, date: date, daily_data: pl.DataFrame,
              stock_list: pl.DataFrame, fina_data: pl.DataFrame,
              pledge_data: pl.DataFrame = None) -> pl.DataFrame:  # [v2.0] +pledge
        """返回该日可投资的 ts_code 列表"""
        ...

    def build_all_dates(self, date_range, ...) -> dict[date, pl.DataFrame]:
        """返回每个调仓日的 universe"""
        ...
```

### 4.4 factors.py — 因子计算 [v2.0 重写]

```python
"""
功能: [v2.0] 五因子计算 + 行业感知去极值 + 截面标准化 + 全因子VMP缩放 + IC引导权重合成
输入: 日线数据（含复权价） + 估值数据 + 财务数据 + 行业分类
输出: 每个调仓日每只股票的因子 z-score 和综合得分

[v2.0] 因子列表:
  1. BP+ (复合估值) = (z_bp + z_ep + z_cfp) / 3（金融股仅EP+CFP）
  2. ROE           = roe_yearly (Point-in-Time)
  3. 残差MOM        = ε_i（原始6-1M动量对市场+行业回归的残差）
  4. IV60           = 1 / std(daily_returns, 60)
  5. Size           = -ln(total_mv)

处理流程:
  Step A: 计算原始因子值 (compute_raw)
  Step A1: [v2.0] 残差动量截面回归
  Step B: [v2.0] 行业感知去极值（申万一级行业内 Winsorize 1%/99%）
  Step C: 截面 Z-score 标准化 (per date)
  Step D: [v2.0] 全因子 VMP 缩放系数计算（5个因子各自 w_vmp_i）
  Step E: [v2.0] IC 衰减引导权重计算（24月滚动窗口）
  Step F: [v2.0] 三维修正等权合成综合得分
"""

class FactorCalculator:
    def compute_raw_factors(self, date, data) -> pl.DataFrame:
        """[v2.0] 计算 5 个因子的原始值"""
        ...

    def compute_residual_momentum(self, date, data, industry_map) -> pl.Series:
        """[v2.0] 残差动量计算:
        Step 1: r_i(t-126, t-21) = 每只股票过去6月（跳过1月）累计收益
        Step 2: 截面回归 r_i = α + β_mkt·r_mkt + Σβ_ind·I_ind + ε_i
        Step 3: 残差动量 = ε_i（不能被市场和行业解释的纯个股动量）
        
        来源: BigQuant(2024) + 华泰证券(2024-2025) + GF证券(2024)
        """
        ...

    def winsorize_industry_aware(self, df, factor_cols, industry_col) -> pl.DataFrame:
        """[v2.0] 行业感知去极值:
        在申万一级行业内部分别执行 1%/99% 分位数截断
        行业内样本量 < 30 → 回退到全市场去极值
        目的: 保留行业间结构性差异
        """
        ...

    def standardize_cross_section(self, df, factor_cols) -> pl.DataFrame:
        """截面 Z-score 标准化（全市场）"""
        ...

    def compute_factor_volatility(self, factor_returns: pl.Series,
                                   lookback: int = 60) -> float:
        """[v2.0] 计算单个因子的已实现波动率
        σ_realized = std(过去60日因子日收益) × √252
        用于全5因子的VMP缩放
        """
        ...

    def compute_vmp_weights(self, df,
                            sigma_target: float = 0.15,
                            lookback: int = 60,
                            upper_bound: float = 2.0) -> pl.DataFrame:
        """[v2.0] 全因子 VMP 缩放系数:
        w_vmp_i = min(upper_bound, sigma_target / σ_i_60d)
        对每个因子i独立计算
        边界: σ→0 → w_vmp_i = upper_bound; σ→∞ → w_vmp_i → 0
        
        来源: Wang & Li (2024) + Barroso & Santa-Clara (2015) + Moreira & Muir (2017)
        """
        ...

    def compute_ic_decay_weights(self, ic_history: pl.DataFrame,
                                  lookback_months: int = 24,
                                  half_life: int = 6,
                                  max_deviation: float = 0.5) -> pl.DataFrame:
        """[v2.0] IC 衰减引导权重:
        IC_weight_i = Σ_t decay(t) × RankIC_i(t) / Σ_i Σ_t decay(t) × RankIC_i(t)
        decay(t) = exp(-ln(2) × t / half_life_months)
        IC_weight_i 限制在等权基线(0.2)的±50%范围内 = [0.1, 0.3]

        来源: 国泰君安(2013) + 广发证券(2018) + 中信建投(2019)
        """
        ...

    def compute_composite_score(self, df,
                                 sigma_target: float = 0.15,
                                 mom_lookback: int = 60,
                                 vmp_upper_bound: float = 2.0) -> pl.DataFrame:
        """[v2.0] 三维修正等权合成:
        adj_weight_i = IC_weight_i × w_vmp_i
        Composite = Σ(adj_weight_i × z_i) / Σ(adj_weight_i)
        
        边界条件:
          - σ_i_60d → 0 → w_vmp_i = vmp_upper_bound
          - RankIC_i 转负 → IC_weight_i 设下限 0.10（仍参与归一化）
        """
        ...

    def compute_all_dates(self, dates, data) -> pl.DataFrame:
        """批量计算所有调仓日的因子得分"""
        ...
```

### 4.5 portfolio.py — 组合构建 [v2.0 +GC001]

```python
"""
功能: 根据综合得分排名选股 → 行业检查 → 生成目标持仓 → [v2.0] 闲置现金管理
输入: 某日 factor_scores + universe + 行业分类
输出: 目标持仓列表 + 交易订单列表
"""

class PortfolioConstructor:
    def select_top_n(self, scores, universe, n=10) -> pl.DataFrame:
        """在 universe 内选综合得分 Top N
        [v2.0] 科创板持仓上限 max_star_market_weight（默认2/10）
        """
        ...

    def check_industry_concentration(self, selected, industry_map, max_pct=0.30):
        """检查行业集中度，超过 30% 时替换"""
        ...

    def generate_orders(self, current_holdings, target_holdings,
                        prices, capital, lot_size=100):
        """
        生成订单列表
        处理: 先卖后买 → 手数取整 → 资金分配 → 异常 fallback
        返回: list[Order]
        """
        ...
    
    # [v2.0] 新增方法
    def allocate_idle_cash(self, cash: float, enable_gc001: bool = True) -> float:
        """闲置资金管理:
        当择时信号降仓至50%时，闲置的~5万资金自动申购GC001国债逆回购
        年化收益约1.5-2.5%，T+0可用（不影响调仓）
        仅在 Phase 5 实盘时启用
        """
        ...
```

### 4.6 backtest.py — 回测引擎 [v2.0 Walk-Forward]

```python
"""
功能: 事件驱动回测引擎 + [v2.0] Walk-Forward 滚动回测框架
输入: 策略参数 + 数据 + 起始/结束日期
输出: 每日净值序列 + 交易记录 + 持仓记录 + [v2.0] WF路径集合

核心循环 (每个交易日):
  1. 检查是否为调仓日 (每月第一个交易日)
  2. 如果是调仓日:
     a. 获取当日 universe
     b. 获取因子综合得分
     c. 选 Top N 目标持仓
     d. 生成订单
     e. 执行订单（建模 T+1/涨跌停/手数/成本）
     f. 更新持仓
  3. 估值（按收盘价计算当日持仓市值）
  4. 记录每日净值
  5. 检查风控信号（择时/熔断）

关键约束:
  - 所有信号基于 t-1 数据计算（防前视偏差）
  - 成交价使用次日开盘价或当日 VWAP
  - 涨停买不到 / 跌停卖不掉 / 停牌跳过的 fallback
"""

@dataclass
class Order:
    ts_code: str
    side: str          # 'BUY' | 'SELL'
    quantity: int      # 股数
    price: float       # 委托价
    order_date: date

@dataclass
class Fill:
    order: Order
    fill_price: float  # 实际成交价
    fill_quantity: int
    commission: float
    stamp_tax: float
    fill_date: date

class BacktestEngine:
    def run(self, start_date, end_date) -> BacktestResult:
        """单次回测运行"""
        ...

class WalkForwardBacktest:  # [v2.0 新增]
    """Walk-Forward 滚动回测框架
    
    方法: 3年训练 → 1年验证 → 滚动前进
    产生多个样本外路径，用于 PBO/DSR 计算
    
    来源: Joubert et al. (2024) + Lopez de Prado (2018)
    """
    def run(self, start_date, end_date,
            train_years=3, test_years=1) -> list[BacktestResult]:
        ...

    def compute_pbo(self, results: list[BacktestResult],
                    n_trials: int = 1000) -> float:
        """PBO (Probability of Backtest Overfitting)
        使用 pypbo 库计算过拟合概率。目标 PBO < 30%
        """
        ...

    def compute_dsr(self, results: list[BacktestResult]) -> float:
        """DSR (Deflated Sharpe Ratio)
        测试夏普比率的统计显著性。目标 DSR > 1.0
        """
        ...

class BacktestResult:
    daily_nav: pl.DataFrame      # 每日净值
    trades: pl.DataFrame          # 交易记录
    holdings: pl.DataFrame        # 持仓快照
    metrics: dict                 # 绩效指标
```

### 4.6.1 回测引擎状态机

```
═══════════════════════════════════════════════════
状态定义（与 v1.3 基本一致，增加 v2.0 参数引用）
═══════════════════════════════════════════════════

Portfolio（持仓状态）:
  cash: float                    # 可用现金
  positions: dict[str, Position] # ts_code → Position
  total_value: float             # 现金 + 持仓市值

Position（单只持仓）:
  ts_code: str
  shares: int                    # 持有股数（整数，100 的倍数）
  avg_cost: float                # 加权平均成本
  market_value: float            # 当前市值 = shares × close_price

Order（订单 — 待执行）:
  ts_code: str
  side: 'BUY' | 'SELL'
  quantity: int                  # 目标股数（100 的倍数）
  order_date: date               # 下单日 = 调仓日 D
  status: 'PENDING' | 'FILLED' | 'CANCELLED'

Fill（成交 — 已执行）:
  order: Order
  fill_price: float              # D+1 开盘价（或 VWAP）
  fill_quantity: int             # 实际成交股数（≤ quantity）
  commission: float              # 佣金
  stamp_tax: float               # 印花税（仅卖出）
  transfer_fee: float            # 过户费（仅沪市）
  fill_date: date                # D+1

═══════════════════════════════════════════════════
Walk-Forward 滚动窗口 [v2.0]
═══════════════════════════════════════════════════

完整回测区间: 2015-01-01 至 2025-12-31

窗口 1: 训练 2015-01 至 2017-12 | 验证 2018-01 至 2018-12
窗口 2: 训练 2016-01 至 2018-12 | 验证 2019-01 至 2019-12
窗口 3: 训练 2017-01 至 2019-12 | 验证 2020-01 至 2020-12
窗口 4: 训练 2018-01 至 2020-12 | 验证 2021-01 至 2021-12
窗口 5: 训练 2019-01 至 2021-12 | 验证 2022-01 至 2022-12
窗口 6: 训练 2020-01 至 2022-12 | 验证 2023-01 至 2023-12
窗口 7: 训练 2021-01 至 2023-12 | 验证 2024-01 至 2024-12
窗口 8: 训练 2022-01 至 2024-12 | 验证 2025-01 至 2025-12

每个窗口独立计算：
  - 因子预热期: 训练窗口前 6 个月（数据积累）
  - 首个有效调仓日: 训练窗口第 7 个月
  - PBO 输入: 8 条样本外路径的夏普比率序列

═══════════════════════════════════════════════════
生命周期
═══════════════════════════════════════════════════

每个交易日 t:
  ┌─ 非调仓日 ─────────────────────────────┐
  │ • 更新持仓市值（收盘价）                 │
  │ • 检查风控信号（择时 + 熔断）            │
  │ • 记录日净值                            │
  └─────────────────────────────────────────┘

  ┌─ 调仓日 D ──────────────────────────────┐
  │ 1. 收盘后计算 factor_scores（基于≤D 数据）│
  │ 2. 选 Top N → 生成 Order 列表            │
  │    Order.status = 'PENDING'              │
  │ 3. 等待 D+1 开盘                         │
  └─────────────────────────────────────────┘

  ┌─ 调仓次日 D+1 ──────────────────────────┐
  │ 1. 按先卖后买顺序处理 Order              │
  │ 2. 检查涨跌停/停牌 → 未成交 → CANCELLED  │
  │ 3. 成交 → Fill.status = 'FILLED'         │
  │    • 卖单: cash += 成交金额 - 费用         │
  │    • 买单: cash -= 成交金额 + 费用         │
  │ 4. 更新 Position（新增/增持/减持/清仓）   │
  │ 5. 买单未成交: 资金分配给下一排名股票      │
  │    卖单未成交: 次日重试（最多重试 5 日）   │
  └─────────────────────────────────────────┘

═══════════════════════════════════════════════════
数据表 Schema (BacktestResult)
═══════════════════════════════════════════════════

daily_nav (每日净值):
  date, nav, cash, equity, position_count,
  benchmark_nav, excess_return, drawdown

trades (交易记录):
  fill_date, ts_code, side, quantity, fill_price,
  amount, commission, stamp_tax, transfer_fee

holdings (持仓快照，每日):
  date, ts_code, shares, weight, market_value
```

### 4.7 risk.py — 风控模块 [v2.0 MA20 + 恢复迟滞 + 15%熔断]

```python
"""
功能: 仓位择时 + 熔断检查
输入: 策略净值 + 基准净值 + 市场行情
输出: 仓位调整信号 + 熔断状态

三层风控:
  1. 组合层 (在 portfolio.py 中实现)
  2. 市场择时: [v2.0] 双条件 (中证500<MA20 AND σ_20d>σ_median_60d AND 连续5日确认) → 仓位降至50%
     恢复: 条件A不成立 AND 连续3日确认 → 满仓（v2.0 加入恢复迟滞）
  3. 熔断: alpha 回撤 / 硬熔断 [v2.0 15%]
"""

class RiskManager:
    def check_market_timing(self, index_close: pl.Series,
                            ma_period: int = 20,          # [v2.0] 60→20
                            vol_window: int = 20,
                            vol_percentile: float = 0.50,
                            hysteresis_down: int = 5,
                            hysteresis_up: int = 3) -> float:  # [v2.0 新增]
        """[v2.0] 双条件市场择时（参数校准版）:
        条件A: 中证500 < MA20  （[v2.0] MA60→MA20，长城证券2016）
        条件B: σ_20d > σ_median_60d（波动率高于历史中位数）
        降仓 = A AND B AND 连续 hysteresis_down 日确认 → 0.5
        恢复 = 非A AND 连续 hysteresis_up 日确认 → 1.0（[v2.0] 加入迟滞）
        
        来源: 长城证券(2016) + 国信证券(2018) + 华泰金工(2026)
        """
        ...

    def check_circuit_breaker(self, strategy_nav, benchmark_nav) -> str:
        """
        返回: 'NORMAL' | 'WARNING' | 'STOP'
        - 连续3月超额 < -3% → WARNING
        - 6月累计超额 < -10% → STOP
        - [v2.0] 绝对回撤 > 15% → STOP（原25%，小账户保护）
        """
        ...
```

### 4.8 performance.py — 绩效分析

```python
"""
功能: 收益归因 + 指标计算
输出指标:
  - 年化收益率 / 年化超额收益率
  - 年化波动率 / 跟踪误差
  - Sharpe Ratio / Information Ratio
  - 最大回撤 / 回撤持续时间 / 恢复时间
  - 月度胜率 / 年度收益
  - 换手率统计
  - 因子归因 (各因子贡献)
  - [v2.0] Walk-Forward 样本外路径统计
"""

class PerformanceAnalyzer:
    def compute_metrics(self, result: BacktestResult, benchmark_nav) -> dict:
        ...

    def factor_attribution(self, result, factor_scores) -> pl.DataFrame:
        """分解收益来源到各因子"""
        ...
    
    # [v2.0 新增]
    def compute_walk_forward_summary(self, wf_results: list[BacktestResult]) -> pl.DataFrame:
        """汇总所有 Walk-Forward 路径的样本外指标"""
        ...
```

---

## 五、关键实现细节与边界条件

### 5.1 防前视偏差（Look-Ahead Bias）检查清单

代码实现时必须强制执行的规则：

```
□ 因子计算: 所有 t 日因子值使用 ≤ t-1 日的数据
□ 调仓信号: t 日收盘后计算 → t+1 日执行 → t+1 日开盘价成交
□ 财务数据: ann_date ≤ 当前交易日（不是 end_date）
□ 复权因子: 使用当日已知的 adj_factor（不是修正后的）
□ 成分股: 使用当时实际的指数成分（不是当前成分回看）
□ 退市股: 退市日期后不可交易，持仓被迫清算
□ [v2.0] IC 衰减权重: 仅使用 t 日之前已知的 IC 数据
```

### 5.2 T+1 制度建模

```python
# 调仓日 D 的执行时序
# D 日 15:00 收盘 → 计算因子得分 → 选出新持仓 → 生成订单
# D+1 日 09:30 开盘 → 执行订单（以开盘价成交）
# D+1 日 15:00 → 新持仓生效

# 先卖后买: 
# D+1 日卖出所得资金，当日可用于买入（A 股规则：资金 T+0 可用，T+1 可取）
# 先执行所有卖单 → 计算可用资金 → 再执行买单
```

### 5.3 涨跌停与停牌处理

```python
def execute_orders(orders, market_data):
    unfilled = []
    for order in orders:
        if order.side == 'BUY':
            # 检查是否涨停
            if market_data[order.ts_code].is_limit_up():
                unfilled.append(order)
                continue
        elif order.side == 'SELL':
            # 检查是否跌停
            if market_data[order.ts_code].is_limit_down():
                unfilled.append(order)  # 次日重试
                continue
            # 检查是否停牌
            if market_data[order.ts_code].is_suspended():
                unfilled.append(order)
                continue
        # 正常成交
        fill = execute(order, market_data)
    
    # 未成交的买单 → 资金分配给下一只排名最高的可选股票
    # 未成交的卖单 → 继续持有，次日再尝试
```

### 5.4 手数约束处理

```python
def calculate_lot_size(target_value, price, lot=100):
    """
    计算最接近目标金额的手数
    返回 (手数, 实际金额, 权重偏差)
    [v2.0] 科创板 lot=200（但本策略不主动配置非指数成分科创板）
    """
    target_shares = target_value / price
    lots = round(target_shares / lot)
    if lots == 0:
        lots = 1  # 至少买 1 手（如果价格允许）
    actual_shares = lots * lot
    actual_value = actual_shares * price
    deviation = (actual_value - target_value) / target_value
    return lots, actual_value, deviation
```

### 5.5 行业分类缺失处理

部分股票可能没有申万行业分类数据。处理策略:
1. 优先使用 `SW2021` 分类
2. 缺失时回退到 `SW`（旧版 28 行业）
3. 仍缺失 → 标记为"其他"行业（不计入行业集中度限制，但最多持有 1 只"其他"股）
4. **[v2.0] 行业感知去极值**：缺少行业分类的股票回退到全市场去极值

### [v2.0] 5.5a BP+ 复合估值因子 — 金融股特殊处理

```python
def compute_bp_plus(df, industry_map):
    """
    BP+ 复合估值 = (z_bp + z_ep + z_cfp) / 3
    金融股（银行+非银金融）→ 仅用 (z_ep + z_cfp) / 2
    
    原因: 金融股 PB<1 不代表低估（高杠杆行业的特性）
    来源: 安信证券(2024)
    """
    is_financial = industry_map['l1_name'].isin(['银行', '非银金融'])
    # 正常股: 三因子等权
    # 金融股: 两因子等权，BP 权重=0
```

### 5.6 预热期与首个有效调仓日

```
═══════════════════════════════════════════════════
回测预热期定义
═══════════════════════════════════════════════════

数据起始日期: 2015-01-01
因子预热需求:
  - 残差动量: 需要 126 个交易日历史收盘价
  - 低波 IV60: 需要 60 个交易日历史日收益
  - BP+/ROE/Size: 无预热需求（截面数据）
  - [v2.0] VMP 缩放: 需要 60 个交易日因子日收益
  - [v2.0] IC 衰减权重: 需要 24 个月 IC 历史

最严格需求 = max(126, 60, 60) = 126 个交易日 + 24月IC历史
2015年1-6月约 120 个交易日
首个有效调仓日 = 2017-01-01（确保24月IC数据 + 126日价格数据）

实际预热:
  - 2015-01-01 至 2016-12-31: 仅积累数据 + 计算IC序列，不执行调仓
  - 2017-01-01 起: 正常月度调仓
  - 初始持仓: 全部持有现金（100,000 元）

Walk-Forward 窗口（[v2.0]）:
  训练窗口 3 年 × 滚动 8 次 × 回测执行
  每个窗口独立预热（窗口起始前 6 月 + 前 24 月 IC 历史）

═══════════════════════════════════════════════════
```

### 5.7 交易日历处理

```
交易日历来源: Tushare trade_cal API
  exchange='SSE', start_date='20150101', end_date='20251231'
  is_open=1 → 交易日

处理策略:
  ├─ 数据下载: 仅对交易日调用 pro.daily()
  │   → 非交易日自动跳过，不消耗 API 配额
  │
  ├─ 回测循环: 仅遍历交易日
  │   → 跳过周末和节假日
  │
  ├─ 调仓日检测: 每月第一个交易日
  │   → 基于交易日历计算，非自然月第一天
  │
  ├─ 数据校验: 交易日历有但 parquet 无 → 缺失数据
  │   → 标记缺失日期，增量下载补全
  │
  └─ 复权因子下载: 仅对交易日
      → 与日线数据对齐

Tushare trade_cal 字段:
  cal_date, is_open, pretrade_date
```

### 5.8 错误处理与异常恢复策略 [v2.0 扩展]

#### API 调用层

```
Tushare API 调用异常:
  ├─ ConnectionError / Timeout
  │   → 指数退避重试: 1s → 2s → 4s → 8s（最多 4 次）
  │   → 4 次均失败 → 抛出 DataDownloadError，记录已下载进度
  │
  ├─ RateLimitError（频率超限）
  │   → sleep(60) 后重试
  │   → 连续 3 次触发 → 暂停 10 分钟后继续
  │
  └─ 数据为空（交易日无数据，如节假日）
      → 跳过，记录 WARNING 日志

[v2.0] BaoStock API 调用异常:
  ├─ 连接失败
  │   → 降级为 "无交叉验证" 模式，不阻断主流程
  │   → 记录 WARNING: "BaoStock 交叉验证不可用"
  │
  └─ 数据缺失（某些日期/股票）
      → 仅在有数据的 (ts_code, trade_date) 上进行交叉验证
      → 覆盖率 < 80% → WARNING
```

#### 数据质量层

```
数据校验异常:
  ├─ 缺失日期（trading_calendar 有但 parquet 无）
  │   → 标记缺失日期，增量下载补全
  │
  ├─ 异常值检测（价格日涨跌幅 > 11% 且非新股首日）
  │   → 标记为可疑，保留但记录 ERROR 日志
  │
  ├─ 复权因子跳跃（单日变化 > 50%）
  │   → 标记，交叉验证是否为除权除息日
  │
  ├─ [v2.0] Tushare vs BaoStock 日收益相关性 < 0.95
  │   → 标记异常日期，人工复核
  │
  └─ 退市股数据超过 delist_date
      → 丢弃超出部分，记录 WARNING
```

#### 计算层

```
因子计算异常:
  ├─ PE_TTM = 0 或 NaN
  │   → EP 设为 NaN，该股票在标准化前排除
  │
  ├─ [v2.0] PB = 0 或 NaN（同上）
  │   → BP 设为 NaN
  │
  ├─ [v2.0] CFP 数据缺失（经营活动现金流不可得）
  │   → 仅用 BP+EP 两因子平均（非金融股也允许）
  │
  ├─ ROE 数据缺失（该报告期末公告）
  │   → 向前填充最近可用值（最多填充 2 个季度），仍缺失则排除
  │
  ├─ [v2.0] 残差动量回归样本不足（截面 < 500 只股票）
  │   → 回退到原始动量值（并标记 WARNING）
  │
  ├─ [v2.0] 行业感知去极值时行业内样本 < 30
  │   → 回退到全市场去极值
  │
  ├─ IV60 计算时数据不足（< 60 个交易日）
  │   → IV60=NaN（该股票在本调仓日排除）
  │
  ├─ [v2.0] VMP 缩放: σ_i_60d = 0（停牌期间）
  │   → w_vmp_i = 2.0（上限，不缩放）
  │
  └─ [v2.0] IC 衰减权重: IC 序列不足 12 个月
      → 等权（所有 IC_weight_i = 0.2）
```

#### 回测层

```
回测异常:
  ├─ 调仓日无可选股票（universe 为空，极端熊市）
  │   → 全部持有现金，记录 CRITICAL 日志
  │
  ├─ 买单资金不足（手数取整后超出预算）
  │   → 少买 1 手，记录 WARNING
  │
  ├─ 卖单持仓不足（数据一致性问题）
  │   → 卖出全部持仓，记录 ERROR → 需人工排查
  │
  ├─ [v2.0] Walk-Forward 单个窗口失败（如数据不足）
  │   → 跳过该窗口，记录 ERROR → 不影响其他窗口
  │   → 有效窗口数 < 3 → 抛出 InsufficientDataError
  │
  └─ 熔断触发
      → 全部清仓，暂停策略，等待人工复核
```

---

## 六、实施顺序与依赖关系

### Phase 1: 数据基础层（第 1-2 周）

#### Week 1: 项目骨架 + 数据下载

| 序号 | 任务 | 产出 | 依赖 | 预估耗时 |
|------|------|------|------|---------|
| 1.1 | 创建项目目录结构 | 完整目录 + .gitignore | 无 | 30 min |
| 1.2 | 创建 `requirements.txt` [v2.0 更新] | 依赖清单（+baostock, pypbo） | 无 | 15 min |
| 1.3 | 创建 `config/settings.toml` + 模板 [v2.0] | 配置文件（含v2.0新参数） | 无 | 20 min |
| 1.4 | 实现 `config.py` [v2.0 更新] | 配置加载器（FactorConfig/BacktestConfig/RiskConfig v2.0） | 1.3 | 30 min |
| 1.5 | 实现 `data_loader.py` 基础框架 | DataLoader 类 | 1.4 | 1 h |
| 1.6 | 实现股票列表下载 | `download_stock_list()` | 1.5 | 30 min |
| 1.7 | 实现按交易日批量下载日线 | `download_daily_batch()` | 1.5 | 1.5 h |
| 1.8 | 运行首次数据下载（2015-2025） | 本地 parquet 数据 | 1.6, 1.7 | 运行时间 ~15 min |
| 1.9 | 实现 `download_daily_basic()` | daily_basic 数据 | 1.5 | 45 min |
| 1.10 | 实现 `download_fina_indicator()` | 财务指标数据（[v2.0] +goodwill/total_equity/audit_opinion） | 1.5 | 45 min |
| 1.11 | 实现 `download_sw_classification()` | 行业分类数据 | 1.5 | 30 min |
| 1.12 | 实现 `download_index_daily()` | 中证 500 指数行情 | 1.5 | 20 min |
| **[v2.0] 1.12a** | **实现 `download_pledge_stat()`** | 股权质押数据 | 1.5 | 30 min |
| **[v2.0] 1.12b** | **实现 `download_baostock_daily()`** | BaoStock 日线交叉验证 | 1.5 | 45 min |

#### Week 2: 数据校验 + 结构化日志

| 序号 | 任务 | 产出 | 依赖 | 预估耗时 |
|------|------|------|------|---------|
| 1.13 | 实现日志系统（structlog） | JSON + 控制台日志 | 1.1 | 30 min |
| 1.14 | 实现 `validate_data()` [v2.0 扩展] | 数据校验函数（含BaoStock交叉验证） | 1.8-1.12b | 1.5 h |
| 1.15 | 修复校验发现的数据问题 | 干净数据 | 1.14 | 视情况 |
| 1.16 | 实现 `load_daily()`（DuckDB 加载） | 高效数据加载 | 1.8 | 45 min |
| 1.17 | 复权因子下载与价格复权 | 复权价格 | 1.8 | 45 min |
| 1.18 | 实现 `update_daily()`（增量更新） | 增量更新能力 | 1.7 | 30 min |

> **Phase 1 验收标准**：2015-2025 全量数据下载到本地，数据校验全部通过（含 Tushare vs BaoStock 相关性 ≥ 0.99），`DataLoader.load_daily('20150101', '20251231')` 在 2 秒内完成加载。

### Phase 2: 因子研究期（第 3-4 周）

#### Week 3: 因子计算 + Universe [v2.0 重写]

| 序号 | 任务 | 产出 | 依赖 | 预估耗时 |
|------|------|------|------|---------|
| 2.1 | 实现 `universe.py` [v2.0 十层过滤] | 十层过滤（含审计意见/商誉/质押） | 1.16 | 3 h |
| 2.2 | 编写 `universe` 单元测试 | 验证过滤正确性 | 2.1 | 1.5 h |
| 2.3 | 实现 `factors.py` 原始因子计算 [v2.0] | BP+/残差MOM/ROE/IV60/ln Size 原始值 | 1.16, 1.17 | 3 h |
| 2.4 | 实现行业感知去极值 + 截面标准化 | winsorize（行业感知）+ z-score | 2.3 | 1.5 h |
| 2.5 | 实现全因子 VMP 缩放系数 | 5 因子的 σ_60d + w_vmp_i | 2.3 | 1 h |
| 2.6 | 实现 IC 衰减引导权重 | 24月滚动 IC 衰减权重 | 2.3 | 1.5 h |
| 2.7 | 实现三维修正等权合成 | composite score | 2.4-2.6 | 1 h |
| 2.8 | 编写 `factors` 单元测试 [v2.0] | 验证所有新因子 + VMP + IC 权重 | 2.3-2.7 | 2 h |

#### Week 4: 因子研究 Notebook

| 序号 | 任务 | 产出 | 依赖 | 预估耗时 |
|------|------|------|------|---------|
| 2.9 | 单因子 Rank IC 分析 [v2.0 扩展] | 5+1 因子 IC 表格 + 图表 | 2.7 | 2 h |
| 2.10 | 单因子分层回测（5 组） | 分组收益柱状图 | 2.9 | 1.5 h |
| 2.11 | 因子间相关性矩阵 | 相关热力图 | 2.7 | 1 h |
| 2.12 | 牛/熊/震荡市因子表现对比 | 分市场环境 IC | 2.10 | 1.5 h |
| 2.13 | IC 衰减曲线分析 | 各因子 24 月 IC 滚动 + 衰减 | 2.9 | 1 h |
| 2.14 | [v2.0] 短期反转因子（实验性）IC 分析 | 判定是否纳入 v2.1 | 2.9 | 1 h |

> **Phase 2 验收标准**：5 个因子的 Rank IC 均值 > 0.02（历史数据），分层回测 Top 组 > Bottom 组，因子相关性 < 0.6。BP+ 复合因子的多空收益 > EP 单独因子。残差动量 ICIR 转正（相对原始动量）。

### Phase 3: 回测验证期（第 5-8 周）

#### Week 5-6: 回测引擎

| 序号 | 任务 | 产出 | 依赖 | 预估耗时 |
|------|------|------|------|---------|
| 3.1 | 实现 `portfolio.py` 选股与订单生成 [v2.0] | 目标持仓 → 订单列表（+科创板约束） | 2.1, 2.7 | 2 h |
| 3.2 | 实现 `backtest.py` 事件循环 | 核心回测逻辑 | 3.1 | 3 h |
| 3.3 | 实现交易成本建模 | 佣金/印花税/过户费/滑点 | 3.2 | 1 h |
| 3.4 | 实现 T+1/涨跌停/停牌约束 | 真实执行约束 | 3.2 | 1.5 h |
| 3.5 | 实现 `risk.py` 仓位择时 [v2.0] | 双条件信号（MA20 + σ过滤 + 降仓/恢复双向迟滞） | 3.2 | 1.5 h |
| 3.6 | 实现 `risk.py` 熔断逻辑 [v2.0] | 三层熔断（15%硬熔断） | 3.2 | 1 h |
| 3.7 | 编写回测引擎单元测试 | 已知场景验证 | 3.2-3.6 | 2 h |
| **[v2.0] 3.7a** | **实现 `validation.py`（PBO + DSR）** | Walk-Forward 框架 + PBO/DSR 计算 | 3.2 | 2 h |

#### Week 7-8: 回测执行 + 分析

| 序号 | 任务 | 产出 | 依赖 | 预估耗时 |
|------|------|------|------|---------|
| 3.8 | Walk-Forward 滚动回测（8窗口） | 8条样本外路径 + 汇总指标 | 3.6, 3.7a | 运行时间 ~30 min |
| 3.9 | PBO + DSR 统计检验 | 过拟合概率 + 夏普显著性 | 3.7a, 3.8 | 运行时间 ~10 min |
| 3.10 | 实现 `performance.py` [v2.0 扩展] | 绩效指标 + 因子归因 + WF汇总 | 3.8 | 2 h |
| 3.11 | 参数敏感性分析 [v2.0 扩展] | σ_target∈{0.10,0.15,0.20}, VMP上限∈{1.5,2.0,2.5}, IC窗口∈{12,24,36}, MA∈{15,20,30}, hysteresis_up∈{0,2,3,5} | 3.8 | 3 h |
| **[v2.0] 3.11a** | **v1.3 vs v2.0 完整对比回测** | 相同数据、相同区间，原版 vs 优化版全指标对比 | 3.8-3.9 | 2 h |
| 3.12 | 实现 `report.py` + 生成回测报告 | HTML 报告 | 3.10 | 2 h |
| 3.13 | 分年度收益分析 | 逐年收益表 | 3.10 | 1 h |

> **Phase 3 验收标准**：
> - Walk-Forward 样本外路径中 ≥ 80% 有正超额（相对中证 500）
> - PBO < 30%
> - DSR > 1.0
> - v2.0 全周期超额 ≥ v1.3（不劣化）
> - 关键参数 ±20% 变化后超额变化 < 25%
> - 振荡市（2016, 2021）降仓信号减少 ≥ 40%
> - 动量因子最大月回撤降低 ≥ 25%

---

## 七、测试策略

### 7.1 单元测试覆盖矩阵

| 模块 | 测试要点 | 优先级 |
|------|---------|:------:|
| `config.py` | 默认值合并、缺失字段报错、token 读取、**[v2.0] 新参数默认值验证** | P1 |
| `data_loader.py` | 股票列表去重、退市股包含、日期范围正确、增量更新不重复、**[v2.0] BaoStock交叉验证相关≥0.99、pledge_stat数据覆盖>4000只** | P1 |
| `universe.py` | **[v2.0] 十层过滤**每层数量递减、ST排除、**[v2.0] 科创板仅指数成分股纳入**、PE<0排除、**[v2.0] 审计意见排除、商誉>30%排除、质押≥50%排除** | P1 |
| `factors.py` | **[v2.0] BP+复合估值（含金融股特殊处理）**、**[v2.0] 残差动量截面回归（残差≠原始动量）**、ROE Point-in-Time、**[v2.0] 行业感知去极值（行业内winsorize）**、标准化后均值≈0 std≈1、**[v2.0] 全因子VMP缩放（w_vmp_i≤2.0）**、**[v2.0] IC衰减权重（24月窗口+半衰衰减+±50%安全阀）**、**[v2.0] 三维修正合成公式验证** | P1 |
| `portfolio.py` | 10 只选出、行业 ≤30%、手数取整、资金不超限、**[v2.0] 科创板持仓≤2/10** | P1 |
| `backtest.py` | 无前视偏差（用 shift 验证）、T+1 成交日正确、交易成本扣减正确、**[v2.0] Walk-Forward 窗口滚动正确（训练/验证无重叠）** | P1 |
| `risk.py` | 双条件同时成立才降仓、单一条件不降仓、**[v2.0] MA20（非MA60）**、降仓迟滞计数器正确累加/清零、**[v2.0] 恢复迟滞计数器（3日确认）**、**[v2.0] 15%硬熔断触发** | P2 |
| `validation.py` [v2.0] | **PBO 计算正确（pypbo库）、DSR > 1.0 判定、WF路径数≥3** | P2 |

### 7.2 集成测试

```python
def test_end_to_end():
    """
    端到端测试: 小数据集（2015 年，100 只股票采样）完整跑通
    验证: 无 crash、净值 > 0、交易记录完整、无 NaN 净值
    """
    ...

def test_no_lookahead():
    """
    前视偏差检测: 对每个交易日 t，验证因子计算只使用了 ≤ t 的数据
    方法: 在 t 日的数据快照上计算因子 → 与全量数据计算的 t 日因子比对
    [v2.0] 扩展: 验证 IC 衰减权重未使用未来 IC
    """
    ...

def test_cost_accounting():
    """
    成本核算验证: 已知交易序列，手动计算预期净值 → 与回测引擎输出对比
    """
    ...

def test_walk_forward_no_overlap():  # [v2.0 新增]
    """
    Walk-Forward 验证: 确保训练窗口和验证窗口无日期重叠
    验证: 训练期最大日期 < 验证期最小日期（每窗口）
    """
    ...

def test_bp_plus_financial_special_case():  # [v2.0 新增]
    """
    BP+ 金融股特殊处理: 银行/非银金融仅用 EP+CFP 两因子
    验证: 金融股的 z_bp 权重 = 0
    """
    ...
```

### 7.3 验收测试用例（精确输入 → 预期输出）[v2.0 更新]

#### 用例 1: BP+ 复合估值因子

```python
def test_bp_plus_composite():
    """验证 BP+ = (z_bp + z_ep + z_cfp) / 3"""
    calc = FactorCalculator()
    
    # 正常股票: 三因子等权
    result = calc.compute_bp_plus(bp=0.5, ep=0.02, cfp=0.03, is_financial=False)
    # z_bp + z_ep + z_cfp 各截面标准化后取平均
    assert result is not None
    
    # 金融股: 仅 EP + CFP
    result_fin = calc.compute_bp_plus(bp=0.3, ep=0.02, cfp=0.01, is_financial=True)
    assert result_fin is not None  # BP 不参与

def test_residual_momentum_sign():
    """验证残差动量 ≠ 原始动量（残差化后方向可能反转）"""
    calc = FactorCalculator()
    raw_mom = pl.Series([0.10, 0.05, -0.03, 0.15])
    residual = calc.compute_residual_momentum(raw_mom, market_ret=0.02, industry_dummies)
    # 残差化后，与原始动量相关性 < 1
    assert abs(residual.corr(raw_mom)) < 0.99
```

#### 用例 2: 全因子 VMP 缩放边界条件

```python
def test_vmp_all_factor_scaling():
    """验证全5因子 VMP 缩放三种边界条件"""
    calc = FactorCalculator()

    # Case A: 低波动 → w_vmp = upper_bound (2.0)
    low_vol_returns = pl.Series([0.0001]*60)
    sigma = calc.compute_factor_volatility(low_vol_returns)
    w = calc._compute_w_vmp(sigma, sigma_target=0.15, upper_bound=2.0)
    assert w == 2.0  # σ ≤ 0.075 → 上限

    # Case B: 中等波动 → w_vmp ≈ 1.0
    sigma_mid = 0.15  # 年化15%
    w = calc._compute_w_vmp(sigma_mid, sigma_target=0.15, upper_bound=2.0)
    assert abs(w - 1.0) < 0.01

    # Case C: 高波动 → w_vmp < 1.0
    sigma_high = 0.30  # 年化30%
    w = calc._compute_w_vmp(sigma_high, sigma_target=0.15, upper_bound=2.0)
    assert abs(w - 0.5) < 0.01

    # Case D: σ=0（停牌）→ w_vmp = 2.0（上限）
    w = calc._compute_w_vmp(0.0, sigma_target=0.15, upper_bound=2.0)
    assert w == 2.0

def test_ic_weight_safety_valve():
    """验证 IC 权重在等权±50%范围内"""
    calc = FactorCalculator()
    
    # 极端场景: 某因子 RankIC 长期为负
    ic_history = pl.DataFrame({
        'date': [...],
        'value': [-0.05]*24, 'roe': [0.02]*24,
        'mom': [0.03]*24, 'iv60': [0.02]*24, 'size': [0.01]*24,
    })
    weights = calc.compute_ic_decay_weights(ic_history)
    
    # 每个权重应在等权基线(0.2)±50%范围内 = [0.1, 0.3]
    for w in weights:
        assert 0.10 <= w <= 0.30
```

#### 用例 3: 双条件择时 [v2.0 更新]

```python
def test_dual_condition_timing_v2():
    """验证 MA20 + 恢复迟滞"""
    rm = RiskManager()

    # 仅条件A（跌破MA20但波动正常）→ 不降仓
    signal = rm.check_market_timing(
        index_close=below_ma20_prices,
        ma_period=20, vol_window=20, vol_percentile=0.5,
        hysteresis_down=5, hysteresis_up=3
    )
    assert signal == 1.0  # 满仓

    # A AND B → 连续5日 → 降仓
    for _ in range(5):
        signal = rm.check_market_timing(index_close=below_ma20_high_vol_prices)
    assert signal == 0.5

    # 条件A恢复 → 需连续3日确认才满仓 [v2.0 变更]
    signal = rm.check_market_timing(index_close=above_ma20_prices)  # 仅1日
    assert signal == 0.5  # 仍降仓（未达3日恢复迟滞）
    
    for _ in range(3):
        signal = rm.check_market_timing(index_close=above_ma20_prices)
    assert signal == 1.0  # 3日确认 → 满仓
```

#### 用例 4: 前视偏差防护

```python
def test_no_lookahead_in_factor_calculation():
    """验证 t 日因子计算未使用 t+1 及以后的数据"""
    data_t = load_data_upto_date('2018-06-15')
    data_all = load_all_data()

    factors_t = FactorCalculator().compute_all_dates([date(2018,6,15)], data_t)
    factors_all = FactorCalculator().compute_all_dates([date(2018,6,15)], data_all)

    # t 日仅用 ≤t 数据计算的因子值 = 用全量数据计算的因子值
    for col in ['z_value', 'z_roe', 'z_res_mom', 'z_iv60', 'z_size']:
        assert (factors_t[col] - factors_all[col]).abs().max() < 1e-10
```

---

## 八、风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| Tushare 服务中断 | 中 | 高 | 本地 Parquet 缓存；**[v2.0] BaoStock 作为备选数据源** |
| API 频率限制触发封禁 | 低 | 中 | `time.sleep(0.35)` 间隔 + 重试机制 + 断点续传 |
| 数据质量问题导致因子失真 | 中 | 高 | Phase 1 强校验 + 异常值检测 + Winsorize + **[v2.0] 双源交叉验证** |
| 回测结果与实盘差距大 | 中 | 高 | 纸面交易期（Phase 4）+ 保守成本建模 + 滑点预留 + **[v2.0] Walk-Forward+PBO+DSR 过拟合检测** |
| 用户无法运行 Python 脚本 | 高 | 中 | 提供详细 README + 一键运行脚本 + Jupyter Notebook 交互 |
| 因子系统性衰减 | 中 | 高 | Phase 5 因子监控面板 + 人工复核机制 + **[v2.0] IC 衰减权重自动降低失效因子暴露** |
| **[v2.0] IC 权重过拟合** | 中 | 中 | ±50% 安全阀 + Phase 3 敏感性测试 12/24/36 月窗口 |
| **[v2.0] 残差动量实施复杂度** | 低-中 | 中 | 使用 SW2021 Level-1（31行业）+ 回退到原始动量机制 |
| **[v2.0] MA20 振荡市假信号增加** | 中 | 低 | 双条件机制（需条件B同时成立）应过滤大部分假信号 |
| **[v2.0] BaoStock 复权差异** | 低 | 低 | 仅用于交叉验证（相关系数），不影响主策略 |

---

## 九、Python 依赖清单 [v2.0 更新]

```
# requirements.txt
# 核心数据
tushare>=1.4.26
baostock>=0.8.0            # [v2.0] 免费备选数据源
polars>=1.0.0
duckdb>=1.1.0
pyarrow>=15.0.0

# 配置与日志
structlog>=24.0.0

# 数值计算
numpy>=1.26.0
scipy>=1.13.0

# [v2.0] 统计检验
pypbo>=0.4.0               # PBO (Probability of Backtest Overfitting)

# 可视化与报告
matplotlib>=3.8.0
seaborn>=0.13.0
tabulate>=0.9.0

# 开发工具
python-dotenv>=1.0.0
ipykernel>=6.29.0
jupyter>=1.0.0

# 测试
pytest>=8.0.0
pytest-cov>=5.0.0
```

---

## 十、环境搭建与下一步行动

### 10.1 本地环境搭建（首次运行）

```bash
# 1. 创建 Python 虚拟环境
python3.12 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. 创建配置文件
cp config/settings.template.toml config/settings.toml
# 编辑 config/settings.toml，填入 Tushare token（从 https://tushare.pro 获取）

# 4. 验证 Tushare 连接
python -c "
import tushare as ts
pro = ts.pro_api('YOUR_TOKEN_HERE')
df = pro.stock_basic(list_status='L')
print(f'Tushare OK — {len(df)} 只上市股票')
"

# [v2.0] 5. 验证 BaoStock 连接
python -c "
import baostock as bs
lg = bs.login()
print(f'BaoStock login: {lg.error_code} {lg.error_msg}')
rs = bs.query_history_k_data_plus('sh.000001', 'date', start_date='2024-01-01')
print(f'BaoStock OK — sample rows: {len(rs.data)}')
bs.logout()
"

# 6. 下载全量数据（约 20 分钟，含 BaoStock）
python -m core.data_loader --download-all

# 7. 运行测试
pytest
```

### 10.2 .gitignore 模板

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/

# 敏感配置
.env
config/settings.toml

# 数据（体积大，本地保留）
data/

# IDE
.vscode/
.idea/

# Jupyter
.ipynb_checkpoints/

# 日志与输出
logs/
output/reports/
```

### 10.3 开始开发

1. **确认 Tushare token 可用 + BaoStock 连接正常** — 按 10.1 步骤 4-5 执行
2. **创建项目目录结构** — 按 §2.3 执行
3. **配置 settings.toml** — 按 §4.1.1 v2.0 模板填入参数
4. **开始 coding** — 按 §六 Phase 1 顺序逐任务推进

---

## 参考

### 技术参考

| 来源 | 内容 |
|------|------|
| Tushare Pro 官方文档 | API 参数、频率限制、积分权限 |
| BaoStock 官方文档 | 免费 A 股日线 + 财务数据 API |
| DuckDB 官方博客 | Parquet 性能基准 |
| KDnuggets / Dev Genius | Polars vs Pandas 性能对比（2025） |
| codecentric.de | DuckDB vs Polars vs Pandas Parquet 基准 |
| TOML 官方规范 | Python tomllib 配置模式 |
| structlog 官方文档 | 结构化日志最佳实践 |
| pypbo 文档 | PBO 过拟合概率计算 |

### 策略参考（v2.0 学术来源）

| 来源 | 内容 | v2.0 应用 |
|------|------|---------|
| Barroso & Santa-Clara (2015), JFE | 动量崩溃风险管理——波动率缩放 | VMP 全扩展 |
| Wang & Li (2024), PBFJ | A股71因子VMP实证——夏普1.50 vs 等权1.12 | VMP 参数（σ_target=0.15） |
| Moreira & Muir (2017), JFE | 简化版VMP——前月已实现方差月频调仓 | VMP 回看窗口 60 日 |
| 安信/国投证券 (2024) | BP+复合估值 RankICIR 3.68, 多空年化 24.16% | BP+ 复合估值因子 |
| 华泰证券 (2024-2025) | A股改进残差动量年化超额 12.90% | 残差动量因子 |
| GF证券 (2024) | 多期限残差动量 RankIC -4.14%, 多头年化 20.13% | 残差动量因子 |
| BigQuant (2024) | 残差动量 3 月 ICIR = +0.15（正动量） | 残差动量 vs 原始动量 |
| 国泰君安 (2013) | FDO 动态因子优化 IR 2.38→2.93 | IC 引导权重 |
| 广发证券 (2018) | IC 加权超额 +3.7% vs 等权 | IC 引导权重 |
| 中信建投 (2019) | IC 半衰衰减加权 | IC 衰减方法 |
| 长城证券 (2016) | 中证 500 最优均线 = 20 日 | MA20 择时 |
| 国信证券 (2018) | 波动率过滤信号胜率 55.45% | 条件B 波动率过滤 |
| 华泰金工 (2026) | 拐点识别率 61.8%——恢复需确认机制 | 恢复迟滞 3 日 |
| 国金证券 (2025) | 外围择时风控：最大回撤 44.3%→11.82% | 15% 硬熔断 |
| 方正证券 (2024) | 股权质押风险量化框架 | Universe 质押过滤 |
| 长江证券 (2024) | 56 指标财务风险 logistic 模型 | Universe 审计/商誉过滤 |
| Joubert et al. (2024) | The Three Types of Backtests (Lopez de Prado et al.) | Walk-Forward + CPCV |
| Ping'an Securities (2024) | 因子拥挤度多维度指标 | Phase 5 监控 |
| 中信期货 (2021) | ES 动态仓位控制回测 2013-2021 | 风控参考 |
| 浙商证券 (2026) | 敞口自适应风控模型 2020-2026 | 风控参考 |
| 中金公司 (2024) | 量化策略超额回撤系统性分析 | 风控参考 |
| Campomanes (2024), Aalto University | bottom-up 等权优于优化权重 | 等权哲学 |

### 项目文档索引

| 文档 | 内容 |
|------|------|
| `docs/strategy-plan-v2.md` v2.0 | 策略方案——做什么 |
| `docs/algorithm-workflow.md` v2.0 | 算法流图——怎么算 |
| `docs/tech-plan.md` v2.0 | 本文档——怎么实现 |
