from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import SCHEMA  # noqa: F401  确保 sys.path 就绪
from payment_credit_consent.ledger import CommandLog, EventStore, VersionConflictError


def sample_event(version: int, aggregate_id: str = "agg-1", command_id: str = "cmd-1") -> dict:
    return {
        "event_id": f"evt-{aggregate_id}-{version}",
        "event_type": "ORDER_OPENED",
        "aggregate_type": "payment_order",
        "aggregate_id": aggregate_id,
        "occurred_at": "2026-10-06T10:00:00+08:00",
        "version": version,
        "summary": "测试事件",
        "command_id": command_id,
        "facts": {"order_id": aggregate_id},
    }


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "events.jsonl"
        self.store = EventStore(self.path)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_versions_must_be_contiguous_per_aggregate(self) -> None:
        self.store.append(sample_event(1))
        with self.assertRaises(VersionConflictError):
            self.store.append(sample_event(3))
        self.store.append(sample_event(2))
        self.assertEqual(3, self.store.next_version("payment_order", "agg-1"))

    def test_versions_are_independent_across_aggregates(self) -> None:
        self.store.append(sample_event(1, "agg-1"))
        self.store.append(sample_event(1, "agg-2"))
        self.assertEqual(2, len(self.store.all()))

    def test_events_survive_reload(self) -> None:
        self.store.append(sample_event(1))
        self.store.append(sample_event(2))
        reloaded = EventStore(self.path)
        self.assertEqual([1, 2], [event["version"] for event in reloaded.all()])

    def test_returned_events_are_copies(self) -> None:
        self.store.append(sample_event(1))
        self.store.all()[0]["facts"]["order_id"] = "tampered"
        self.store.get("evt-agg-1-1")["summary"] = "tampered"
        fresh = self.store.get("evt-agg-1-1")
        self.assertEqual("agg-1", fresh["facts"]["order_id"])
        self.assertEqual("测试事件", fresh["summary"])

    def test_command_index_is_rebuilt_from_events(self) -> None:
        self.store.append(sample_event(1, command_id="cmd-a"))
        self.store.append(sample_event(2, command_id="cmd-a"))
        reloaded = EventStore(self.path)
        self.assertEqual(
            {"cmd-a": ["evt-agg-1-1", "evt-agg-1-2"]}, reloaded.command_events()
        )


class CommandLogTests(unittest.TestCase):
    def test_records_first_decision_and_ignores_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = CommandLog(Path(tmp) / "commands.jsonl")
            log.record("cmd-1", "hash-1", True, None, ["evt-1"])
            log.record("cmd-1", "hash-1", False, "other", [])
            record = log.get("cmd-1")
            self.assertTrue(record["ok"])
            self.assertEqual(["evt-1"], record["event_ids"])

    def test_records_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commands.jsonl"
            CommandLog(path).record("cmd-1", "hash-1", False, "items_missing", [], {"missing": ["risk_warning"]})
            record = CommandLog(path).get("cmd-1")
            self.assertEqual("items_missing", record["reason"])
            self.assertEqual({"missing": ["risk_warning"]}, record["details"])


if __name__ == "__main__":
    unittest.main()
