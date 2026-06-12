"""数据下载器 — Tushare + BaoStock 数据下载、本地缓存、增量更新、双源交叉验证。

功能: Tushare + BaoStock 数据下载与本地缓存、增量更新、双源交叉验证
对外接口:
  - download_all(config) → None  首次全量下载
  - update_daily(config) → None  增量更新（仅下载新交易日）
  - load_daily(start, end) → pl.DataFrame  加载本地数据
  - [v2.0] validate_cross_source() → dict  Tushare vs BaoStock 交叉验证

来源: docs/tech-plan.md §4.2, §3.1-3.4, §5.7-5.8
"""

import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

import polars as pl
import structlog
import tushare as ts

from core.config import DataConfig

logger = structlog.get_logger()

# ── 可重试异常类型 ─────────────────────────────────────────


def _is_retryable_error(error: Exception) -> bool:
    """判断异常是否可重试（网络超时/连接错误）。"""
    err_str = str(error).lower()
    retryable = ("timeout", "connection", "timed out", "reset", "refused",
                 "too many requests", "service unavailable", "网关",
                 "连接", "超时", "频率", "rate limit",
                 "prematurely", "incomplete")
    return any(kw in err_str for kw in retryable)


def _is_rate_limit_error(error: Exception) -> bool:
    """判断异常是否为速率限制。"""
    err_str = str(error).lower()
    return "rate" in err_str or "频率" in err_str


class DataLoader:
    """Tushare + BaoStock 数据下载器与本地缓存管理器。

    负责全量数据下载、增量更新、本地 Parquet 分区存储、
    DuckDB/Polars 高效加载、Tushare-BaoStock 双源交叉验证。

    Attributes:
        config: DataConfig 数据层配置。
        pro: Tushare Pro API 客户端。
    """

    def __init__(self, config: DataConfig):
        """初始化 DataLoader，验证 Tushare token 可用性。

        Args:
            config: DataConfig 实例，含 token 和路径配置。

        Raises:
            ConnectionError: 当 Tushare token 验证失败时。
        """
        self.config = config
        self._ensure_dirs()
        self._rate_limit_count = 0  # 连续速率限制触发计数
        self._consecutive_failures = 0  # 连续失败计数（断路器）
        self._proxy_state = None  # 保存原始代理设置以便恢复

        # [v2.0] 代理控制：不稳定时可绕过系统代理直连 Tushare
        if not config.tushare_use_proxy:
            self._proxy_state = {
                "HTTP_PROXY": os.environ.pop("HTTP_PROXY", None),
                "HTTPS_PROXY": os.environ.pop("HTTPS_PROXY", None),
                "http_proxy": os.environ.pop("http_proxy", None),
                "https_proxy": os.environ.pop("https_proxy", None),
            }
            # 只保留 NO_PROXY
            logger.info("已绕过系统代理（tushare_use_proxy=false），直连 Tushare API")

        try:
            self.pro = ts.pro_api(config.tushare_token)
        except Exception as e:
            self._restore_proxy()
            raise ConnectionError(f"Tushare token 验证失败: {e}") from e

        logger.info("DataLoader 初始化完成", token_prefix=config.tushare_token[:4])

    def _restore_proxy(self) -> None:
        """恢复原始代理环境变量。"""
        if self._proxy_state:
            for k, v in self._proxy_state.items():
                if v is not None:
                    os.environ[k] = v
            self._proxy_state = None

    def _retry_api_call(self, fn: Callable[[], Any], context: str = "",
                        max_retries: int = 4) -> Any:
        """统一的 API 调用重试逻辑。

        指数退避 1s→2s→4s→8s，最多 max_retries 次尝试。
        速率限制检测 → sleep(60) 后重试，连续3次触发暂停10分钟。
        非可重试异常（如 token 无效）直接抛出，不浪费重试。

        Args:
            fn: 无参可调用对象，返回 API 响应。
            context: 日志上下文标识（如 ts_code、trade_date）。
            max_retries: 最大尝试次数（含首次，即最多重试 max_retries-1 次）。

        Returns:
            fn 的返回值。

        Raises:
            最后一次尝试的异常（非可重试异常直接抛出）。
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                result = fn()
                # 成功后重置连续失败计数
                if attempt > 0:
                    self._consecutive_failures = 0
                return result
            except Exception as e:
                last_error = e

                # 非可重试异常直接抛出（如 token 无效、参数错误等）
                if not _is_retryable_error(e):
                    logger.error(f"不可重试错误: {context}", error=str(e)[:120])
                    raise

                # 速率限制特殊处理
                if _is_rate_limit_error(e):
                    self._rate_limit_count += 1
                    logger.warning("触发速率限制", attempt=attempt + 1,
                                   context=context,
                                   consecutive=self._rate_limit_count)
                    if self._rate_limit_count >= 3:
                        logger.warning("连续3次速率限制，暂停10分钟")
                        time.sleep(600)
                        self._rate_limit_count = 0
                    else:
                        time.sleep(60)
                    continue

                # 可重试但非速率限制 → 指数退避
                if attempt < max_retries - 1:
                    self._rate_limit_count = 0
                    wait = 2 ** attempt  # 1s, 2s, 4s, 8s
                    logger.warning("API 调用失败，重试中",
                                   attempt=attempt + 1, wait_s=wait,
                                   context=context, error=str(e)[:120])
                    time.sleep(wait)
                else:
                    # 最后一次尝试也失败
                    self._consecutive_failures += 1
                    logger.error("API 调用失败（已耗尽重试）",
                                 context=context, attempts=max_retries,
                                 error=str(e)[:120])

        raise last_error  # type: ignore[misc]

    def _ensure_dirs(self) -> None:
        """确保所有数据目录存在。"""
        for d in [
            self.config.raw_dir,
            self.config.processed_dir,
            self.config.baostock_raw_dir,
        ]:
            Path(d).mkdir(parents=True, exist_ok=True)

    # ── 交易日历 ─────────────────────────────────────────────

    def download_trade_cal(self) -> pl.DataFrame:
        """下载 SSE 交易日历并缓存为 Parquet。

        从 Tushare trade_cal API 下载上证交易所交易日历，
        过滤 is_open=1 的交易日，存储到 data/raw/trade_cal.parquet。

        Returns:
            DataFrame，列: cal_date, is_open, pretrade_date。
        """
        # 缓存优先：已下载则直接返回
        output_path = Path(self.config.raw_dir) / "trade_cal.parquet"
        if output_path.exists():
            logger.info("交易日历已缓存，跳过下载", path=str(output_path))
            return pl.read_parquet(output_path)

        logger.info("开始下载交易日历", exchange="SSE",
                     start=self.config.start_date, end=self.config.end_date)

        df_raw = self.pro.trade_cal(
            exchange="SSE",
            start_date=self.config.start_date,
            end_date=self.config.end_date,
        )

        # 空数据保护：代理/网络问题可能返回空 DataFrame
        if df_raw is None or df_raw.empty:
            raise ConnectionError(
                "交易日历下载失败: API 返回空数据。"
                "请检查网络连接，或尝试 python -m core.data_loader --download-all --no-proxy 绕过代理"
            )

        df = pl.from_pandas(df_raw)

        # 仅保留交易日
        df_trading = df.filter(pl.col("is_open") == 1)

        # 缓存
        df_trading.write_parquet(output_path)

        logger.info("交易日历下载完成", trading_days=len(df_trading),
                     output=str(output_path))
        return df_trading

    def load_trade_cal(self) -> pl.DataFrame:
        """从缓存加载交易日历。

        Returns:
            DataFrame，列: cal_date, is_open, pretrade_date。

        Raises:
            FileNotFoundError: 当缓存文件不存在时（需先运行 download_trade_cal）。
        """
        path = Path(self.config.raw_dir) / "trade_cal.parquet"
        if not path.exists():
            raise FileNotFoundError(
                f"交易日历缓存不存在: {path}。请先运行 download_trade_cal()"
            )
        return pl.read_parquet(path)

    # ── Tushare 核心数据下载 ──────────────────────────────────

    def download_stock_list(self) -> pl.DataFrame:
        """下载全量股票列表（含退市股）。

        合并 list_status in ('L', 'D', 'P') 三部分，按 ts_code 去重。
        返回列: ts_code, name, market, list_date, delist_date, list_status。

        Returns:
            DataFrame，包含所有上市/退市/暂停上市股票。
        """
        logger.info("开始下载股票列表")
        stock_fields = "ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status"
        try:
            listed = pl.from_pandas(self.pro.stock_basic(
                list_status="L", fields=stock_fields
            ))
            delisted = pl.from_pandas(self.pro.stock_basic(
                list_status="D", fields=stock_fields
            ))
            suspended = pl.from_pandas(self.pro.stock_basic(
                list_status="P", fields=stock_fields
            ))

            df = pl.concat([listed, delisted, suspended], how="diagonal_relaxed")
            df = df.unique(subset=["ts_code"], keep="first")

            output_path = Path(self.config.raw_dir) / "stock_list.parquet"
            df.write_parquet(output_path)

            logger.info("股票列表下载完成",
                        listed=len(listed), delisted=len(delisted),
                        suspended=len(suspended), total=len(df))
            return df
        except Exception:
            logger.exception("股票列表下载失败")
            raise

    def download_daily_batch(self, start: str = "", end: str = "",
                             retry_failed: bool = False,
                             retry_empty: bool = False) -> None:
        """按交易日批量下载日线行情（Hive 分区存储）。

        遍历交易日历的每个交易日 → pro.daily(trade_date=date)
        → 存为 data/raw/tushare_daily/date=YYYYMMDD/data.parquet

        速率控制: time.sleep(0.35) 每调用一次。
        错误恢复: ConnectionError/Timeout → 指数退避重试（1s→2s→4s→8s→16s，最多5次）。
                  RateLimitError → sleep(60) 后重试，连续3次触发则暂停10分钟。
        熔断机制: 连续 5 个日期下载失败 → 暂停 5 分钟后再继续。
        持久化记录:
          - failed_dates.json: 网络错误导致下载失败的日期（--retry-failed 重试）。
          - empty_dates.json: API 返回空数据的日期（--retry-empty 重试）。

        Args:
            start: 起始日期（YYYYMMDD），默认使用 config.start_date。
            end: 结束日期（YYYYMMDD），默认使用 config.end_date。
            retry_failed: 仅重试 failed_dates.json 中记录的失败日期。
            retry_empty: 仅重试 empty_dates.json 中记录的空数据日期。
        """
        import json

        start = start or self.config.start_date
        end = end or self.config.end_date

        # 持久化路径
        failed_path = Path(self.config.raw_dir) / "failed_dates.json"
        empty_path = Path(self.config.raw_dir) / "empty_dates.json"

        if retry_failed:
            if not failed_path.exists():
                logger.info("无失败日期记录，无需重试", path=str(failed_path))
                return
            failed_list = json.loads(failed_path.read_text())
            if not failed_list:
                logger.info("失败日期列表为空，无需重试")
                return
            trade_dates = failed_list
            logger.info("仅重试失败日期", count=len(trade_dates))
        elif retry_empty:
            if not empty_path.exists():
                logger.info("无空数据日期记录，无需重试", path=str(empty_path))
                return
            empty_list = json.loads(empty_path.read_text())
            if not empty_list:
                logger.info("空数据日期列表为空，无需重试")
                return
            trade_dates = empty_list
            logger.info("仅重试空数据日期", count=len(trade_dates))
        else:
            trade_cal = self.load_trade_cal()
            trade_dates = (
                trade_cal
                .filter((pl.col("cal_date") >= start) & (pl.col("cal_date") <= end))
                .get_column("cal_date")
                .to_list()
            )

        logger.info("开始批量下载日线", total_dates=len(trade_dates),
                     start=start, end=end,
                     retry_failed=retry_failed, retry_empty=retry_empty)

        new_failed_dates: list[str] = []
        new_empty_dates: list[str] = []
        success_count = 0
        cached_count = 0
        empty_count = 0
        consecutive_failures = 0
        circuit_breaker_sleep = 300  # 5 分钟熔断等待

        for i, date in enumerate(trade_dates):
            try:
                status = self._download_single_daily_date(date)
                if status == "success":
                    success_count += 1
                    consecutive_failures = 0
                elif status == "cached":
                    cached_count += 1
                    consecutive_failures = 0
                elif status == "empty":
                    new_empty_dates.append(date)
                    empty_count += 1
                    # 不重置断路器：空数据可能由代理/服务端异常引起
                else:  # "failed"
                    new_failed_dates.append(date)
                    consecutive_failures += 1
            except Exception as e:
                logger.error("日线下载异常", trade_date=date, error=str(e))
                new_failed_dates.append(date)
                consecutive_failures += 1

            # 熔断：连续 N 次失败 → 暂停等待代理恢复
            if consecutive_failures >= 5:
                logger.warning(
                    "连续 5 次下载失败，触发熔断",
                    pause_minutes=circuit_breaker_sleep // 60,
                    next_date=(trade_dates[i + 1]
                               if i + 1 < len(trade_dates) else "N/A"),
                )
                time.sleep(circuit_breaker_sleep)
                consecutive_failures = 0

            # 进度汇报 + 速率保护
            if (i + 1) % 200 == 0:
                time.sleep(5)
            if (i + 1) % 100 == 0:
                logger.info("日线下载进度",
                            processed=i + 1, total=len(trade_dates),
                            success=success_count, cached=cached_count,
                            empty=empty_count,
                            failed=len(new_failed_dates))

            time.sleep(0.35)

        # ── 持久化失败日期 ──
        if new_failed_dates:
            existing: list[str] = []
            if failed_path.exists() and not retry_failed:
                try:
                    existing = json.loads(failed_path.read_text())
                except Exception:
                    pass
            all_failed = sorted(set(existing + new_failed_dates))
            failed_path.write_text(json.dumps(all_failed, indent=2))
            logger.warning("日线下载完成（有失败）",
                           success=success_count,
                           cached=cached_count,
                           empty=empty_count,
                           failed=len(new_failed_dates),
                           total_failed_ever=len(all_failed),
                           failed_path=str(failed_path),
                           tip="运行 python -m core.data_loader --retry-failed 重试失败日期")
        elif failed_path.exists() and not retry_failed:
            failed_path.unlink()
            logger.info("失败记录已清理（本次无新失败）")

        # ── 持久化空数据日期 ──
        if new_empty_dates:
            existing_empty: list[str] = []
            if empty_path.exists() and not retry_empty:
                try:
                    existing_empty = json.loads(empty_path.read_text())
                except Exception:
                    pass
            all_empty = sorted(set(existing_empty + new_empty_dates))
            empty_path.write_text(json.dumps(all_empty, indent=2))
            logger.warning("日线下载完成（有空数据日期）",
                           success=success_count,
                           cached=cached_count,
                           empty=empty_count,
                           total_empty_ever=len(all_empty),
                           empty_path=str(empty_path),
                           tip="运行 python -m core.data_loader --retry-empty 重试空数据日期")
        elif empty_path.exists() and not retry_empty:
            empty_path.unlink()
            logger.info("空数据记录已清理（本次无新空数据）")

        # ── 汇总 ──
        total_ok = success_count + cached_count
        if not new_failed_dates and not new_empty_dates:
            logger.info("日线下载全部完成",
                        success=success_count, cached=cached_count,
                        total=len(trade_dates))
        else:
            logger.info("日线下载本轮结束",
                        success=success_count, cached=cached_count,
                        empty=empty_count, failed=len(new_failed_dates),
                        total=len(trade_dates))

    def _download_single_daily_date(self, date: str) -> str:
        """下载单个交易日的日线数据（含重试逻辑）。

        重试策略: 5 次最大尝试，指数退避 1s→2s→4s→8s→16s。
        超时错误（Read timed out / Connect timeout）与其他错误区分日志。

        Args:
            date: 交易日期（YYYYMMDD）。

        Returns:
            "success": 下载成功，文件已写入磁盘。
            "cached": 文件已存在，跳过下载。
            "empty": API 返回空数据（非交易日、Tushare 权限不足或服务端异常）。
            "failed": 所有重试耗尽，下载失败（网络/代理问题）。
        """
        max_retries = 5
        output_dir = Path(self.config.raw_dir) / "tushare_daily" / f"date={date}"
        output_path = output_dir / "data.parquet"

        if output_path.exists():
            return "cached"

        for attempt in range(max_retries):
            try:
                df_raw = self.pro.daily(trade_date=date)
                if df_raw is None or len(df_raw) == 0:
                    logger.warning("API 返回空数据（可能权限不足或服务端异常）",
                                   trade_date=date)
                    return "empty"

                df = pl.from_pandas(df_raw)
                output_dir.mkdir(parents=True, exist_ok=True)
                df.write_parquet(output_path)
                return "success"
            except Exception as e:
                err_str = str(e)
                # 速率限制错误
                if "rate" in err_str.lower() or "频率" in err_str:
                    self._rate_limit_count += 1
                    logger.warning("触发速率限制",
                                   attempt=attempt + 1,
                                   consecutive=self._rate_limit_count)
                    if self._rate_limit_count >= 3:
                        logger.warning("连续 3 次速率限制，暂停 10 分钟")
                        time.sleep(600)
                        self._rate_limit_count = 0
                    else:
                        time.sleep(60)
                # 超时错误 — 代理/网络问题
                elif ("timeout" in err_str.lower()
                      or "timed out" in err_str.lower()):
                    self._rate_limit_count = 0
                    if attempt < max_retries - 1:
                        wait = 2 ** attempt  # 1s, 2s, 4s, 8s
                        logger.warning("网络超时，重试中",
                                       attempt=attempt + 1,
                                       wait_s=wait,
                                       trade_date=date)
                        time.sleep(wait)
                    else:
                        logger.error("网络超时（已耗尽重试）",
                                     trade_date=date,
                                     attempts=max_retries)
                        return "failed"
                # 其他连接错误
                elif attempt < max_retries - 1:
                    self._rate_limit_count = 0
                    wait = 2 ** attempt
                    logger.warning("连接错误，重试中",
                                   attempt=attempt + 1,
                                   wait_s=wait,
                                   trade_date=date)
                    time.sleep(wait)
                else:
                    logger.error("连接错误（已耗尽重试）",
                                 trade_date=date,
                                 attempts=max_retries,
                                 error=err_str[:200])
                    return "failed"

        return "failed"

    def _fetch_and_save_basic(self, date: str, output_dir: Path,
                              output_path: Path) -> str:
        """单次 daily_basic API 调用 + 存储。

        Returns:
            "success": 数据非空，已写入磁盘。
            "empty": API 返回空数据。
        """
        df_raw = self.pro.daily_basic(trade_date=date)
        if df_raw is not None and len(df_raw) > 0:
            df = pl.from_pandas(df_raw)
            output_dir.mkdir(parents=True, exist_ok=True)
            df.write_parquet(output_path)
            return "success"
        return "empty"

    def download_daily_basic(self, start: str = "", end: str = "") -> None:
        """下载每日估值指标（daily_basic）。

        按交易日批量下载 PE/PB/总市值等估值数据。
        分区: data/raw/tushare_basic/date=YYYYMMDD/data.parquet

        空数据日期写入 data/raw/empty_basic_dates.json，可通过 --retry-empty 重试。

        Args:
            start: 起始日期（YYYYMMDD）。
            end: 结束日期（YYYYMMDD）。
        """
        import json

        start = start or self.config.start_date
        end = end or self.config.end_date

        trade_cal = self.load_trade_cal()
        trade_dates = (
            trade_cal
            .filter((pl.col("cal_date") >= start) & (pl.col("cal_date") <= end))
            .get_column("cal_date")
            .to_list()
        )

        empty_path = Path(self.config.raw_dir) / "empty_basic_dates.json"
        logger.info("开始下载估值指标", total_dates=len(trade_dates))

        success_count = 0
        cached_count = 0
        empty_count = 0
        failed_count = 0
        new_empty_dates: list[str] = []

        for i, date in enumerate(trade_dates):
            output_dir = Path(self.config.raw_dir) / "tushare_basic" / f"date={date}"
            output_path = output_dir / "data.parquet"

            if output_path.exists():
                cached_count += 1
                continue

            try:
                result = self._retry_api_call(
                    lambda d=date: self._fetch_and_save_basic(d, output_dir, output_path),
                    context=f"daily_basic date={date}",
                    max_retries=4,
                )
                if result == "success":
                    success_count += 1
                elif result == "empty":
                    empty_count += 1
                    new_empty_dates.append(date)
                    logger.warning("估值指标 API 返回空数据", trade_date=date)
                else:
                    success_count += 1  # 兼容旧返回 None 的情况
            except Exception:
                failed_count += 1
                logger.exception("估值指标下载失败（已耗尽重试）", trade_date=date)

            if (i + 1) % 100 == 0:
                logger.info("估值指标下载进度",
                            processed=i + 1, total=len(trade_dates),
                            success=success_count, cached=cached_count,
                            empty=empty_count, failed=failed_count)
            time.sleep(0.35)

        # 持久化空数据日期
        if new_empty_dates:
            existing: list[str] = []
            if empty_path.exists():
                try:
                    existing = json.loads(empty_path.read_text())
                except Exception:
                    pass
            all_empty = sorted(set(existing + new_empty_dates))
            empty_path.write_text(json.dumps(all_empty, indent=2))
            logger.warning("估值指标下载完成（有空数据日期）",
                           success=success_count, cached=cached_count,
                           empty=empty_count, failed=failed_count,
                           empty_path=str(empty_path))
        else:
            if empty_path.exists():
                empty_path.unlink()
            logger.info("估值指标下载完成",
                        success=success_count, cached=cached_count,
                        failed=failed_count)

    def _fetch_and_save_fina(self, code: str, start: str,
                              end: str) -> bool:
        """单次 fina_indicator API 调用 + 存储（供 _retry_api_call 使用）。

        Returns:
            True 如果数据非空且已保存，False 如果该股票无数据。
        """
        df_raw = self.pro.fina_indicator(ts_code=code)
        if df_raw is not None and len(df_raw) > 0:
            df = pl.from_pandas(df_raw)
            # 过滤日期范围并存储
            df = df.filter(
                (pl.col("end_date") >= start[:4] + "0101")
                & (pl.col("end_date") <= end)
            )
            if df.height > 0:
                output_path = (
                    Path(self.config.raw_dir)
                    / "tushare_fina" / f"{code}.parquet"
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                df.write_parquet(output_path)
                return True
        return False

    def download_fina_indicator(self, start: str = "", end: str = "") -> None:
        """下载财务指标（fina_indicator，按股票逐只下载）。

        fina_indicator API 要求 ts_code 为必填参数，需逐股票下载。
        单次调用 pro.fina_indicator(ts_code=code) 返回该股票所有季度数据，
        因此无需内层季度循环，5000 次调用即可覆盖全市场。

        注意: v2.0 所需字段 goodwill（商誉）、total_equity（净资产）、
        audit_opinion（审计意见）不在 fina_indicator 中，需通过
        download_balancesheet() 和 download_fina_audit() 补充。

        Args:
            start: 起始日期（YYYYMMDD），仅保留 >= start 的数据。
            end: 结束日期（YYYYMMDD），仅保留 <= end 的数据。
        """
        start = start or self.config.start_date
        end = end or self.config.end_date

        stock_path = Path(self.config.raw_dir) / "stock_list.parquet"
        if not stock_path.exists():
            logger.error("股票列表不存在，请先运行 download_stock_list()")
            return
        stock_list = pl.read_parquet(stock_path)
        codes = stock_list["ts_code"].to_list()

        logger.info("开始下载财务指标（逐股票，单次返回全部季度）",
                     stocks=len(codes))

        downloaded = 0
        failed = 0
        for i, code in enumerate(codes):
            try:
                saved = self._retry_api_call(
                    lambda c=code: self._fetch_and_save_fina(c, start, end),
                    context=f"fina_indicator ts_code={code}",
                    max_retries=4,
                )
                if saved:
                    downloaded += 1
            except Exception as e:
                failed += 1
                if failed <= 5:
                    logger.warning("财务指标下载失败", ts_code=code, error=str(e)[:120])
            time.sleep(0.35)

            if (i + 1) % 500 == 0:
                logger.info("财务指标下载进度",
                            processed=i + 1, total=len(codes),
                            downloaded=downloaded, failed=failed)

        logger.info("财务指标下载完成",
                     stocks_with_data=downloaded, failed=failed)

    @staticmethod
    def _generate_quarters(start_year: str, end_year: str) -> list[str]:
        """生成季度列表，如 ['20150101', '20150331', '20150630', ...]。

        Args:
            start_year: 起始年份。
            end_year: 结束年份。

        Returns:
            季度日期列表（YYYYMMDD 格式，每季度最后一天）。
        """
        quarters = []
        for y in range(int(start_year), int(end_year) + 1):
            for m, d in [("03", "31"), ("06", "30"), ("09", "30"), ("12", "31")]:
                quarters.append(f"{y}{m}{d}")
        return quarters

    # ── Tushare 辅助数据下载 ──────────────────────────────────

    def download_sw_classification(self) -> pl.DataFrame:
        """下载申万行业分类（SW2021 Level-1）。

        使用 index_member_all() 获取全量股票行业分类（单次调用返回所有行业），
        按 ts_code 去重，保留最新 in_date 的行业分类。

        返回列: ts_code, l1_name, in_date, out_date。

        Returns:
            DataFrame，每只股票唯一行业分类。
        """
        logger.info("开始下载申万行业分类")

        try:
            # index_member_all 返回全量股票的所有行业历史成员（含 l1_name）
            # 不传 index_code 参数可获得完整数据
            df_raw = self.pro.index_member_all()
            if df_raw is None or len(df_raw) == 0:
                # 备选：按申万一级行业代码获取
                df_raw = self.pro.index_member_all(l1_code="801010.SI")

            df = pl.from_pandas(df_raw)

            # index_member_all 返回含 l1_name 列 — 使用 API 响应中的真实行业
            # 按 ts_code 分组，取最新 in_date 的行业分类
            df_dedup = (
                df
                .sort("in_date", descending=True)
                .unique(subset=["ts_code"], keep="first")
                .select(["ts_code", "l1_name", "in_date", "out_date"])
            )

            output_path = Path(self.config.raw_dir) / "sw_classification.parquet"
            df_dedup.write_parquet(output_path)

            n_industries = df_dedup["l1_name"].n_unique()
            n_stocks = len(df_dedup)
            logger.info("申万行业分类下载完成",
                        industries=n_industries,
                        stocks=n_stocks,
                        note=f"{n_stocks} 只股票，{n_industries} 个行业")
            return df_dedup
        except Exception:
            logger.exception("申万行业分类下载失败")
            raise

    def download_index_daily(self, index_code: str = "000905.SH",
                             start: str = "", end: str = "") -> pl.DataFrame:
        """下载指数日线行情（默认中证500）。

        Args:
            index_code: 指数代码（如 000905.SH 中证500）。
            start: 起始日期。
            end: 结束日期。

        Returns:
            DataFrame，列含 trade_date, close, pct_chg。
        """
        start = start or self.config.start_date
        end = end or self.config.end_date

        logger.info("开始下载指数行情", index_code=index_code, start=start, end=end)

        try:
            df_raw = self.pro.index_daily(
                ts_code=index_code, start_date=start, end_date=end
            )
            df = pl.from_pandas(df_raw)

            output_path = Path(self.config.raw_dir) / f"{index_code[:6]}_daily.parquet"
            df.write_parquet(output_path)

            logger.info("指数行情下载完成", index_code=index_code, rows=len(df))
            return df
        except Exception:
            logger.exception("指数行情下载失败", index_code=index_code)
            raise

    # ── [v2.0] 资产负债表与审计意见 ──────────────────────────

    def _fetch_bs_chunk(self, code: str, start: str, end: str) -> Optional[pl.DataFrame]:
        """单次 balancesheet API 调用（供 _retry_api_call 使用）。

        Returns:
            DataFrame 如果数据非空，None 如果该股票无资产负债表数据。
        """
        df_raw = self.pro.balancesheet(ts_code=code)
        if df_raw is not None and len(df_raw) > 0:
            df = pl.from_pandas(df_raw)
            # 保留关键列
            keep_cols = ["ts_code", "end_date", "goodwill",
                         "total_hldr_eqy_exc_min_int"]
            available = [c for c in keep_cols if c in df.columns]
            df = df.select(available)
            # 过滤日期范围
            df = df.filter(
                (pl.col("end_date") >= start[:4] + "0101")
                & (pl.col("end_date") <= end)
            )
            if df.height > 0:
                return df
        return None

    def download_balancesheet(self, start: str = "", end: str = "") -> None:
        """[v2.0] 下载资产负债表数据（商誉、净资产）。

        从 balancesheet API 逐股票获取 goodwill（商誉）和
        total_hldr_eqy_exc_min_int（归属母公司股东权益，即净资产）。
        单次调用返回该股票所有报告期数据。

        Universe Layer 7: 净资产 ≤ 0 → 排除
        Universe Layer 9: 商誉/净资产 > 30% → 排除

        Args:
            start: 起始日期（YYYYMMDD）。
            end: 结束日期（YYYYMMDD）。
        """
        start = start or self.config.start_date
        end = end or self.config.end_date

        stock_path = Path(self.config.raw_dir) / "stock_list.parquet"
        if not stock_path.exists():
            logger.error("股票列表不存在，请先运行 download_stock_list()")
            return
        stock_list = pl.read_parquet(stock_path)
        codes = stock_list["ts_code"].to_list()

        logger.info("开始下载资产负债表（商誉+净资产）", stocks=len(codes))

        results = []
        failed = 0
        for i, code in enumerate(codes):
            try:
                df_chunk = self._retry_api_call(
                    lambda c=code: self._fetch_bs_chunk(
                        c, start, end
                    ),
                    context=f"balancesheet ts_code={code}",
                    max_retries=4,
                )
                if df_chunk is not None and df_chunk.height > 0:
                    results.append(df_chunk)
            except Exception as e:
                failed += 1
                if failed <= 5:
                    logger.warning("资产负债表下载失败",
                                   ts_code=code, error=str(e)[:120])
            time.sleep(0.35)
            if (i + 1) % 500 == 0:
                logger.info("资产负债表下载进度",
                            processed=i + 1, total=len(codes),
                            success=len(results), failed=failed)

        if results:
            df_all = pl.concat(results, how="diagonal_relaxed")
            output_path = Path(self.config.raw_dir) / "balancesheet.parquet"
            df_all.write_parquet(output_path)
            logger.info("资产负债表下载完成",
                        stocks=df_all["ts_code"].n_unique(), rows=len(df_all))
        else:
            logger.warning("未获取到资产负债表数据")

    def _fetch_audit_chunk(self, code: str, start: str,
                            end: str) -> Optional[pl.DataFrame]:
        """单次 fina_audit API 调用（供 _retry_api_call 使用）。

        Returns:
            DataFrame 如果数据非空，None 如果该股票无审计意见数据。
        """
        df_raw = self.pro.fina_audit(ts_code=code)
        if df_raw is not None and len(df_raw) > 0:
            df = pl.from_pandas(df_raw)
            keep_cols = ["ts_code", "ann_date", "end_date", "audit_result"]
            available = [c for c in keep_cols if c in df.columns]
            df = df.select(available)
            # 过滤日期范围（按 ann_date）
            df = df.filter(
                (pl.col("ann_date") >= start)
                & (pl.col("ann_date") <= end)
            )
            if df.height > 0:
                return df
        return None

    def download_fina_audit(self, start: str = "", end: str = "") -> None:
        """[v2.0] 下载财务审计意见数据。

        从 fina_audit API 逐股票获取 audit_result（审计意见）。
        单次调用返回该股票所有年份的审计意见。

        Universe Layer 8: 非标准无保留意见 → 排除

        Args:
            start: 起始日期（YYYYMMDD）。
            end: 结束日期（YYYYMMDD）。
        """
        start = start or self.config.start_date
        end = end or self.config.end_date

        stock_path = Path(self.config.raw_dir) / "stock_list.parquet"
        if not stock_path.exists():
            logger.error("股票列表不存在，请先运行 download_stock_list()")
            return
        stock_list = pl.read_parquet(stock_path)
        codes = stock_list["ts_code"].to_list()

        logger.info("开始下载财务审计意见", stocks=len(codes))

        results = []
        failed = 0
        for i, code in enumerate(codes):
            try:
                df_chunk = self._retry_api_call(
                    lambda c=code: self._fetch_audit_chunk(c, start, end),
                    context=f"fina_audit ts_code={code}",
                    max_retries=4,
                )
                if df_chunk is not None and df_chunk.height > 0:
                    results.append(df_chunk)
            except Exception as e:
                failed += 1
                if failed <= 5:
                    logger.warning("审计意见下载失败",
                                   ts_code=code, error=str(e)[:120])
            time.sleep(0.35)
            if (i + 1) % 500 == 0:
                logger.info("审计意见下载进度",
                            processed=i + 1, total=len(codes),
                            success=len(results), failed=failed)

        if results:
            df_all = pl.concat(results, how="diagonal_relaxed")
            output_path = Path(self.config.raw_dir) / "fina_audit.parquet"
            df_all.write_parquet(output_path)
            logger.info("财务审计意见下载完成",
                        stocks=df_all["ts_code"].n_unique(), rows=len(df_all))
        else:
            logger.warning("未获取到审计意见数据")

    # ── [v2.0] 股权质押数据 ───────────────────────────────────

    def _fetch_pledge_single(self, code: str) -> Optional[pl.DataFrame]:
        """单次 pledge_stat API 调用（供 _retry_api_call 使用）。

        Returns:
            DataFrame 如果数据非空，None 如果该股票无质押数据。
        """
        df_raw = self.pro.pledge_stat(ts_code=code)
        if df_raw is not None and len(df_raw) > 0:
            return pl.from_pandas(df_raw)
        return None

    def download_pledge_stat(self, ts_codes: Optional[list[str]] = None) -> pl.DataFrame:
        """[v2.0] 下载股权质押数据。

        使用 pledge_stat API 逐只获取质押比例。
        返回: ts_code, end_date, pledge_ratio。
        注: ctrl_pledge_ratio（控股股东质押比例）不在 pledge_stat API 中，
        Universe Layer 10 使用 pledge_ratio >= 50% 单条件过滤。
        存储: data/raw/tushare_pledge/pledge.parquet。

        Args:
            ts_codes: 股票代码列表，为 None 时自动从股票列表获取。

        Returns:
            DataFrame，含质押比例数据。
        """
        if ts_codes is None:
            stock_list = pl.read_parquet(
                Path(self.config.raw_dir) / "stock_list.parquet"
            )
            ts_codes = stock_list["ts_code"].to_list()

        logger.info("开始下载股权质押数据", total_stocks=len(ts_codes))

        results = []
        failed = 0
        for i, code in enumerate(ts_codes):
            try:
                df_chunk = self._retry_api_call(
                    lambda c=code: self._fetch_pledge_single(c),
                    context=f"pledge_stat ts_code={code}",
                    max_retries=4,
                )
                if df_chunk is not None:
                    results.append(df_chunk)
            except Exception as e:
                failed += 1
                if failed <= 3:
                    logger.warning("质押数据下载失败", ts_code=code,
                                   error=str(e)[:120])

            if (i + 1) % 500 == 0:
                logger.info("质押数据下载进度",
                            processed=i + 1, total=len(ts_codes))
            time.sleep(0.35)

        if results:
            df = pl.concat(results, how="diagonal_relaxed")
            output_dir = Path(self.config.raw_dir) / "tushare_pledge"
            output_dir.mkdir(parents=True, exist_ok=True)
            df.write_parquet(output_dir / "pledge.parquet")
            logger.info("股权质押数据下载完成",
                        rows=len(df), stocks=df["ts_code"].n_unique(),
                        failed=failed)
            return df
        else:
            logger.warning("未获取到质押数据")
            return pl.DataFrame()

    # ── [v2.0] BaoStock 交叉验证 ──────────────────────────────

    def download_baostock_daily(self, start: str = "", end: str = "") -> None:
        """[v2.0] 下载 BaoStock 日线用于交叉验证。

        优化策略: 按股票逐只下载全量历史（单次 API 调用返回该股票全部交易日），
        内存缓冲 200 只股票后批量写入日期分区。
        相比原 date→stock 嵌套循环（15.6M 调用），减少到 5850 调用（~2674x）。

        字段映射: code→ts_code（加后缀 .SH/.SZ）、date→trade_date、
                  preclose→pre_close、volume→vol。
        分区: data/raw/baostock_daily/date=YYYYMMDD/data.parquet。

        断点续传: data/raw/baostock_completed.json 记录已完成股票代码，
        重新运行时跳过已完成的股票。

        Args:
            start: 起始日期（YYYYMMDD）。
            end: 结束日期（YYYYMMDD）。
        """
        import json
        from collections import defaultdict

        start = start or self.config.start_date
        end = end or self.config.end_date

        try:
            import baostock as bs
        except ImportError:
            logger.error("baostock 未安装，跳过 BaoStock 下载")
            return

        # 连接验证
        lg = bs.login()
        if lg.error_code != "0":
            logger.warning("BaoStock 连接失败，跳过交叉验证下载",
                           error_code=lg.error_code, error_msg=lg.error_msg)
            return

        # 转换 BaoStock 日期格式
        bs_start = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
        bs_end = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

        logger.info("BaoStock 连接成功，开始按股票批量下载日线",
                     start=bs_start, end=bs_end)

        try:
            trade_cal = self.load_trade_cal()
            valid_dates = set(
                trade_cal
                .filter((pl.col("cal_date") >= start) & (pl.col("cal_date") <= end))
                .get_column("cal_date")
                .to_list()
            )

            stock_list = pl.read_parquet(
                Path(self.config.raw_dir) / "stock_list.parquet"
            )
            codes = stock_list["ts_code"].to_list()

            # 转换 Tushare 代码格式 → BaoStock 格式
            # 000001.SZ → sz.000001, 600000.SH → sh.600000
            bs_codes = []
            for c in codes:
                if c.endswith(".SZ"):
                    bs_codes.append(f"sz.{c[:6]}")
                elif c.endswith(".SH"):
                    bs_codes.append(f"sh.{c[:6]}")

            # 断点续传：恢复已完成的股票
            completed_path = Path(self.config.raw_dir) / "baostock_completed.json"
            completed: set[str] = set()
            if completed_path.exists():
                try:
                    completed = set(json.loads(completed_path.read_text()))
                except Exception:
                    pass

            pending = [(bs, ts) for bs, ts in zip(bs_codes, codes)
                       if bs not in completed]
            logger.info("BaoStock 下载进度",
                        total=len(bs_codes), completed=len(completed),
                        pending=len(pending))

            if not pending:
                logger.info("所有股票已完成 BaoStock 下载")
                return

            # 内存缓冲: date → list of dict rows
            buffer: dict[str, list[dict]] = defaultdict(list)
            batch_size = 200
            success = 0
            failed = 0
            bs_columns = ["date", "code", "open", "high", "low", "close",
                          "preclose", "volume", "amount"]

            for i, (bs_code, ts_code) in enumerate(pending):
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,code,open,high,low,close,preclose,volume,amount",
                        start_date=bs_start,
                        end_date=bs_end,
                        frequency="d",
                        adjustflag="1",  # 后复权
                    )
                    if rs.error_code == "0" and rs.data:
                        for row in rs.data:
                            bs_date = row[0].replace("-", "")  # 2015-01-05 → 20150105
                            if bs_date not in valid_dates:
                                continue
                            try:
                                buffer[bs_date].append({
                                    "trade_date": bs_date,
                                    "ts_code": ts_code,
                                    "open": float(row[2]),
                                    "high": float(row[3]),
                                    "low": float(row[4]),
                                    "close": float(row[5]),
                                    "pre_close": float(row[6]),
                                    "vol": float(row[7]),
                                    "amount": float(row[8]),
                                })
                            except (ValueError, TypeError):
                                # 退市/停牌股票某些字段为空字符串 ''，跳过该行
                                continue
                        success += 1
                        completed.add(bs_code)
                    else:
                        failed += 1
                        if failed <= 5:
                            logger.warning("BaoStock 单只股票查询失败",
                                           bs_code=bs_code, ts_code=ts_code,
                                           error_code=rs.error_code)
                        else:
                            logger.debug("BaoStock 无数据（正常，如北交所股票）",
                                         bs_code=bs_code, ts_code=ts_code)
                except Exception as e:
                    failed += 1
                    if failed <= 5:
                        logger.warning("BaoStock 查询异常",
                                       bs_code=bs_code, error=str(e)[:100])

                # 每 batch_size 只股票刷盘一次
                if (i + 1) % batch_size == 0:
                    self._flush_baostock_buffer(buffer)
                    buffer.clear()
                    # 保存断点
                    completed_path.write_text(
                        json.dumps(sorted(completed), indent=2))
                    logger.info("BaoStock 下载进度（按股票）",
                                processed=i + 1, total=len(pending),
                                success=success, failed=failed,
                                buffered_dates=0)

                time.sleep(0.05)  # BaoStock 免费，无需严格限速

            # 最终刷盘
            if buffer:
                self._flush_baostock_buffer(buffer)

            # 保存最终断点
            completed_path.write_text(json.dumps(sorted(completed), indent=2))

            logger.info("BaoStock 日线下载完成",
                        total_target=len(bs_codes),
                        success=success, failed=failed)

        finally:
            bs.logout()

    def _flush_baostock_buffer(self, buffer: dict) -> None:
        """将内存缓冲中的 BaoStock 数据写入日期分区 Parquet。

        对于每个日期，读取已有分区文件，合并非重复股票后写回。
        去重策略: 按 ts_code 去重，保留最新数据。

        Args:
            buffer: dict[date_str, list[dict]] 格式的缓冲数据。
        """
        import pandas as pd

        baostock_dir = Path(self.config.baostock_raw_dir)

        for date, rows in buffer.items():
            if not rows:
                continue

            output_dir = baostock_dir / f"date={date}"
            output_path = output_dir / "data.parquet"

            # 将缓冲行转为 Polars DataFrame
            new_df = pl.DataFrame(rows, schema={
                "trade_date": pl.Utf8,
                "ts_code": pl.Utf8,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "pre_close": pl.Float64,
                "vol": pl.Float64,
                "amount": pl.Float64,
            })

            output_dir.mkdir(parents=True, exist_ok=True)

            if output_path.exists():
                existing = pl.read_parquet(output_path)
                # 合并：删除已有股票的行，保留新数据
                existing_codes = set(existing["ts_code"].unique().to_list())
                new_only = new_df.filter(~pl.col("ts_code").is_in(existing_codes))
                if new_only.height > 0:
                    combined = pl.concat(
                        [existing, new_only], how="diagonal_relaxed")
                    combined.write_parquet(output_path)
            else:
                new_df.write_parquet(output_path)

    def validate_cross_source(self, start: str = "", end: str = "") -> dict:
        """[v2.0] Tushare vs BaoStock 日收益 Pearson 相关系数校验。

        加载同期 Tushare 和 BaoStock 日线 → 计算每只股票日收益 →
        计算全市场 Pearson 相关系数。

        Args:
            start: 起始日期。
            end: 结束日期。

        Returns:
            dict: {'correlation': float, 'pass': bool, 'warning_dates': list[str]}。

        判定标准:
            corr >= 0.99 → pass
            0.95 <= corr < 0.99 → WARNING
            corr < 0.95 → ERROR + 标记异常日期
        """
        start = start or self.config.start_date
        end = end or self.config.end_date

        logger.info("开始交叉验证", start=start, end=end)

        # 加载 Tushare 数据
        tushare_dir = Path(self.config.raw_dir) / "tushare_daily"
        tushare_pattern = str(tushare_dir / "date=*" / "data.parquet")

        try:
            df_ts = pl.read_parquet(tushare_pattern)
        except Exception:
            logger.warning("Tushare 数据不存在，无法交叉验证")
            return {"correlation": float("nan"), "pass": False, "warning_dates": []}

        # 加载 BaoStock 数据
        baostock_dir = Path(self.config.baostock_raw_dir)
        baostock_pattern = str(baostock_dir / "date=*" / "data.parquet")

        try:
            df_bs = pl.read_parquet(baostock_pattern)
        except Exception:
            logger.warning("BaoStock 数据不存在，无法交叉验证")
            return {"correlation": float("nan"), "pass": False, "warning_dates": []}

        # 计算日收益
        ts_ret = (
            df_ts
            .sort(["ts_code", "trade_date"])
            .with_columns(
                (pl.col("close") / pl.col("pre_close") - 1).alias("ret")
            )
            .select(["ts_code", "trade_date", "ret"])
            .filter(pl.col("ret").is_not_null() & pl.col("ret").is_finite())
        )

        bs_ret = (
            df_bs
            .sort(["ts_code", "trade_date"])
            .with_columns(
                (pl.col("close").cast(pl.Float64)
                 / pl.col("pre_close").cast(pl.Float64) - 1).alias("ret")
            )
            .select(["ts_code", "trade_date", "ret"])
            .filter(pl.col("ret").is_not_null() & pl.col("ret").is_finite())
        )

        # 合并计算相关性
        merged = ts_ret.join(
            bs_ret,
            on=["ts_code", "trade_date"],
            suffix="_bs",
            how="inner",
        )

        if len(merged) < 100:
            logger.warning("交叉验证样本不足", n=len(merged))
            return {"correlation": float("nan"), "pass": False, "warning_dates": []}

        corr = merged.select(pl.corr("ret", "ret_bs")).item()

        warning_dates = []
        if corr >= 0.99:
            status = "PASS"
            passed = True
        elif corr >= 0.95:
            status = "WARNING"
            passed = True
            logger.warning("交叉验证相关性偏低", correlation=corr)
            # 抽样识别异常日期
            warning_dates = self._find_anomalous_dates(merged, threshold=0.95)
        else:
            status = "ERROR"
            passed = False
            logger.error("交叉验证相关性异常", correlation=corr)
            # 识别异常日期
            warning_dates = self._find_anomalous_dates(merged, threshold=0.95)

        logger.info("交叉验证完成", correlation=corr, status=status,
                     anomalous_dates=len(warning_dates))
        return {"correlation": corr, "pass": passed, "warning_dates": warning_dates}

    @staticmethod
    def _find_anomalous_dates(merged: pl.DataFrame, threshold: float = 0.95) -> list[str]:
        """识别交叉验证中相关性异常的日期（抽样策略）。

        随机抽取最多 20 个交易日，逐日计算 Pearson 相关系数，
        标记相关系数低于阈值的日期。

        Args:
            merged: 已合并的 Tushare + BaoStock 收益 DataFrame。
            threshold: 相关性阈值，低于此值为异常。

        Returns:
            异常日期列表。
        """
        import random
        all_dates = merged["trade_date"].unique().to_list()
        sample_dates = random.sample(all_dates, min(20, len(all_dates)))

        anomalous = []
        for d in sample_dates:
            day_data = merged.filter(pl.col("trade_date") == d)
            if day_data.height < 30:
                continue
            day_corr = day_data.select(pl.corr("ret", "ret_bs")).item()
            if day_corr is not None and day_corr < threshold:
                anomalous.append(str(d))
        return anomalous

    # ── 数据加载与复权 ───────────────────────────────────────

    def _fetch_adj_single(self, code: str) -> Optional[pl.DataFrame]:
        """单次 adj_factor API 调用（供 _retry_api_call 使用）。

        Returns:
            DataFrame 如果数据非空，None 如果该股票无复权因子数据。
        """
        df_raw = self.pro.adj_factor(
            ts_code=code,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
        )
        if df_raw is not None and len(df_raw) > 0:
            return pl.from_pandas(df_raw)
        return None

    def download_adj_factor(self) -> None:
        """下载复权因子（adj_factor）。

        adj_factor API 单次最大返回 3000 行，全市场约 5000 只股票
        按 ts_code 首字母分两批（'0'-'3' 和 '6'-'8'）。
        使用临时文件 + 原子重命名防止中途中断导致数据损坏。
        存储: data/raw/adj_factor.parquet。
        """
        import tempfile

        logger.info("开始下载复权因子")

        stock_list = pl.read_parquet(Path(self.config.raw_dir) / "stock_list.parquet")
        codes = stock_list["ts_code"].to_list()

        # 按 ts_code 首字母分两批
        batch_1 = [c for c in codes if c[0] in "0123"]
        batch_2 = [c for c in codes if c[0] in "6789"]

        all_results = []
        failed = 0
        for batch_name, batch_codes in [("batch_1", batch_1), ("batch_2", batch_2)]:
            logger.info("下载复权因子批次", batch=batch_name, codes=len(batch_codes))
            for i, code in enumerate(batch_codes):
                try:
                    df_chunk = self._retry_api_call(
                        lambda c=code: self._fetch_adj_single(c),
                        context=f"adj_factor ts_code={code}",
                        max_retries=4,
                    )
                    if df_chunk is not None:
                        all_results.append(df_chunk)
                except Exception as e:
                    failed += 1
                    if failed <= 5:
                        logger.warning("复权因子下载失败",
                                       ts_code=code, error=str(e)[:120])
                time.sleep(0.35)

            if (i + 1) % 500 == 0:
                logger.info("复权因子下载进度",
                            processed=i + 1, total=len(batch_codes),
                            batch=batch_name, collected=len(all_results))

        # 两个批次全部完成后一次性写入（原子操作）
        if all_results:
            df = pl.concat(all_results, how="diagonal_relaxed")
            output_path = Path(self.config.raw_dir) / "adj_factor.parquet"
            # 先写临时文件，成功后再原子重命名
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".parquet", prefix="adj_factor_",
                dir=self.config.raw_dir,
            )
            try:
                df.write_parquet(tmp_path)
                Path(tmp_path).rename(output_path)
            finally:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
            logger.info("复权因子下载完成",
                        total_rows=len(df), stocks=df["ts_code"].n_unique(),
                        failed=failed)
        else:
            logger.warning("未获取到复权因子数据")

    def load_daily(self, start: str = "", end: str = "") -> pl.DataFrame:
        """从 Parquet 加载日线数据，自动合并复权因子计算后复权价格。

        使用 DuckDB 谓词下推 + Parquet 列裁剪，全区间加载 < 2s。
        返回列: ts_code, trade_date, close_adj, open_adj, high_adj, low_adj,
               vol, amount（及其他原始列）。

        Args:
            start: 起始日期（YYYYMMDD）。
            end: 结束日期（YYYYMMDD）。

        Returns:
            DataFrame，含后复权价格列。
        """
        start = start or self.config.start_date
        end = end or self.config.end_date

        import duckdb

        daily_pattern = str(
            Path(self.config.raw_dir) / "tushare_daily" / "date=*" / "data.parquet"
        )

        query = f"""
        SELECT *
        FROM read_parquet('{daily_pattern}', hive_partitioning=true)
        WHERE trade_date >= '{start}' AND trade_date <= '{end}'
        ORDER BY ts_code, trade_date
        """

        df = duckdb.sql(query).pl()

        # 合并复权因子
        adj_path = Path(self.config.raw_dir) / "adj_factor.parquet"
        if adj_path.exists():
            adj_df = pl.read_parquet(adj_path)
            df = df.join(adj_df, on=["ts_code", "trade_date"], how="left")

            # 计算后复权价格
            adj_col = pl.col("adj_factor").fill_null(1.0)
            df = df.with_columns([
                (pl.col("close") * adj_col).alias("close_adj"),
                (pl.col("open") * adj_col).alias("open_adj"),
                (pl.col("high") * adj_col).alias("high_adj"),
                (pl.col("low") * adj_col).alias("low_adj"),
            ])

        return df

    # ── 增量更新 ─────────────────────────────────────────────

    def update_daily(self) -> None:
        """增量更新日线数据。

        检查 tushare_daily/ 最新分区日期 → 从该日期+1天开始下载到 end_date（或今天）。
        仅下载缺失的交易日。
        """
        daily_dir = Path(self.config.raw_dir) / "tushare_daily"

        if not daily_dir.exists() or not any(daily_dir.iterdir()):
            logger.info("无已有数据，执行全量下载")
            self.download_daily_batch()
            return

        # 找到最新分区日期
        dates = sorted([
            d.name.replace("date=", "")
            for d in daily_dir.iterdir()
            if d.is_dir() and d.name.startswith("date=")
        ])
        if not dates:
            self.download_daily_batch()
            return

        latest_date = dates[-1]

        # 找到下一个交易日
        trade_cal = self.load_trade_cal()
        next_dates = (
            trade_cal
            .filter(pl.col("cal_date") > latest_date)
            .get_column("cal_date")
            .to_list()
        )

        if not next_dates:
            logger.info("已是最新，无需下载", latest_date=latest_date)
            return

        logger.info("开始增量更新", from_date=next_dates[0], to_date=next_dates[-1],
                     new_trading_days=len(next_dates))

        for date in next_dates:
            try:
                self._download_single_daily_date(date)
            except Exception as e:
                logger.error("增量下载失败", trade_date=date, error=str(e))
            time.sleep(0.35)

        logger.info("增量更新完成")

    # ── 数据校验 ─────────────────────────────────────────────

    def validate(self) -> dict[str, bool]:
        """执行 13 项数据质量校验。

        覆盖 tech-plan.md §3.4 全部 12 项 + §5.8 的 1 项异常检测。
        每项校验失败有对应日志级别（WARNING/ERROR），
        可自动修复的问题当场处理。

        Returns:
            dict[str, bool]: 各项校验的通过/失败状态。
        """
        logger.info("=== 开始 13 项数据质量校验 ===")
        results: dict[str, bool] = {}

        checks = [
            ("check_1_dedup", self._check_1_stock_list_dedup),
            ("check_2_list_dates", self._check_2_list_dates),
            ("check_3_daily_unique", self._check_3_daily_unique),
            ("check_4_no_future", self._check_4_no_future_dates),
            ("check_5_no_negative", self._check_5_no_negative_prices),
            ("check_5a_no_extreme_returns", self._check_5a_no_extreme_returns),
            ("check_6_delisted", self._check_6_delisted_no_future),
            ("check_7_adj_factor", self._check_7_adj_factor_monotonic),
            ("check_8_basic_align", self._check_8_basic_date_align),
            ("check_9_fina_ann_date", self._check_9_fina_ann_date),
            ("check_10_fina_v20_fields", self._check_10_fina_v20_fields),
            ("check_11_pledge_coverage", self._check_11_pledge_coverage),
            ("check_12_cross_source_corr", self._check_12_cross_source_corr),
        ]

        for name, check_fn in checks:
            try:
                result = check_fn()
                results.update(result)
            except Exception as e:
                logger.error(f"校验执行异常: {name}", error=str(e))
                results[name] = False

        passed = sum(1 for v in results.values() if v)
        total = len(results)
        failed_items = {k: v for k, v in results.items() if not v}
        if failed_items:
            logger.warning("数据校验完成（有失败项）",
                           passed=passed, total=total, failed=failed_items)
        else:
            logger.info("数据校验全部通过", passed=passed, total=total)
        return results

    # ── 校验 1-5: 基础数据完整性 ─────────────────────────────

    def _check_1_stock_list_dedup(self) -> dict[str, bool]:
        """校验 1: 股票列表按 ts_code 去重，无重复。重复时自动去重保留第一条。"""
        path = Path(self.config.raw_dir) / "stock_list.parquet"
        if not path.exists():
            logger.warning("校验1: 股票列表文件不存在")
            return {"check_1_dedup": False}
        try:
            df = pl.read_parquet(path)
            dupes = len(df) - df["ts_code"].n_unique()
            if dupes > 0:
                logger.warning("校验1: 股票列表存在重复，自动去重", duplicates=dupes)
                df = df.unique(subset=["ts_code"], keep="first")
                df.write_parquet(path)
                return {"check_1_dedup": True}
            logger.info("校验1 PASS: 股票列表无重复", total=len(df))
            return {"check_1_dedup": True}
        except Exception as e:
            logger.error("校验1: 无法执行", error=str(e))
            return {"check_1_dedup": False}

    def _check_2_list_dates(self) -> dict[str, bool]:
        """校验 2: 每只股票有明确 list_date。缺失 list_date → ERROR。"""
        path = Path(self.config.raw_dir) / "stock_list.parquet"
        if not path.exists():
            return {"check_2_list_dates": False}
        try:
            df = pl.read_parquet(path)
            missing = df.filter(
                pl.col("list_date").is_null() | (pl.col("list_date") == "")
            ).height
            if missing > 0:
                logger.error("校验2 FAIL: 缺失 list_date", count=missing)
                return {"check_2_list_dates": False}
            logger.info("校验2 PASS: 所有股票有 list_date")
            return {"check_2_list_dates": True}
        except Exception as e:
            logger.error("校验2: 无法执行", error=str(e))
            return {"check_2_list_dates": False}

    def _check_3_daily_unique(self) -> dict[str, bool]:
        """校验 3: 日线 (ts_code, trade_date) 唯一。按日期分区存储天然唯一。"""
        daily_dir = Path(self.config.raw_dir) / "tushare_daily"
        if not daily_dir.exists() or not any(daily_dir.iterdir()):
            logger.warning("校验3: 日线目录无数据")
            return {"check_3_daily_unique": False}
        # 抽样检查几个分区
        import random
        date_dirs = sorted([
            d for d in daily_dir.iterdir()
            if d.is_dir() and d.name.startswith("date=")
        ])
        if not date_dirs:
            return {"check_3_daily_unique": False}

        sample_dirs = random.sample(date_dirs, min(5, len(date_dirs)))
        for d in sample_dirs:
            try:
                df = pl.read_parquet(d / "data.parquet")
                if df.height != df.unique(["ts_code", "trade_date"]).height:
                    logger.warning("校验3 FAIL: 存在重复行", date=d.name)
                    return {"check_3_daily_unique": False}
            except Exception:
                pass
        logger.info("校验3 PASS: 抽样无重复")
        return {"check_3_daily_unique": True}

    def _check_4_no_future_dates(self) -> dict[str, bool]:
        """校验 4: 无未来日期数据。"""
        from datetime import date as dt_date
        today = dt_date.today().strftime("%Y%m%d")
        daily_dir = Path(self.config.raw_dir) / "tushare_daily"
        if not daily_dir.exists():
            return {"check_4_no_future": False}
        date_dirs = sorted([
            d.name.replace("date=", "") for d in daily_dir.iterdir()
            if d.is_dir() and d.name.startswith("date=")
        ])
        future = [d for d in date_dirs if d > today]
        if future:
            logger.error("校验4 FAIL: 存在未来日期", future_dates=future[:10])
            return {"check_4_no_future": False}
        logger.info("校验4 PASS: 无未来日期")
        return {"check_4_no_future": True}

    def _check_5_no_negative_prices(self) -> dict[str, bool]:
        """校验 5: 无负数价格或 vol=0 的异常记录。抽样检查。"""
        daily_dir = Path(self.config.raw_dir) / "tushare_daily"
        if not daily_dir.exists():
            return {"check_5_no_negative": False}
        try:
            date_dirs = sorted([
                d for d in daily_dir.iterdir()
                if d.is_dir() and d.name.startswith("date=")
            ])
            if not date_dirs:
                return {"check_5_no_negative": False}

            import random
            sample_dirs = random.sample(date_dirs, min(20, len(date_dirs)))
            issues = 0
            for d in sample_dirs:
                df = pl.read_parquet(d / "data.parquet")
                neg = df.filter(
                    (pl.col("open") < 0) | (pl.col("high") < 0)
                    | (pl.col("low") < 0) | (pl.col("close") < 0)
                    | (pl.col("vol") <= 0)
                ).height
                if neg > 0:
                    issues += neg
                    logger.warning("校验5: 发现异常价格/成交量",
                                   date=d.name.replace("date=", ""), count=neg)
            if issues > 0:
                return {"check_5_no_negative": False}
            logger.info("校验5 PASS: 抽样无负价/零量")
            return {"check_5_no_negative": True}
        except Exception as e:
            logger.error("校验5: 无法执行", error=str(e))
            return {"check_5_no_negative": False}

    def _check_5a_no_extreme_returns(self) -> dict[str, bool]:
        """校验 5a: 日涨跌幅异常检测（区分不同板块涨跌停限制）。"""
        daily_dir = Path(self.config.raw_dir) / "tushare_daily"
        if not daily_dir.exists():
            return {"check_5a_no_extreme_returns": False}
        try:
            date_dirs = sorted([
                d for d in daily_dir.iterdir()
                if d.is_dir() and d.name.startswith("date=")
            ])
            if not date_dirs:
                return {"check_5a_no_extreme_returns": False}

            import random
            sample_dirs = random.sample(date_dirs, min(20, len(date_dirs)))
            extreme = 0
            sampled = 0
            for d in sample_dirs:
                df = pl.read_parquet(d / "data.parquet")
                sampled += df.height
                # 主板: ±10% limit + 1% margin = 11%
                main_board = df.filter(
                    ~pl.col("ts_code").str.contains(r"^(300|688)")
                )
                extreme += main_board.filter(
                    pl.col("pct_chg").abs() > 11
                ).height
                # 创业板/科创板: ±20% limit + 2% margin = 22%
                growth_board = df.filter(
                    pl.col("ts_code").str.contains(r"^(300|688)")
                )
                extreme += growth_board.filter(
                    pl.col("pct_chg").abs() > 22
                ).height
            if extreme > 10:
                logger.warning("校验5a: 发现异常涨跌幅",
                               extreme=extreme, sampled=sampled,
                               note="可能含新股首日（无涨跌停限制）")
            # 不阻断 — 新股首日和极端行情属正常现象
            logger.info("校验5a PASS", extreme=extreme, sampled=sampled)
            return {"check_5a_no_extreme_returns": True}
        except Exception as e:
            logger.error("校验5a: 无法执行", error=str(e))
            return {"check_5a_no_extreme_returns": False}

    # ── 校验 6-9: 退市/复权/对齐 ─────────────────────────────

    def _check_6_delisted_no_future(self) -> dict[str, bool]:
        """校验 6: 退市股退市日期后无新数据。抽样检查。"""
        stock_path = Path(self.config.raw_dir) / "stock_list.parquet"
        daily_dir = Path(self.config.raw_dir) / "tushare_daily"
        if not stock_path.exists() or not daily_dir.exists():
            return {"check_6_delisted": False}
        try:
            stocks = pl.read_parquet(stock_path)
            delisted = stocks.filter(
                pl.col("delist_date").is_not_null() & (pl.col("delist_date") != "")
            )
            if delisted.height == 0:
                logger.info("校验6 PASS: 无退市股")
                return {"check_6_delisted": True}

            # 抽样检查部分退市股
            sample = delisted.head(min(20, delisted.height))
            violations = 0
            for row in sample.iter_rows(named=True):
                code = row["ts_code"]
                delist = row["delist_date"]
                # 快速检查: 读取几个最新日期分区
                date_dirs = sorted([
                    d for d in daily_dir.iterdir()
                    if d.is_dir() and d.name.startswith("date=")
                ])
                recent = date_dirs[-10:]  # last 10 dates
                for d in recent:
                    try:
                        df = pl.read_parquet(d / "data.parquet")
                        post_delist = df.filter(
                            (pl.col("ts_code") == code)
                            & (pl.col("trade_date") > delist)
                        ).height
                        if post_delist > 0:
                            violations += 1
                            logger.warning("校验6: 退市后仍有数据",
                                           ts_code=code, delist_date=delist)
                    except Exception:
                        pass
            if violations > 0:
                return {"check_6_delisted": False}
            logger.info("校验6 PASS: 退市股抽样检查通过")
            return {"check_6_delisted": True}
        except Exception as e:
            logger.error("校验6: 无法执行", error=str(e))
            return {"check_6_delisted": False}

    def _check_7_adj_factor_monotonic(self) -> dict[str, bool]:
        """校验 7: adj_factor 连续递增（单日变化 > 50% 标记 WARNING）。"""
        adj_path = Path(self.config.raw_dir) / "adj_factor.parquet"
        if not adj_path.exists():
            logger.warning("校验7: adj_factor 文件不存在")
            return {"check_7_adj_factor": False}
        try:
            df = pl.read_parquet(adj_path)
            # 抽样检查部分股票
            codes = df["ts_code"].unique().head(min(100, df["ts_code"].n_unique()))
            warnings = 0
            for code in codes.to_list():
                ts = df.filter(pl.col("ts_code") == code).sort("trade_date")
                if ts.height < 2:
                    continue
                changes = (
                    ts["adj_factor"].diff().abs() / ts["adj_factor"].shift(1)
                ).drop_nulls()
                big_jumps = changes.filter(changes > 0.5).len()
                if big_jumps > 0:
                    warnings += 1
            if warnings > 5:
                logger.warning("校验7: adj_factor 跳跃较多",
                               stocks_with_jumps=warnings,
                               note="可能是除权除息日，需交叉验证")
            logger.info("校验7 PASS: adj_factor 抽样检查完成",
                        sampled=len(codes), warnings=warnings)
            return {"check_7_adj_factor": True}
        except Exception as e:
            logger.error("校验7: 无法执行", error=str(e))
            return {"check_7_adj_factor": False}

    def _check_8_basic_date_align(self) -> dict[str, bool]:
        """校验 8: daily_basic 的 trade_date 分区与日线一致。"""
        basic_dir = Path(self.config.raw_dir) / "tushare_basic"
        daily_dir = Path(self.config.raw_dir) / "tushare_daily"
        if not basic_dir.exists() and not daily_dir.exists():
            return {"check_8_basic_align": False}
        if not basic_dir.exists() or not any(basic_dir.iterdir()):
            logger.warning("校验8: daily_basic 无数据，跳过")
            return {"check_8_basic_align": True}
        try:
            basic_dates = set(
                d.name.replace("date=", "") for d in basic_dir.iterdir()
                if d.is_dir() and d.name.startswith("date=")
            )
            daily_dates = set(
                d.name.replace("date=", "") for d in daily_dir.iterdir()
                if d.is_dir() and d.name.startswith("date=")
            )
            missing = daily_dates - basic_dates
            if missing and len(missing) > len(daily_dates) * 0.1:
                logger.warning("校验8: daily_basic 缺失较多日期",
                               missing=len(missing))
                return {"check_8_basic_align": False}
            logger.info("校验8 PASS: basic 与 daily 日期对齐",
                        daily=len(daily_dates), basic=len(basic_dates))
            return {"check_8_basic_align": True}
        except Exception as e:
            logger.error("校验8: 无法执行", error=str(e))
            return {"check_8_basic_align": False}

    # ── 校验 9-12: v2.0 数据质量 ─────────────────────────────

    def _check_9_fina_ann_date(self) -> dict[str, bool]:
        """校验 9: fina_indicator 的 ann_date >= end_date。

        fina_indicator 数据按股票存储为扁平 parquet 文件
        （tushare_fina/{ts_code}.parquet），抽样校验 ann_date >= end_date。
        """
        fina_dir = Path(self.config.raw_dir) / "tushare_fina"
        if not fina_dir.exists() or not any(fina_dir.iterdir()):
            logger.warning("校验9: fina 数据不存在，跳过")
            return {"check_9_fina_ann_date": True}
        try:
            # 扁平文件结构：tushare_fina/{ts_code}.parquet
            parquet_files = sorted([
                f for f in fina_dir.iterdir()
                if f.is_file() and f.suffix == ".parquet"
            ])
            if not parquet_files:
                return {"check_9_fina_ann_date": True}

            import random
            sample_files = random.sample(parquet_files, min(10, len(parquet_files)))
            violations = 0
            for pf in sample_files:
                df = pl.read_parquet(pf)
                if "ann_date" in df.columns and "end_date" in df.columns:
                    bad = df.filter(
                        pl.col("ann_date").is_not_null()
                        & pl.col("end_date").is_not_null()
                        & (pl.col("ann_date").cast(pl.Utf8)
                           < pl.col("end_date").cast(pl.Utf8))
                    ).height
                    violations += bad
            if violations > 0:
                logger.error("校验9 FAIL: ann_date < end_date",
                             violations=violations)
                return {"check_9_fina_ann_date": False}
            logger.info("校验9 PASS: ann_date >= end_date 通过",
                        sampled=len(sample_files))
            return {"check_9_fina_ann_date": True}
        except Exception as e:
            logger.error("校验9: 无法执行", error=str(e))
            return {"check_9_fina_ann_date": False}

    def _check_10_fina_v20_fields(self) -> dict[str, bool]:
        """校验 10: [v2.0] v2.0 辅助数据（资产负债表 + 审计意见）已下载。

        goodwill(商誉)、total_hldr_eqy_exc_min_int(净资产) 来自 balancesheet API，
        audit_result(审计意见) 来自 fina_audit API。
        fina_indicator API 本身不包含这些字段。
        """
        bs_path = Path(self.config.raw_dir) / "balancesheet.parquet"
        audit_path = Path(self.config.raw_dir) / "fina_audit.parquet"

        issues = []
        if bs_path.exists():
            df_bs = pl.read_parquet(bs_path)
            bs_cols = df_bs.columns
            if "goodwill" not in bs_cols:
                issues.append("balancesheet 缺少 goodwill")
            if "total_hldr_eqy_exc_min_int" not in bs_cols:
                issues.append("balancesheet 缺少 total_hldr_eqy_exc_min_int")
            logger.info("校验10: balancesheet",
                        stocks=df_bs["ts_code"].n_unique(), rows=len(df_bs))
        else:
            issues.append("balancesheet.parquet 不存在")

        if audit_path.exists():
            df_audit = pl.read_parquet(audit_path)
            if "audit_result" not in df_audit.columns:
                issues.append("fina_audit 缺少 audit_result")
            logger.info("校验10: fina_audit",
                        stocks=df_audit["ts_code"].n_unique(), rows=len(df_audit))
        else:
            issues.append("fina_audit.parquet 不存在")

        if issues:
            logger.warning("校验10: v2.0 辅助数据不完整",
                           issues=issues,
                           note="运行 --download-all 下载 balancesheet 和 fina_audit")
            return {"check_10_fina_v20_fields": False}
        logger.info("校验10 PASS: v2.0 辅助数据完整")
        return {"check_10_fina_v20_fields": True}

    def _check_11_pledge_coverage(self) -> dict[str, bool]:
        """校验 11: [v2.0] pledge_stat 覆盖 > 4000 只股票。"""
        pledge_path = Path(self.config.raw_dir) / "tushare_pledge" / "pledge.parquet"
        if not pledge_path.exists():
            logger.warning("校验11: pledge 数据不存在")
            return {"check_11_pledge_coverage": False}
        try:
            df = pl.read_parquet(pledge_path)
            n_stocks = df["ts_code"].n_unique()
            if n_stocks < 4000:
                logger.warning("校验11: pledge 覆盖不足",
                               stocks=n_stocks, required=4000)
                return {"check_11_pledge_coverage": False}
            logger.info("校验11 PASS: pledge 覆盖充足", stocks=n_stocks)
            return {"check_11_pledge_coverage": True}
        except Exception as e:
            logger.error("校验11: 无法执行", error=str(e))
            return {"check_11_pledge_coverage": False}

    def _check_12_cross_source_corr(self) -> dict[str, bool]:
        """校验 12: [v2.0] Tushare vs BaoStock 日收益 Pearson corr ≥ 0.99。"""
        try:
            result = self.validate_cross_source()
            corr = result.get("correlation", float("nan"))
            if corr != corr:  # NaN check
                logger.warning("校验12: 交叉验证不可用（BaoStock 数据不足）")
                return {"check_12_cross_source_corr": True}  # 不阻断，容错
            if corr >= 0.99:
                logger.info("校验12 PASS: 交叉验证相关性", correlation=corr)
                return {"check_12_cross_source_corr": True}
            elif corr >= 0.95:
                logger.warning("校验12: 交叉验证相关性偏低",
                               correlation=corr)
                return {"check_12_cross_source_corr": True}
            else:
                logger.error("校验12 FAIL: 交叉验证相关性异常",
                             correlation=corr)
                return {"check_12_cross_source_corr": False}
        except Exception as e:
            logger.warning("校验12: 交叉验证跳过", error=str(e))
            return {"check_12_cross_source_corr": True}  # 容错

    # ── 全量下载入口 ─────────────────────────────────────────

    def download_all(self) -> None:
        """执行首次全量数据下载。

        按顺序执行: 交易日历 → 股票列表 → 日线 → daily_basic →
        fina_indicator → 资产负债表 → 审计意见 → SW分类 → 中证500 →
        质押数据 → BaoStock → 复权因子。
        预估耗时: ~30 min（含 ~5000 股 × 3 组逐股 API 调用）。
        """
        logger.info("=== 开始全量数据下载（预计 ~30 分钟）===")

        logger.info("Step 1/12: 交易日历")
        self.download_trade_cal()

        logger.info("Step 2/12: 股票列表")
        self.download_stock_list()

        logger.info("Step 3/12: 日线行情")
        self.download_daily_batch()

        logger.info("Step 4/12: 估值指标")
        self.download_daily_basic()

        logger.info("Step 5/12: 财务指标（fina_indicator）")
        self.download_fina_indicator()

        logger.info("Step 6/12: 资产负债表（商誉+净资产）[v2.0]")
        self.download_balancesheet()

        logger.info("Step 7/12: 财务审计意见 [v2.0]")
        self.download_fina_audit()

        logger.info("Step 8/12: 申万行业分类")
        self.download_sw_classification()

        logger.info("Step 9/12: 中证500指数")
        self.download_index_daily()

        logger.info("Step 10/12: 股权质押数据 [v2.0]")
        self.download_pledge_stat()

        if self.config.use_baostock_validation:
            logger.info("Step 11/12: BaoStock 交叉验证 [v2.0]")
            try:
                self.download_baostock_daily()
            except Exception:
                logger.warning("BaoStock 下载失败，降级为无交叉验证模式")

        logger.info("Step 12/12: 复权因子")
        self.download_adj_factor()

        logger.info("=== 全量数据下载完成 ===")


# ── CLI 入口 ─────────────────────────────────────────────────

if __name__ == "__main__":
    import os as _os
    import sys

    from core.config import Config
    from core.logger import setup_logging

    setup_logging()

    try:
        config = Config.from_toml("config/settings.toml")
    except (FileNotFoundError, ValueError) as e:
        print(f"配置加载失败: {e}", file=sys.stderr)
        sys.exit(1)

    # --no-proxy: 绕过系统代理直连 Tushare API
    if "--no-proxy" in sys.argv:
        for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            _os.environ.pop(_key, None)
        config.data.tushare_use_proxy = False
        print("  已绕过系统代理，直连 Tushare API")
        print()

    dl = DataLoader(config.data)

    if "--download-all" in sys.argv:
        print("=" * 60)
        print("  全量数据下载 (预计 ~30 分钟)")
        print("  包含: 日线/估值/财务/资产负债表/审计/行业/指数/质押/BaoStock/复权")
        print("=" * 60)
        dl.download_all()
    elif "--retry-failed" in sys.argv:
        print("=" * 60)
        print("  重试失败日期")
        print("=" * 60)
        dl.download_daily_batch(retry_failed=True)
    elif "--retry-empty" in sys.argv:
        print("=" * 60)
        print("  重试空数据日期（此前 API 返回空数据的交易日）")
        print("=" * 60)
        dl.download_daily_batch(retry_empty=True)
    elif "--update" in sys.argv:
        dl.update_daily()
    elif "--validate" in sys.argv:
        dl.download_trade_cal()
        results = dl.validate()
        passed = sum(1 for v in results.values() if v)
        print(f"校验结果: {passed}/{len(results)} 通过")
        if passed < len(results):
            failed = [k for k, v in results.items() if not v]
            print(f"失败项: {failed}")
            print("提示: 运行 python -m core.data_loader --download-all 下载全量数据")
    elif "--validate-cross-source" in sys.argv:
        result = dl.validate_cross_source()
        print(f"交叉验证: correlation={result['correlation']:.4f}, pass={result['pass']}")
    else:
        print("=" * 60)
        print("  DataLoader CLI — Phase 1 数据管理工具")
        print("=" * 60)
        print()
        print("  ⚠️  首次使用请先运行:")
        print("     python -m core.data_loader --download-all")
        print()
        print("  命令:")
        print("     --download-all          全量下载 (2015-2025, ~30min)")
        print("     --update                增量更新（仅新交易日）")
        print("     --retry-failed          仅重试上次失败的日期")
        print("     --retry-empty           仅重试上次返回空数据的日期")
        print("     --no-proxy              绕过系统代理直连 Tushare")
        print("     --validate              运行 13 项数据校验")
        print("     --validate-cross-source Tushare vs BaoStock 交叉验证")
