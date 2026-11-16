"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_SECRET_KEYS = re.compile(r"(key|secret|password|token|private|authorization)", re.I)
_HEX_KEY = re.compile(r"0x[0-9a-fA-F]{64}")


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _HEX_KEY.sub("0x<redacted>", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact(record.getMessage()),
        }
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            payload[k] = "<redacted>" if _SECRET_KEYS.search(k) else _redact(v)
        if record.exc_info:
            payload["exc"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
