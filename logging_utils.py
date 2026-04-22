import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional


def configure_logger(
    *,
    log_dir: Path,
    main_log_filename: Optional[str],
    error_log_filename: str,
    backup_count: int,
    logger_name: Optional[str] = None,
) -> logging.Logger:
    """配置按日滚动日志：可选主日志 + error 日志 + 控制台。"""
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    target.setLevel(logging.DEBUG)
    target.handlers.clear()

    if main_log_filename:
        main_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / main_log_filename,
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
        )
        main_handler.setLevel(logging.DEBUG)
        main_handler.setFormatter(formatter)
        target.addHandler(main_handler)

    error_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / error_log_filename,
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    target.addHandler(error_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    target.addHandler(console)

    return target
