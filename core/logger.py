"""日志系统 — 基于 structlog 的双输出日志（JSON 文件 + 人类可读控制台）。

功能: 配置 structlog，输出结构化 JSON 日志到 logs/ 目录（含日志轮转），
      同时输出彩色人类可读格式到控制台。
所有后续模块统一使用 logger = structlog.get_logger()。

来源: docs/tech-plan.md Phase 1 Week 2
"""

import logging
import logging.handlers
import structlog
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    """配置 structlog 双输出日志系统。

    - JSON 输出到 logs/app.log（RotatingFileHandler: 10MB × 3 个文件）
    - 彩色控制台输出到 stderr
    - 所有模块通过 structlog.get_logger() 获取 logger

    Args:
        level: 日志级别，默认 "INFO"。支持 "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"。
    """
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)

    # 1. JSON 文件处理器（带日志轮转）
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / "app.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)

    # 2. 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)

    # 3. 配置根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    # 清除已有的 handler（防止重复配置）
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 4. 配置 structlog
    structlog.configure(
        processors=[
            # 添加日志级别
            structlog.stdlib.add_log_level,
            # 添加时间戳
            structlog.processors.TimeStamper(fmt="iso"),
            # 添加调用者信息（模块:行号）
            structlog.stdlib.add_logger_name,
            # 格式化异常信息
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 5. 配置 formatter：文件用 JSON，控制台用彩色
    json_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
    )

    file_handler.setFormatter(json_formatter)
    console_handler.setFormatter(console_formatter)
