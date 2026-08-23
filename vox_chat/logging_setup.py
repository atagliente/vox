"""Rotating file logging with redaction of secrets.

Prompt bodies and API keys must never reach the log file.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from .storage import logs_dir

LOGGER_NAME = "vox"
_MAX_BYTES = 512 * 1024
_BACKUPS = 3

_SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key\W{1,4})([^\s,'\"}\]]+)", re.IGNORECASE),
    re.compile(r"(authorization\W{1,4}bearer\s+)(\S+)", re.IGNORECASE),
    re.compile(r"\b(sk-[A-Za-z0-9]{6,})", re.IGNORECASE),
)


def redact(text: str) -> str:
    """Replace anything that looks like a credential with ``***``."""
    out = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            out = pattern.sub(lambda m: f"{m.group(1)}***", out)
        else:
            out = pattern.sub("***", out)
    return out


class RedactingFilter(logging.Filter):
    """Scrub credentials from every record before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the VOX logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return logger
    try:
        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            directory / "vox.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
