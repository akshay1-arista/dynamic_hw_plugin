from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AUDIT_LOG_PATH
from .models import ActorIdentity, AuditActionName, AuditEvent, AuditTargetType, AuditTrailResult


class AuditTrailError(ValueError):
    pass


@contextmanager
def _locked_audit_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read().strip()
        try:
            data = json.loads(raw) if raw else []
        except json.JSONDecodeError as error:
            raise AuditTrailError(f"Invalid audit log JSON at {path}") from error
        yield data, fh
        fh.seek(0)
        fh.truncate()
        json.dump(data, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def list_audit_events(path: Path = AUDIT_LOG_PATH, *, limit: int = 200) -> AuditTrailResult:
    if limit < 1:
        return AuditTrailResult(events=[])
    if not path.exists():
        return AuditTrailResult(events=[])
    with _locked_audit_log(path) as (data, _fh):
        events = [AuditEvent.model_validate(item) for item in data]
    events.sort(key=lambda item: (item.created_at, item.id), reverse=True)
    return AuditTrailResult(events=events[:limit])


def append_audit_event(
    *,
    action: AuditActionName,
    actor: ActorIdentity,
    target_type: AuditTargetType,
    target_id: str,
    summary: str,
    details: dict[str, Any] | None = None,
    path: Path = AUDIT_LOG_PATH,
) -> AuditEvent:
    event = AuditEvent(
        id=uuid.uuid4().hex[:12],
        action=action,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        details=details or {},
        created_at=_utc_now(),
    )
    with _locked_audit_log(path) as (data, _fh):
        data.append(event.model_dump(mode="json"))
    return event


def append_audit_events(events: list[AuditEvent], path: Path = AUDIT_LOG_PATH) -> list[AuditEvent]:
    if not events:
        return []
    with _locked_audit_log(path) as (data, _fh):
        data.extend(event.model_dump(mode="json") for event in events)
    return events


def build_audit_event(
    *,
    action: AuditActionName,
    actor: ActorIdentity,
    target_type: AuditTargetType,
    target_id: str,
    summary: str,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    return AuditEvent(
        id=uuid.uuid4().hex[:12],
        action=action,
        actor=actor,
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        details=details or {},
        created_at=_utc_now(),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
