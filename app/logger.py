"""Logging configuration for the Pólizas Service."""

import logging
import sys
from pathlib import Path
from typing import Any

from app.config import settings


class ContextFilter(logging.Filter):
    """Add context information to log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        if not hasattr(record, "user_id"):
            record.user_id = "-"
        return True


class JSONFormatter(logging.Formatter):
    """Format log records as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import UTC, datetime

        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if hasattr(record, "request_id") and record.request_id != "-":
            log_data["request_id"] = record.request_id
        if hasattr(record, "user_id") and record.user_id != "-":
            log_data["user_id"] = record.user_id

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in (
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "exc_info",
                "exc_text",
                "thread",
                "threadName",
                "request_id",
                "user_id",
                "message",
            ):
                log_data[key] = value

        return json.dumps(log_data)


class TextFormatter(logging.Formatter):
    """Format log records as readable text."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def configure_logging() -> None:
    """Configure logging for the application."""
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    root_logger.addFilter(ContextFilter())

    formatter = JSONFormatter() if settings.log_format == "json" else TextFormatter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if settings.log_file:
        log_path = Path(settings.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance."""
    return logging.getLogger(name)
