"""Admission control and observable, bounded discovery operations (one service worker)."""
from __future__ import annotations

from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import hashlib
import logging
from pathlib import Path
from threading import Lock, RLock
import time
from typing import Any
from uuid import UUID, uuid4


OPERATION_SECONDS = 90.0
STATUS_TTL = 900.0
PREVIEW_TTL = 300.0
incoming_request_id: ContextVar[str | None] = ContextVar("discovery_request_id", default=None)
_current: ContextVar[Operation | None] = ContextVar("discovery_operation", default=None)
_gate = Lock()
_state_lock = RLock()
inventory_write_lock = RLock()
_records: OrderedDict[str, Operation] = OrderedDict()
_previews: OrderedDict[str, dict[str, Any]] = OrderedDict()
_active: Operation | None = None
# Share the service's configured handler; application-only handlers can be disabled
# when Uvicorn configures logging in its worker process.
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)


class DiscoveryOperationError(Exception):
    def __init__(self, message: str, *, status_code: int, code: str, active_request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.active_request_id = active_request_id


class DiscoveryTimeout(DiscoveryOperationError):
    def __init__(self):
        super().__init__("Discovery exceeded its 90-second limit. Start a new preview to retry.",
                         status_code=504, code="discovery_timeout")


def request_id(value: str | None = None) -> str:
    try:
        return str(UUID(value)) if value else str(uuid4())
    except (ValueError, AttributeError):
        return str(uuid4())


@dataclass
class Operation:
    request_id: str
    operation: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    state: str = "running"
    stage: str = "starting"
    counters: dict[str, int] = field(default_factory=lambda: {
        "completed_roots": 0, "total_roots": 0, "completed_switches": 0,
        "discovered_switches": 0, "upstream_calls": 0, "cache_hits": 0,
    })
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    sequence: int = 0
    last_event: float = 0
    parent_request_id: str | None = None
    partial: bool = False

    def remaining(self) -> float:
        remaining = OPERATION_SECONDS - (time.monotonic() - self.started)
        if remaining <= 0:
            raise DiscoveryTimeout()
        return remaining

    def update(self, stage: str | None = None, message: str | None = None, *, level="info", **counts):
        with _state_lock:
            changed = stage is not None and stage != self.stage
            if stage:
                self.stage = stage
            self.counters.update(counts)
            if message or changed or time.monotonic() - self.last_event >= 2:
                self.sequence += 1
                event = {"seq": self.sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
                         "stage": self.stage, "level": level, "message": message or self.stage,
                         "counts": dict(self.counters)}
                self.events.append(event)
                self.last_event = time.monotonic()
                logger.log(logging.WARNING if level == "warning" else logging.INFO,
                           "[request_id=%s] stage=%s state=%s %s counts=%s elapsed=%.2fs",
                           self.request_id, self.stage, self.state, event["message"], self.counters,
                           (self.finished or time.monotonic()) - self.started)

    def increment(self, name: str):
        with _state_lock:
            self.counters[name] = self.counters.get(name, 0) + 1
            self.update()

    def snapshot(self, after_seq=0):
        with _state_lock:
            return {"request_id": self.request_id, "operation": self.operation, "state": self.state,
                    "stage": self.stage, "started_at": self.started_at,
                    "elapsed_seconds": round((self.finished or time.monotonic()) - self.started, 2),
                    "parent_request_id": self.parent_request_id, **self.counters,
                    "last_seq": self.sequence,
                    "logs": [dict(event) for event in self.events if event["seq"] > after_seq]}


def current_operation() -> Operation | None:
    return _current.get()


def check_deadline():
    if operation := _current.get():
        operation.remaining()


def progress(stage: str | None = None, message: str | None = None, **counts):
    check_deadline()
    if operation := _current.get():
        operation.update(stage, message, **counts)


def _prune():
    now = time.monotonic()
    for key, record in list(_records.items()):
        if record.finished is not None and (now - record.finished > STATUS_TTL or len(_records) > 100):
            del _records[key]
    for key, record in list(_previews.items()):
        if now - record["created"] > PREVIEW_TTL:
            del _previews[key]


def active_status():
    with _state_lock:
        return {"active": _active.snapshot() if _active else None}


def operation_status(identifier: str, after_seq=0):
    with _state_lock:
        _prune()
        operation = _records.get(identifier)
        return operation.snapshot(after_seq) if operation else None


def discovery_operation(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            global _active
            if _current.get() is not None:
                check_deadline()
                result = function(*args, **kwargs)
                if getattr(getattr(result, "summary", None), "status", None) == "partial":
                    _current.get().partial = True
                return result
            identifier = incoming_request_id.get() or request_id()
            with _state_lock:
                if not _gate.acquire(blocking=False):
                    active_id = _active.request_id if _active else None
                    raise DiscoveryOperationError(
                        f"Discovery is already running (request {active_id}). Please retry after it finishes.",
                        status_code=409, code="discovery_busy", active_request_id=active_id)
                _prune()
                if identifier in _records:
                    _gate.release()
                    raise DiscoveryOperationError("Request ID was already used. Start a new request.",
                                                  status_code=409, code="request_id_used")
                operation = Operation(identifier, name)
                _records[identifier] = operation
                _active = operation
                _prune()
            token = _current.set(operation)
            operation.update(message=f"Started {name}")
            try:
                result = function(*args, **kwargs)
                operation.state = "partial" if operation.partial or getattr(getattr(result, "summary", None), "status", None) == "partial" else "succeeded"
                if hasattr(result, "request_id"):
                    result.request_id = identifier
                return result
            except Exception as error:
                operation.state = "timed_out" if isinstance(error, DiscoveryTimeout) else "failed"
                # Public errors are safe summaries; arbitrary exceptions can contain secrets.
                safe_error = isinstance(error, DiscoveryOperationError) or type(error).__name__ in {"DiscoveryError", "GenerationError"}
                message = str(error) if safe_error else "Discovery failed; see server diagnostics."
                operation.update(message=message, level="warning")
                raise
            finally:
                with _state_lock:
                    operation.finished = time.monotonic()
                    operation.update("finished", f"Discovery {operation.state}")
                    _active = None
                    _gate.release()
                _current.reset(token)
        return wrapped
    return decorate


def inventory_revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def store_preview(result, path: Path, revision: str):
    with _state_lock:
        _prune()
        identifier = request_id()
        result.preview_id = identifier
        operation = _current.get()
        if operation:
            result.request_id = operation.request_id
        _previews[identifier] = {"created": time.monotonic(), "path": path.resolve(), "revision": revision,
                                 "targets": result.hardware_ids, "result": result.model_copy(deep=True)}
        while len(_previews) > 32:
            _previews.popitem(last=False)


def get_preview(identifier: str, path: Path, targets: list[str]):
    with _state_lock:
        _prune()
        record = _previews.get(identifier)
        if record is None:
            raise DiscoveryOperationError("Preview expired or is unavailable. Refresh again before applying.",
                                          status_code=410, code="preview_unavailable")
        if record["path"] != path.resolve() or record["targets"] != targets:
            raise DiscoveryOperationError("Preview does not match the requested hardware. Refresh again.",
                                          status_code=400, code="preview_mismatch")
        if record["revision"] != inventory_revision(path):
            raise DiscoveryOperationError("Inventory changed after this preview. Refresh again before applying.",
                                          status_code=409, code="preview_stale")
        if operation := _current.get():
            operation.parent_request_id = record["result"].request_id
        return record["result"].model_copy(deep=True)


def consume_preview(identifier: str):
    with _state_lock:
        _previews.pop(identifier, None)


def inventory_transaction(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with inventory_write_lock:
            return function(*args, **kwargs)
    return wrapped
