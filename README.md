# 多因子量化选股系统 v2.0

Personal quantitative trading system for A-shares (Chinese stock market).

## Strategy

5-factor composite (BP+ Value, ROE, Residual Momentum, Low Volatility IV60, Size), with VMP all-factor scaling + IC decay-guided weights.

- **Benchmark**: CSI 500 (中证500) primary + CSI 1000 (中证1000) secondary
- **Data source**: Tushare Pro (2120 points tier) primary + BaoStock (cross-validation backup)
- **Rebalance**: Monthly, top 10 equal-weight positions

## Quick Start

```bash
# 1. Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp config/settings.template.toml config/settings.toml
# Edit config/settings.toml and fill in your Tushare token

# 4. Download all data (~20 min)
python -m core.data_loader --download-all

# 5. Validate data
python -m core.data_loader --validate

# 6. Run tests
pytest
```

## Directory Structure

```
trading/
├── config/          # Configuration files
├── data/            # Raw and processed data (gitignored)
├── core/            # Core modules
├── strategies/      # Strategy parameter configs
├── research/        # Jupyter notebooks for factor research
├── output/          # Reports and charts
├── logs/            # Runtime logs
├── tests/           # Unit tests
└── docs/            # Documentation
```

## Documentation

- Strategy plan (v2.0): `docs/strategy-plan-v2.md`
- Technical plan (v2.0): `docs/tech-plan.md`
- Algorithm workflow diagrams: `docs/algorithm-workflow.md`
- Phase 1 TODO list: `docs/phase1-todos.md`
