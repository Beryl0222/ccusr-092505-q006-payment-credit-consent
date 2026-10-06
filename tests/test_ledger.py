from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from payment_credit_consent.ledger import EventStore

OCCURRED_AT = "2026-10-06T10:00:00+08:00"


def _append(store: EventStore, aggregate_id: str, event_type: str = "ORDER_CREATED"):
    return store.append(
        event_type=event_type,
        aggregate_type="payment_order",
        aggregate_id=aggregate_id,
        occurred_at=OCCURRED_AT,
        summary="测试事件",
        payload={"order_id": aggregate_id},
    )


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_versions_are_sequential_per_aggregate(self) -> None:
        store = EventStore(self.path)
        first = _append(store, "o-1")
        second = _append(store, "o-1")
        other = _append(store, "o-2")
        self.assertEqual((1, 2, 1), (first.version, second.version, other.version))
        self.assertNotEqual(first.event_id, second.event_id)

    def test_reload_recovers_events_and_continues_versions(self) -> None:
        store = EventStore(self.path)
        _append(store, "o-1")
        _append(store, "o-1")
        reopened = EventStore(self.path)
        self.assertEqual(2, len(reopened))
        third = _append(reopened, "o-1")
        self.assertEqual(3, third.version)

    def test_broken_version_chain_is_rejected_on_load(self) -> None:
        store = EventStore(self.path)
        _append(store, "o-1")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        broken = json.loads(lines[0])
        broken["version"] = 3
        broken["event_id"] = "payment_order-o-1-000003"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(broken, ensure_ascii=False) + "\n")
        with self.assertRaises(ValueError):
            EventStore(self.path)

    def test_schema_validation_blocks_unregistered_event_type(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        store = EventStore(self.path, schema=schema)
        with self.assertRaises(ValueError):
            _append(store, "o-1", event_type="NOT_REGISTERED")
        self.assertEqual(0, len(store))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
