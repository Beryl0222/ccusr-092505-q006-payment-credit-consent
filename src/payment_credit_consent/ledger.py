"""追加式事件账本与命令决定日志。

账本只增不改：每个聚合内版本严格递增，写入即落盘（JSONL），
读取返回深拷贝，保证历史事实不可被后续动作改写。命令日志记录
每个 ``command_id`` 的决定，用于重试时返回原决定。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator


class VersionConflictError(Exception):
    """追加事件的版本与聚合当前版本不连续。"""


def canonical_request(payload: Any) -> str:
    """请求载荷的规范化串，用于幂等冲突检测。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_request(payload).encode("utf-8")).hexdigest()


class EventStore:
    """按聚合版本递增的追加式事件存储。"""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._events: list[dict[str, Any]] = []
        self._versions: dict[tuple[str, str], int] = {}
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._index(json.loads(line))

    def _index(self, event: dict[str, Any]) -> None:
        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._versions.get(key, 0) + 1
        if event["version"] != expected:
            raise VersionConflictError(
                f"{key[0]}/{key[1]} 期望版本 {expected}，收到 {event['version']}"
            )
        self._versions[key] = event["version"]
        self._events.append(event)

    def next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get((aggregate_type, aggregate_id), 0) + 1

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """校验版本连续性后追加并落盘，返回存入的事件。"""
        stored = copy.deepcopy(event)
        self._index(stored)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stored, ensure_ascii=False) + "\n")
        return copy.deepcopy(stored)

    def all(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._events)

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(
            [
                event
                for event in self._events
                if event["aggregate_type"] == aggregate_type and event["aggregate_id"] == aggregate_id
            ]
        )

    def get(self, event_id: str) -> dict[str, Any] | None:
        for event in self._events:
            if event["event_id"] == event_id:
                return copy.deepcopy(event)
        return None

    def command_events(self) -> dict[str, list[str]]:
        """从事件本身重建 command_id -> event_ids 索引（崩溃恢复的真相来源）。"""
        index: dict[str, list[str]] = {}
        for event in self._events:
            command_id = event.get("command_id")
            if command_id:
                index.setdefault(command_id, []).append(event["event_id"])
        return index

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.all())


class CommandLog:
    """命令决定日志：同一 command_id 重试必须返回原决定。"""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._records: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self._records[record["command_id"]] = record

    def get(self, command_id: str) -> dict[str, Any] | None:
        record = self._records.get(command_id)
        return copy.deepcopy(record) if record else None

    def record(
        self,
        command_id: str,
        digest: str,
        ok: bool,
        reason: str | None,
        event_ids: list[str],
        details: dict[str, Any] | None = None,
    ) -> None:
        if command_id in self._records:
            return
        entry = {
            "command_id": command_id,
            "request_hash": digest,
            "ok": ok,
            "reason": reason,
            "event_ids": list(event_ids),
            "details": details or {},
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._records[command_id] = entry
