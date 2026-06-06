"""DataLoader 单元测试 — test_data_loader.py

覆盖 P1 优先级:
  - test_stock_list_dedup: 股票列表无重复
  - test_delisted_included: 退市股在列表中
  - test_date_range_correct: 下载数据日期范围与参数一致
  - test_incremental_no_duplicate: 增量更新不产生重复
  - test_baostock_cross_validation_corr: 交叉验证相关性 ≥ 0.99
  - test_pledge_stat_coverage: 质押数据覆盖 > 4000

来源: docs/tech-plan.md §7.1 + docs/phase1-todos.md TODO-10
"""

import os
from pathlib import Path

import polars as pl
import pytest

from core.config import Config, DataConfig
from core.data_loader import DataLoader


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def config():
    """加载真实配置（需 settings.toml 存在）。"""
    try:
        return Config.from_toml("config/settings.toml")
    except (FileNotFoundError, ValueError):
        pytest.skip("config/settings.toml 不可用，跳过集成测试")


@pytest.fixture(scope="module")
def data_loader(config):
    """创建 DataLoader 实例。"""
    return DataLoader(config.data)


# ── 股票列表测试 ────────────────────────────────────────

class TestStockList:
    """P1: 股票列表数据质量测试。"""

    def test_stock_list_exists(self, data_loader):
        """验证股票列表文件存在。"""
        path = Path(data_loader.config.raw_dir) / "stock_list.parquet"
        if not path.exists():
            data_loader.download_stock_list()
        assert path.exists(), "股票列表文件不存在"

    def test_stock_list_dedup(self, data_loader):
        """P1: 股票列表按 ts_code 去重，无重复。"""
        path = Path(data_loader.config.raw_dir) / "stock_list.parquet"
        if not path.exists():
            data_loader.download_stock_list()
        df = pl.read_parquet(path)
        assert len(df) == df["ts_code"].n_unique(), \
            f"存在 {len(df) - df['ts_code'].n_unique()} 条重复记录"

    def test_delisted_included(self, data_loader):
        """P1: 退市股在股票列表中。"""
        path = Path(data_loader.config.raw_dir) / "stock_list.parquet"
        if not path.exists():
            data_loader.download_stock_list()
        df = pl.read_parquet(path)
        if "list_status" in df.columns:
            delisted = df.filter(pl.col("list_status") == "D")
            assert delisted.height > 0, "股票列表无退市股 — 存在生存者偏差！"
        # 至少检查 delist_date 列存在且有非空值
        if "delist_date" in df.columns:
            has_delist = df["delist_date"].is_not_null().sum()
            assert has_delist > 0, "无 delist_date — 退市股可能缺失"

    def test_stock_list_min_count(self, data_loader):
        """股票列表总数 ≥ 5000（覆盖全市场）。"""
        path = Path(data_loader.config.raw_dir) / "stock_list.parquet"
        if not path.exists():
            data_loader.download_stock_list()
        df = pl.read_parquet(path)
        assert len(df) >= 5000, \
            f"股票列表仅 {len(df)} 只，预期 ≥ 5000"


# ── 交易日历测试 ────────────────────────────────────────

class TestTradeCalendar:
    """P1: 交易日历数据质量测试。"""

    def test_trade_cal_exists(self, data_loader):
        """交易日历文件存在。"""
        path = Path(data_loader.config.raw_dir) / "trade_cal.parquet"
        if not path.exists():
            data_loader.download_trade_cal()
        assert path.exists()

    def test_date_range_correct(self, data_loader):
        """P1: 交易日历日期范围与配置一致。"""
        path = Path(data_loader.config.raw_dir) / "trade_cal.parquet"
        if not path.exists():
            data_loader.download_trade_cal()
        df = pl.read_parquet(path)
        dates = df["cal_date"].sort()
        assert dates.min() >= data_loader.config.start_date, \
            f"最早日期 {dates.min()} < {data_loader.config.start_date}"
        assert dates.max() <= data_loader.config.end_date, \
            f"最晚日期 {dates.max()} > {data_loader.config.end_date}"

    def test_trade_cal_min_count(self, data_loader):
        """2015-2025 约 2450 个交易日。"""
        path = Path(data_loader.config.raw_dir) / "trade_cal.parquet"
        if not path.exists():
            data_loader.download_trade_cal()
        df = pl.read_parquet(path)
        assert len(df) >= 2400, \
            f"交易日仅 {len(df)} 天，预期 ≈ 2450"


# ── 日线数据测试 ────────────────────────────────────────

class TestDailyData:
    """P1: 日线数据质量测试。"""

    def test_daily_partitions_exist(self, data_loader):
        """日线分区目录存在。"""
        daily_dir = Path(data_loader.config.raw_dir) / "tushare_daily"
        # 如果还没下载，至少检查目录结构正确
        assert daily_dir.exists()

    def test_daily_schema(self, data_loader):
        """日线 schema 包含 11 个标准列。"""
        daily_dir = Path(data_loader.config.raw_dir) / "tushare_daily"
        if not daily_dir.exists() or not any(daily_dir.iterdir()):
            pytest.skip("日线数据未下载")
        date_dirs = sorted([
            d for d in daily_dir.iterdir()
            if d.is_dir() and d.name.startswith("date=")
        ])
        if not date_dirs:
            pytest.skip("无日期分区")
        df = pl.read_parquet(date_dirs[0] / "data.parquet")
        expected = {"ts_code", "trade_date", "open", "high", "low",
                    "close", "pre_close", "change", "pct_chg", "vol", "amount"}
        assert set(df.columns) == expected, \
            f"Schema 不匹配: 缺 {expected - set(df.columns)}, 多 {set(df.columns) - expected}"


# ── 增量更新测试 ────────────────────────────────────────

class TestIncrementalUpdate:
    """P1: 增量更新测试。"""

    def test_incremental_no_duplicate(self, data_loader):
        """P1: 增量更新不产生重复数据。"""
        data_loader.download_trade_cal()
        # 先下载一个日期
        data_loader.download_daily_batch("20240603", "20240603")

        daily_dir = Path(data_loader.config.raw_dir) / "tushare_daily"
        date_dir = daily_dir / "date=20240603"
        assert date_dir.exists(), "测试数据未下载成功"

        # 读取原始数据
        df_before = pl.read_parquet(date_dir / "data.parquet")
        count_before = len(df_before)

        # 再次"增量"下载同一日期（应跳过已存在文件）
        data_loader.download_daily_batch("20240603", "20240603")

        df_after = pl.read_parquet(date_dir / "data.parquet")
        count_after = len(df_after)

        assert count_after == count_before, \
            f"增量更新产生重复: {count_before} → {count_after}"


# ── 交叉验证测试 ────────────────────────────────────────

class TestCrossValidation:
    """P1: [v2.0] BaoStock 交叉验证测试。"""

    def test_baostock_cross_validation_corr(self, data_loader):
        """P1: Tushare vs BaoStock 日收益相关性 ≥ 0.99。"""
        # 此测试需要 Tushare 和 BaoStock 数据都已下载
        tushare_dir = Path(data_loader.config.raw_dir) / "tushare_daily"
        baostock_dir = Path(data_loader.config.baostock_raw_dir)

        if not (tushare_dir.exists() and any(tushare_dir.iterdir())):
            pytest.skip("Tushare 日线数据未下载")
        if not (baostock_dir.exists() and any(baostock_dir.iterdir())):
            pytest.skip("BaoStock 日线数据未下载")

        result = data_loader.validate_cross_source("20240603", "20240604")
        corr = result.get("correlation", float("nan"))

        # 如果是 NaN（数据不足），跳过断言
        if corr != corr:  # NaN check
            pytest.skip("交叉验证数据不足")
        assert corr >= 0.99, \
            f"交叉验证相关性 {corr:.4f} < 0.99 阈值"


# ── 质押数据测试 ────────────────────────────────────────

class TestPledgeStat:
    """P1: [v2.0] 质押数据测试。"""

    def test_pledge_stat_coverage(self, data_loader):
        """P1: pledge_stat 覆盖 ≥ 4000 只股票。"""
        pledge_path = Path(data_loader.config.raw_dir) / "tushare_pledge" / "pledge.parquet"
        if not pledge_path.exists():
            pytest.skip("质押数据未下载 — 运行 --download-all 后重试")

        df = pl.read_parquet(pledge_path)
        n_stocks = df["ts_code"].n_unique()
        assert n_stocks >= 4000, \
            f"质押数据仅覆盖 {n_stocks} 只股票，预期 ≥ 4000"


# ── 数据校验测试 ────────────────────────────────────────

class TestValidate:
    """P1: 数据校验集成测试。"""

    def test_validate_returns_dict(self, data_loader):
        """验证 validate() 返回 dict[str, bool]。"""
        data_loader.download_trade_cal()
        results = data_loader.validate()
        assert isinstance(results, dict)
        assert len(results) == 13, f"预期 13 项校验，实际 {len(results)} 项"
        for k, v in results.items():
            assert isinstance(k, str)
            assert isinstance(v, bool)

    def test_validate_stock_list_checks_pass(self, data_loader):
        """校验 1-2 项（股票列表）应通过。"""
        results = data_loader.validate()
        assert results.get("check_1_dedup", False), "股票去重校验失败"
        assert results.get("check_2_list_dates", False), "上市日期校验失败"


# ── 数据加载测试 ────────────────────────────────────────

class TestLoadDaily:
    """P1: 数据加载测试。"""

    def test_load_daily_returns_dataframe(self, data_loader):
        """load_daily() 返回 Polars DataFrame。"""
        daily_dir = Path(data_loader.config.raw_dir) / "tushare_daily"
        if not daily_dir.exists() or not any(daily_dir.iterdir()):
            pytest.skip("日线数据未下载")

        df = data_loader.load_daily("20240603", "20240604")
        assert isinstance(df, pl.DataFrame)
        assert len(df) > 0

    def test_load_daily_has_adj_price(self, data_loader):
        """load_daily 返回含 close_adj 列。"""
        daily_dir = Path(data_loader.config.raw_dir) / "tushare_daily"
        adj_path = Path(data_loader.config.raw_dir) / "adj_factor.parquet"

        if not daily_dir.exists() or not any(daily_dir.iterdir()):
            pytest.skip("日线数据未下载")
        if not adj_path.exists():
            pytest.skip("复权因子未下载")

        df = data_loader.load_daily("20240603", "20240604")
        assert "close_adj" in df.columns, "load_daily 缺少 close_adj 列"
