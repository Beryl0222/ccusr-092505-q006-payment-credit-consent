"""仅追加的事件存储：按聚合维护不可变版本序列，并落盘以便重启恢复。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event


@dataclass(frozen=True)
class StoredEvent:
    """一条已登记的领域事实，版本号在所属聚合内从 1 开始严格递增。"""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_envelope(self) -> dict[str, Any]:
        """交换层信封：契约字段平铺，业务数据放在 payload 中避免撞名。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at,
            "version": self.version,
            "summary": self.summary,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_envelope(cls, raw: Mapping[str, Any]) -> "StoredEvent":
        return cls(
            event_id=str(raw["event_id"]),
            event_type=str(raw["event_type"]),
            aggregate_type=str(raw["aggregate_type"]),
            aggregate_id=str(raw["aggregate_id"]),
            occurred_at=str(raw["occurred_at"]),
            version=int(raw["version"]),
            summary=str(raw["summary"]),
            payload=dict(raw.get("payload", {})),
        )


class EventStore:
    """只增不改的事件日志；写入前可选地按交换契约校验信封。"""

    def __init__(self, path: str | Path | None = None, schema: Mapping[str, Any] | None = None) -> None:
        self._path = Path(path) if path else None
        self._schema = schema
        self._events: list[StoredEvent] = []
        self._versions: dict[tuple[str, str], int] = {}
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = StoredEvent.from_envelope(json.loads(line))
            self._check_version(event)
            self._events.append(event)
            self._versions[(event.aggregate_type, event.aggregate_id)] = event.version

    def _check_version(self, event: StoredEvent) -> None:
        key = (event.aggregate_type, event.aggregate_id)
        expected = self._versions.get(key, 0) + 1
        if event.version != expected:
            raise ValueError(f"聚合 {key} 的版本序列断裂：期望 {expected}，实际 {event.version}")

    def append(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        summary: str,
        payload: Mapping[str, Any] | None = None,
    ) -> StoredEvent:
        key = (aggregate_type, aggregate_id)
        version = self._versions.get(key, 0) + 1
        event = StoredEvent(
            event_id=f"{aggregate_type}-{aggregate_id}-{version:06d}",
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at,
            version=version,
            summary=summary,
            payload=dict(payload or {}),
        )
        if self._schema is not None:
            issues = validate_event(event.to_envelope(), self._schema)
            if issues:
                detail = "; ".join(f"{issue.field}:{issue.code}" for issue in issues)
                raise ValueError(f"事件不符合交换契约：{detail}")
        if self._path:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_envelope(), ensure_ascii=False, sort_keys=True) + "\n")
        self._events.append(event)
        self._versions[key] = version
        return event

    def events(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> list[StoredEvent]:
        """按写入顺序返回事件，可按聚合过滤。"""
        return [
            event
            for event in self._events
            if aggregate_type in (None, event.aggregate_type) and aggregate_id in (None, event.aggregate_id)
        ]

    def __len__(self) -> int:
        return len(self._events)
