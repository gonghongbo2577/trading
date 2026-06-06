"""配置加载器 — 加载 TOML 配置，合并默认值，暴露类型安全的配置对象。

功能: 加载 TOML 配置，合并默认值，暴露类型安全的配置对象
输入: config/settings.toml
输出: Config dataclass

来源: docs/tech-plan.md §4.1, §4.1.1
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DataConfig:
    """数据层配置。

    Attributes:
        tushare_token: Tushare Pro API token（必填）。
        raw_dir: 原始数据存储目录。
        processed_dir: 处理后数据存储目录。
        start_date: 数据起始日期（YYYYMMDD）。
        end_date: 数据结束日期（YYYYMMDD）。
        use_baostock_validation: 是否启用 BaoStock 交叉验证。
        baostock_raw_dir: BaoStock 日线存储目录。
        tushare_use_proxy: 是否使用系统代理访问 Tushare API（代理不稳定时可设为 false）。
    """
    tushare_token: str
    raw_dir: str = "data/raw"
    processed_dir: str = "data/processed"
    start_date: str = "20150101"
    end_date: str = "20251231"
    use_baostock_validation: bool = True
    baostock_raw_dir: str = "data/raw/baostock_daily"
    tushare_use_proxy: bool = True


@dataclass
class FactorConfig:
    """[v2.0] 因子计算参数 — 全因子VMP + IC引导权重。

    Attributes:
        sigma_target: 目标年化波动率（Wang & Li 2024）。
        mom_lookback: 波动率回看交易日（Moreira & Muir 2017）。
        vmp_upper_bound: VMP缩放上限，允许适度放大。
        ic_lookback_months: IC回看窗口（月）。
        ic_half_life_months: IC衰减半衰期（月），因子特异可在 4-12 月范围调节。
        ic_weight_deviation_max: IC权重偏离等权上限（±50%安全阀）。
    """
    sigma_target: float = 0.15
    mom_lookback: int = 60
    vmp_upper_bound: float = 2.0
    ic_lookback_months: int = 24
    ic_half_life_months: int = 6
    ic_weight_deviation_max: float = 0.5


@dataclass
class BacktestConfig:
    """[v2.0] 回测配置 — 含 Walk-Forward 参数。

    Attributes:
        initial_capital: 初始资金。
        commission_rate: 佣金费率（万2.5）。
        min_commission: 最低佣金（元/笔）。
        stamp_tax_rate: 印花税率（万5，卖出）。
        transfer_fee_rate: 过户费率（万0.1，沪市双向）。
        slippage_bps: 滑点（bps）。
        walk_forward_train_years: Walk-Forward 训练窗口（年）。
        walk_forward_test_years: Walk-Forward 验证窗口（年）。
        pbo_threshold: PBO 合格阈值。
    """
    initial_capital: float = 100_000.0
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_bps: float = 0.10
    walk_forward_train_years: int = 3
    walk_forward_test_years: int = 1
    pbo_threshold: float = 0.30


@dataclass
class RiskConfig:
    """[v2.0] 风控配置 — MA20双条件择时 + 15%硬熔断。

    Attributes:
        max_single_weight: 单只股票最大权重。
        max_industry_weight: 单行业最大权重。
        max_star_market_weight: 科创板最大持仓数/10。
        ma_period: 均线周期（MA20，长城证券2016）。
        vol_window: 波动率计算窗口。
        vol_percentile: 波动率阈值（>60日历史中位数）。
        hysteresis_down: 降仓迟滞天数。
        hysteresis_up: 恢复迟滞天数（华泰金工2026）。
        reduced_position_ratio: 降仓目标仓位比例。
        alpha_drawdown_threshold: 6月超额回撤阈值。
        early_warning_threshold: 3月超额预警阈值。
        hard_drawdown_limit: 绝对回撤硬熔断线（15%）。
        enable_gc001: 闲置资金自动申购GC001。
    """
    max_single_weight: float = 0.10
    max_industry_weight: float = 0.30
    max_star_market_weight: int = 2
    ma_period: int = 20
    vol_window: int = 20
    vol_percentile: float = 0.50
    hysteresis_down: int = 5
    hysteresis_up: int = 3
    reduced_position_ratio: float = 0.50
    alpha_drawdown_threshold: float = -0.10
    early_warning_threshold: float = -0.03
    hard_drawdown_limit: float = -0.15
    enable_gc001: bool = True


@dataclass
class Config:
    """顶层配置聚合 — 包含所有子配置段。

    通过 Config.from_toml(path) 从 TOML 文件加载，
    缺失的段使用对应 dataclass 的默认值。
    """
    data: DataConfig
    factor: FactorConfig
    backtest: BacktestConfig
    risk: RiskConfig

    @classmethod
    def from_toml(cls, path: str = "config/settings.toml") -> "Config":
        """从 TOML 文件加载配置，缺失段使用默认值。

        Args:
            path: TOML 配置文件路径。

        Returns:
            Config 实例，所有字段已合并默认值。

        Raises:
            ValueError: 当 tushare_token 缺失或为占位符时。
            FileNotFoundError: 当配置文件不存在时。
        """
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        # 解析 data 段 — tushare_token 为必填
        data_raw = raw.get("data", {})
        tushare_token = data_raw.get("tushare_token", "")
        if not tushare_token or tushare_token == "YOUR_TOKEN_HERE":
            raise ValueError(
                "tushare_token is required. "
                "请在 config/settings.toml 中填入你的 Tushare Pro token"
            )
        data_config = DataConfig(
            tushare_token=tushare_token,
            raw_dir=data_raw.get("raw_dir", "data/raw"),
            processed_dir=data_raw.get("processed_dir", "data/processed"),
            start_date=data_raw.get("start_date", "20150101"),
            end_date=data_raw.get("end_date", "20251231"),
            use_baostock_validation=data_raw.get("use_baostock_validation", True),
            baostock_raw_dir=data_raw.get("baostock_raw_dir", "data/raw/baostock_daily"),
            tushare_use_proxy=data_raw.get("tushare_use_proxy", True),
        )

        # 解析 factor 段 — 全部可选，使用默认值
        factor_raw = raw.get("factor", {})
        factor_config = FactorConfig(
            sigma_target=factor_raw.get("sigma_target", 0.15),
            mom_lookback=factor_raw.get("mom_lookback", 60),
            vmp_upper_bound=factor_raw.get("vmp_upper_bound", 2.0),
            ic_lookback_months=factor_raw.get("ic_lookback_months", 24),
            ic_half_life_months=factor_raw.get("ic_half_life_months", 6),
            ic_weight_deviation_max=factor_raw.get("ic_weight_deviation_max", 0.5),
        )

        # 解析 backtest 段 — 全部可选
        backtest_raw = raw.get("backtest", {})
        backtest_config = BacktestConfig(
            initial_capital=backtest_raw.get("initial_capital", 100_000.0),
            commission_rate=backtest_raw.get("commission_rate", 0.00025),
            min_commission=backtest_raw.get("min_commission", 5.0),
            stamp_tax_rate=backtest_raw.get("stamp_tax_rate", 0.0005),
            transfer_fee_rate=backtest_raw.get("transfer_fee_rate", 0.00001),
            slippage_bps=backtest_raw.get("slippage_bps", 0.10),
            walk_forward_train_years=backtest_raw.get("walk_forward_train_years", 3),
            walk_forward_test_years=backtest_raw.get("walk_forward_test_years", 1),
            pbo_threshold=backtest_raw.get("pbo_threshold", 0.30),
        )

        # 解析 risk 段 — 全部可选
        risk_raw = raw.get("risk", {})
        risk_config = RiskConfig(
            max_single_weight=risk_raw.get("max_single_weight", 0.10),
            max_industry_weight=risk_raw.get("max_industry_weight", 0.30),
            max_star_market_weight=risk_raw.get("max_star_market_weight", 2),
            ma_period=risk_raw.get("ma_period", 20),
            vol_window=risk_raw.get("vol_window", 20),
            vol_percentile=risk_raw.get("vol_percentile", 0.50),
            hysteresis_down=risk_raw.get("hysteresis_down", 5),
            hysteresis_up=risk_raw.get("hysteresis_up", 3),
            reduced_position_ratio=risk_raw.get("reduced_position_ratio", 0.50),
            alpha_drawdown_threshold=risk_raw.get("alpha_drawdown_threshold", -0.10),
            early_warning_threshold=risk_raw.get("early_warning_threshold", -0.03),
            hard_drawdown_limit=risk_raw.get("hard_drawdown_limit", -0.15),
            enable_gc001=risk_raw.get("enable_gc001", True),
        )

        return cls(
            data=data_config,
            factor=factor_config,
            backtest=backtest_config,
            risk=risk_config,
        )
