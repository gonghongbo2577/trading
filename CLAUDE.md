# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Personal quantitative trading system for A-shares (Chinese stock market). A multi-factor stock selection strategy with monthly rebalancing, targeting 10 equal-weight positions from ~10万 RMB capital.

- **Strategy**: 5-factor composite (BP+ Value, ROE, Residual Momentum, Low Volatility IV60, Size), with **VMP all-factor scaling + IC decay-guided weights** (v2.0)
- **Strategy version**: v2.0 (see `docs/strategy-plan-v2.md` for full history)
- **Benchmark**: CSI 500 (中证500) primary + CSI 1000 (中证1000) secondary
- **Data source**: Tushare Pro (2120 points tier) primary + BaoStock (free, cross-validation backup)
- **Status**: Planning phase — no code written yet

## Tech Stack

| Layer | Choice | Version |
|-------|--------|---------|
| Language | Python | **3.12** (3.13 incompatible with statsmodels) |
| Data storage | Parquet + Zstd compression | — |
| Data ETL | DuckDB | ≥1.1 |
| Factor computation | Polars | ≥1.0 |
| Backtest engine | Custom event-driven | — |
| Config | TOML (`tomllib`, stdlib) | — |
| Logging | structlog | ≥24.0 |
| Testing | pytest + pytest-cov | ≥8.0 |

## Architecture

```
config/settings.toml
       │
       ▼
data_loader.py  ← No dependencies, implement first
       │
  ┌────┴────┐
  ▼         ▼
universe.py  factors.py  ← Depend on data_loader
  └────┬────┘
       ▼
  backtest.py  ← Depends on factors + universe
       │
  ┌────┴────┐
  ▼    ▼    ▼
risk.py  performance.py  report.py  ← Depend on backtest
```

**Data flow**: Tushare API → data_loader (download + clean) → Parquet partitioned storage → DuckDB/Polars loading → factor calculation → backtest engine → risk/performance/report output.

## Planned Directory Structure

```
trading/
├── config/
│   ├── settings.toml          # Main config (token, capital, risk params)
│   └── settings.template.toml # Template without secrets (git-tracked)
├── data/
│   ├── raw/                   # Raw downloads partitioned by source
│   └── processed/factors/     # Cleaned factor values partitioned by date
├── core/
│   ├── config.py              # TOML config loader → Config dataclass
│   ├── data_loader.py         # Tushare + BaoStock download, local cache, incremental update
│   ├── universe.py            # [v2.0] 10-layer stock filtering → investable universe
│   ├── factors.py             # [v2.0] BP+/Residual MOM/ROE/IV60/ln Size + industry-aware winsorize + VMP all-factor scaling + IC decay weights
│   ├── backtest.py            # Event-driven backtest engine + Walk-Forward + PBO + DSR
│   ├── risk.py                # [v2.0] Dual-condition market timing (MA20 + volatility) + hysteresis + circuit breakers (15%)
│   ├── portfolio.py           # Top-N selection, industry check, order generation
│   ├── performance.py         # Return attribution, metrics calculation
│   └── report.py              # Report generation (charts + tables)
├── strategies/
│   └── multi_factor.toml      # Strategy parameter config
├── research/                  # Jupyter notebooks for factor research
├── tests/
│   ├── test_data_loader.py
│   ├── test_universe.py
│   ├── test_factors.py
│   ├── test_backtest.py
│   └── test_risk.py
├── output/reports/ + charts/
├── logs/
└── docs/
```

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest

# Run tests with coverage
pytest --cov=core --cov-report=term-missing

# Run a single test file
pytest tests/test_factors.py

# Run a single test function
pytest tests/test_factors.py::test_ep_factor_positive

# Run Jupyter for factor research
jupyter notebook research/

# Download initial data (once implemented)
python -m core.data_loader --download-all
```

## Critical Implementation Rules

These are hard constraints from the strategy/tech plans — violations produce incorrect results:

### Anti-Lookahead Bias (防前视偏差)
- **All signals at time t must use data ≤ t-1**. This is the #1 source of inflated backtest results.
- **Financial data uses `ann_date` (announcement date), NOT `end_date` (report period end)**. A Q1 report ending 3/31 isn't available until 4/30 — using it before `ann_date` is lookahead bias.
- **Rebalance timing**: signals computed after market close on day D → orders executed at D+1 open.
- **Index constituents**: use historical constituents, not current ones.
- **Delisted stocks**: no trading after `delist_date`; forced liquidation of holdings.

### Survivorship Bias
- **Download ALL stocks including delisted** (`list_status in ('L', 'D', 'P')`). Using only currently-listed stocks inflates backtest returns by 0.5-2% annually.

### Tushare API Constraints
- `pro_bar` queries ONE stock at a time. For bulk daily data, use `pro.daily(trade_date='YYYYMMDD')` which returns all stocks for a single date (~5000 rows per call).
- Rate limit: 200 calls/minute → use `time.sleep(0.35)` between calls, leave margin.
- 10 years ≈ 2450 trading days ≈ 2450 API calls ≈ ~14 minutes for full download.

### Data Validation (must implement)
- No duplicate (ts_code, trade_date) pairs
- No future dates in data
- No negative prices or zero-volume records
- Delisted stocks have no data after delist_date
- Adj_factor monotonically increasing (no gaps)
- fina_indicator: ann_date ≥ end_date

### Market Timing (v2.0 dual-condition, calibrated)
- **Dual-condition trigger** with calibrated parameters:
  - Condition A (trend): CSI 500 close < **MA20** (长城证券 2016: CSI 500 optimal MA = 20-day)
  - Condition B (volatility): 20-day annualized vol > 60-day median vol
  - Position reduction signal: A AND B AND confirmed for 5 consecutive days → reduce to 50%
  - Recovery: exit condition A AND confirmed for **3 consecutive days** → restore 100% (华泰金工 2026: inflection recognition rate only 61.8%)
- This filters out ~70% of whipsaw signals in oscillating markets while preserving bear market protection.
- Sources: 长城证券 (2016) MA+BOLL有效性; 国信证券 (2018) 波动率过滤实证; 华泰金工 (2026) 拐点识别率
- New parameters: `ma_period=20`, `vol_window=20`, `vol_percentile=0.50`, `hysteresis_down=5`, `hysteresis_up=3`

### A-Share Market Mechanics
- **T+1 settlement**: buy today, can sell tomorrow. But sale proceeds are available same-day for buying (T+0 available, T+1 withdrawable).
- **Lot size**: 100 shares = 1手, indivisible. Round to nearest lot, handle cases where stock price exceeds single-position budget.
- **Price limits**: ±10% for most stocks (±5% for ST, ±20% for STAR Market). Limit-up → can't buy; limit-down → can't sell.
- **STAR Market (688xxx)**: CSI 300/500/1000 index constituents may be included (≤2/10 positions, 200-share lot). Non-index STAR Market stocks excluded.
- **Costs**: commission 万2.5 (min ¥5/trade), stamp tax 万5 (sell only), transfer fee 万0.1 (Shanghai only, both sides).

### Factor Computation
- **[v2.0] Factors: BP+ (composite value), ROE, Residual Momentum, IV60, Size (-ln)**
  - BP+ = (z_bp + z_ep + z_cfp) / 3 (financials: EP+CFP only). Source: 安信证券 (2024), RankICIR 3.68
  - Residual MOM = ε_i from cross-sectional regression r_i = α + β_mkt·r_mkt + Σβ_ind·I_ind + ε_i. Source: 华泰/GF (2024)
  - Size = -ln(total_mv) (was -log in v1.3)
- **[v2.0] All-factor VMP (Volatility-Managed Portfolio)**: Each of 5 factors has its own VMP scaling:
  - `w_vmp_i = min(2.0, 0.15 / σ_i_60d)` where 0.15 is the target annualized volatility
  - Upper bound 2.0 allows moderate leverage when volatility is unusually low
  - Source: Wang & Li (2024, PBFJ) A-share VMP: Sharpe 1.50 vs equal-weight 1.12
- **[v2.0] IC decay-guided weights**: 24-month rolling RankIC with exponential decay (half-life 6 months, factor-specific tuning in 4-12 month range)
  - IC_weight_i restricted to ±50% of equal-weight baseline ([0.1, 0.3])
  - Sources: 国泰君安 (2013) + 广发证券 (2018) + 中信建投 (2019)
- **[v2.0] Composite score**: `Σ(IC_weight_i × w_vmp_i × z_i) / Σ(IC_weight_i × w_vmp_i)`
- Cross-sectional standardization per date: **Industry-aware** Winsorize at 1%/99% (within SW Level-1 industries) → Z-score → ensure direction (higher = better).
- EP/BP only meaningful for PE_TTM > 0 stocks (negative PE excluded in universe filtering).
- Residual momentum: skip the most recent month (6-month return excluding last 1 month) plus market+industry neutralization.

## Implementation Phases

1. **Phase 1 (weeks 1-2)**: Data layer — project skeleton, Tushare + BaoStock downloader, Parquet storage, data validation, pledge_stat
2. **Phase 2 (weeks 3-4)**: Factor research — [v2.0] BP+, residual MOM, VMP all-factor, IC weights, 10-layer universe, IC analysis notebooks
3. **Phase 3 (weeks 5-8)**: Backtest — Walk-Forward engine, PBO/DSR, MA20 timing, 15% circuit breaker, v1.3 vs v2.0 comparison
4. **Phase 4 (weeks 9-12)**: Paper trading — daily signal recording, paper-vs-backtest comparison, BaoStock validation
5. **Phase 5 (week 13+)**: Live — 30% → 60% → 100% gradual capital deployment, GC001 idle cash, monthly attribution

## v2.0 Key Parameters

| Parameter | Value | Module | Source |
|-----------|-------|--------|--------|
| `sigma_target` | 0.15 (annualized) | `factors.py` | Wang & Li (2024) A-share VMP |
| `vmp_upper_bound` | 2.0 | `factors.py` | Wang & Li (2024) |
| `mom_lookback` | 60 trading days | `factors.py` | Moreira & Muir (2017) |
| `ic_lookback_months` | 24 | `factors.py` | 国泰君安 (2013) |
| `ic_half_life_months` | 6 (factor-specific 4-12) | `factors.py` | 中信建投 (2019) |
| `ic_weight_deviation_max` | 0.5 (±50% of baseline) | `factors.py` | v2.0 safety valve |
| `ma_period` | 20 | `risk.py` | 长城证券 (2016) |
| `vol_window` | 20 trading days | `risk.py` | 国信证券 (2018) |
| `vol_percentile` | 0.50 (median) | `risk.py` | 长城证券 (2016) |
| `hysteresis_down` | 5 trading days | `risk.py` | Industry practice |
| `hysteresis_up` | 3 trading days | `risk.py` | 华泰金工 (2026) |
| `hard_drawdown_limit` | -0.15 | `risk.py` | Small-account protection |

Total code delta: ~600 lines across `factors.py` (~200) + `risk.py` (~30) + `backtest.py` (~100) + tests (~120) + new modules (~150).

## References

- Strategy plan (v2.0): `docs/strategy-plan-v2.md`
- Technical plan (module interfaces, implementation order, test strategy v2.0): `docs/tech-plan.md`
- Algorithm workflow diagrams (9 Mermaid charts v2.0): `docs/algorithm-workflow.md`
- Expert review & roadshow (bull/bear/ranging analysis): `docs/expert-review.md`
- Structural risk optimization plan (v1.3 design rationale): `docs/optimization-plan.md`
- Tushare Pro API docs: https://tushare.pro/document/2
- BaoStock API docs: http://baostock.com
- pypbo (PBO computation): https://pypi.org/project/pypbo/
