"""配置系统单元测试 — test_config.py

覆盖 P1 优先级:
  - test_default_value_merging: 缺失段使用默认值
  - test_missing_token_error: 缺失 token 抛出 ValueError
  - test_v20_new_params: v2.0 新参数默认值验证

来源: docs/tech-plan.md §7.1
"""

import tempfile
import os

import pytest

from core.config import Config, DataConfig, FactorConfig, BacktestConfig, RiskConfig


class TestConfigDefaults:
    """测试默认值合并 — 缺失段使用默认值。"""

    def test_all_dataclass_defaults(self):
        """验证所有 dataclass 的默认值正确。"""
        dc = DataConfig(tushare_token="test")
        assert dc.start_date == "20150101"
        assert dc.end_date == "20251231"
        assert dc.use_baostock_validation is True
        assert dc.raw_dir == "data/raw"

    def test_factor_config_defaults(self):
        """验证 FactorConfig v2.0 默认参数。"""
        fc = FactorConfig()
        assert fc.sigma_target == 0.15
        assert fc.vmp_upper_bound == 2.0
        assert fc.mom_lookback == 60
        assert fc.ic_lookback_months == 24
        assert fc.ic_half_life_months == 6
        assert fc.ic_weight_deviation_max == 0.5

    def test_risk_config_defaults(self):
        """验证 RiskConfig v2.0 默认参数。"""
        rc = RiskConfig()
        assert rc.ma_period == 20
        assert rc.hard_drawdown_limit == -0.15
        assert rc.hysteresis_up == 3
        assert rc.hysteresis_down == 5
        assert rc.vol_window == 20
        assert rc.vol_percentile == 0.50

    def test_backtest_config_defaults(self):
        """验证 BacktestConfig 默认参数。"""
        bc = BacktestConfig()
        assert bc.initial_capital == 100_000.0
        assert bc.commission_rate == 0.00025
        assert bc.pbo_threshold == 0.30
        assert bc.walk_forward_train_years == 3
        assert bc.walk_forward_test_years == 1


class TestConfigFromToml:
    """测试 Config.from_toml() 方法。"""

    def test_default_value_merging(self):
        """验证: 缺失的 TOML 段使用 dataclass 默认值。"""
        minimal_toml = """
[data]
tushare_token = "test_token_for_merging"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(minimal_toml)
            tmp_path = f.name

        try:
            config = Config.from_toml(tmp_path)
            # 缺失段使用默认值
            assert config.factor.sigma_target == 0.15
            assert config.factor.vmp_upper_bound == 2.0
            assert config.risk.ma_period == 20
            assert config.risk.hard_drawdown_limit == -0.15
            assert config.backtest.initial_capital == 100_000.0
        finally:
            os.unlink(tmp_path)

    def test_missing_token_error(self):
        """验证: tushare_token 缺失或为占位符时抛出 ValueError。"""
        # Case 1: YOUR_TOKEN_HERE
        placeholder_toml = """
[data]
tushare_token = "YOUR_TOKEN_HERE"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(placeholder_toml)
            tmp_path = f.name

        try:
            with pytest.raises(ValueError, match="tushare_token is required"):
                Config.from_toml(tmp_path)
        finally:
            os.unlink(tmp_path)

        # Case 2: empty
        empty_toml = """
[data]
tushare_token = ""
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(empty_toml)
            tmp_path2 = f.name

        try:
            with pytest.raises(ValueError, match="tushare_token is required"):
                Config.from_toml(tmp_path2)
        finally:
            os.unlink(tmp_path2)

    def test_missing_file_error(self):
        """验证: 配置文件不存在时抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            Config.from_toml("/nonexistent/path/config.toml")

    def test_v20_new_params(self):
        """验证: v2.0 新参数在 TOML 中正确读取。"""
        v20_toml = """
[data]
tushare_token = "v20_test_token"

[factor]
sigma_target = 0.20
vmp_upper_bound = 1.5
ic_lookback_months = 12

[risk]
ma_period = 30
hard_drawdown_limit = -0.20
hysteresis_up = 5
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(v20_toml)
            tmp_path = f.name

        try:
            config = Config.from_toml(tmp_path)
            # 自定义值覆盖默认值
            assert config.factor.sigma_target == 0.20
            assert config.factor.vmp_upper_bound == 1.5
            assert config.factor.ic_lookback_months == 12
            assert config.risk.ma_period == 30
            assert config.risk.hard_drawdown_limit == -0.20
            assert config.risk.hysteresis_up == 5
            # 未指定的 v2.0 参数使用默认值
            assert config.factor.ic_half_life_months == 6
            assert config.factor.ic_weight_deviation_max == 0.5
            assert config.risk.hysteresis_down == 5
        finally:
            os.unlink(tmp_path)

    def test_partial_override(self):
        """验证: 部分覆盖 + 部分默认的混合模式。"""
        partial_toml = """
[data]
tushare_token = "partial_test"

[risk]
ma_period = 25
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(partial_toml)
            tmp_path = f.name

        try:
            config = Config.from_toml(tmp_path)
            # 自定义值
            assert config.risk.ma_period == 25
            # 未指定的保持默认
            assert config.risk.hard_drawdown_limit == -0.15
            assert config.risk.hysteresis_up == 3
            assert config.factor.sigma_target == 0.15
        finally:
            os.unlink(tmp_path)
