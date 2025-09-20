# bot/logging_json.py
from __future__ import annotations
import json, logging, os, sys, time
from contextvars import ContextVar
from typing import Any, Dict, Optional, Callable, Awaitable
from functools import wraps

# Контекст для логов
_c_req_id: ContextVar[str] = ContextVar("req_id", default="-")
_c_user:   ContextVar[str] = ContextVar("user_id", default="-")
_c_dialog: ContextVar[str] = ContextVar("dialog_id", default="-")
_c_event:  ContextVar[str] = ContextVar("event", default="-")

def bind_log_context(*, request_id: Optional[str]=None,
                     user_id: Optional[int|str]=None,
                     dialog_id: Optional[int|str]=None,
                     event: Optional[str]=None) -> None:
    if request_id is not None:
        _c_req_id.set(str(request_id))
    if user_id is not None:
        _c_user.set(str(user_id))
    if dialog_id is not None:
        _c_dialog.set(str(dialog_id))
    if event is not None:
        _c_event.set(event)

class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.req_id   = _c_req_id.get()
        record.user_id  = _c_user.get()
        record.dialog_id= _c_dialog.get()
        record.event    = _c_event.get()
        return True

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
            "ts":          round(time.time() * 1000),
            "level":       record.levelname,
            "logger":      record.name,
            "msg":         record.getMessage(),
            "req_id":      getattr(record, "req_id", "-"),
            "user_id":     getattr(record, "user_id", "-"),
            "dialog_id":   getattr(record, "dialog_id", "-"),
            "event":       getattr(record, "event", "-"),
        }
        # добираем extra, если есть
        for key in ("latency_ms", "model", "tokens_prompt", "tokens_answer", "cost_usd"):
            val = getattr(record, key, None)
            if val is not None:
                base[key] = val
        if record.exc_info:
            base["exc_type"] = record.exc_info[0].__name__
            base["exc_msg"]  = str(record.exc_info[1])
        return json.dumps(base, ensure_ascii=False)

def setup_logging(level: str | int = "INFO") -> None:
    """Инициализация JSON-логов для всего процесса."""
    lvl = logging.getLevelName(level) if isinstance(level, str) else level
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(stream=sys.stdout)
    h.setFormatter(_JsonFormatter())
    h.addFilter(_ContextFilter())
    root.addHandler(h)
    root.setLevel(lvl)
    # снизим болтовню лишних либ
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)

def log_timed(event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Декоратор: логируем latency_ms (работает и для async, и для sync)."""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if hasattr(fn, "__call__") and hasattr(fn, "__await__"):
            # async
            @wraps(fn)
            async def _aw(*args, **kwargs):
                t0 = time.perf_counter()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    logging.getLogger("perf").info(
                        "timed", extra={"event": event, "latency_ms": int((time.perf_counter()-t0)*1000)}
                    )
            return _aw
        else:
            @wraps(fn)
            def _w(*args, **kwargs):
                t0 = time.perf_counter()
                try:
                    return fn(*args, **kwargs)
                finally:
                    logging.getLogger("perf").info(
                        "timed", extra={"event": event, "latency_ms": int((time.perf_counter()-t0)*1000)}
                    )
            return _w
    return deco
