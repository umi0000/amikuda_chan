"""Focused regressions for browsing and clearing the plugin's message archive."""

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from storage import Store


class MessageStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "archive.db"
        self.store = Store(self.path)
        self.groups = {"qq:1001": "Selected group", "qq:1002": "Other group"}
        self.key = "qq:1001"
        self.base = int(datetime(2024, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        self.now = self.base + 3 * 86400
        self.store.open(self.groups, self.base - 100, 30)

    def record(self, message_id, offset, *, key=None, reply_to=None):
        self.store.record(
            self.key if key is None else key, message_id, self.base + offset,
            "sender", "text " + message_id, reply_to,
        )

    def page(self, *, key=None, start=None, end=None, cursor=None, limit=50):
        return self.store.messages(
            self.key if key is None else key, start, end, self.now, cursor, limit,
        )

    def ids(self, **kwargs):
        return [row["id"] for row in self.page(**kwargs)["messages"]]

    def iso(self, at):
        return datetime.fromtimestamp(at, UTC).isoformat()

    def ranges(self):
        with sqlite3.connect(self.path) as db:
            return db.execute(
                "SELECT group_key, start_at, end_at FROM archive_clear_ranges ORDER BY start_at"
            ).fetchall()

    def test_before_is_exclusive_and_other_group_and_enabled_windows_remain(self):
        self.record("before", 9)
        self.record("boundary", 10)
        self.record("after", 11)
        self.record("other", 9, key="qq:1002")
        end = self.base + 10
        count = self.store.cleanup_count(self.key, None, end, self.now)
        self.assertEqual(count, 1)
        self.assertEqual(self.store.cleanup(self.key, None, end, self.now, count), 1)
        self.assertEqual(self.ids(), ["after", "boundary"])
        self.assertEqual(self.ids(key="qq:1002"), ["other"])
        self.record("still-enabled", 12)
        self.assertIn("still-enabled", self.ids())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_groups").fetchone()[0], 2)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM collection_windows WHERE ended_at IS NULL"
            ).fetchone()[0], 2)

    def test_inclusive_date_uses_next_shanghai_midnight_as_exclusive_end(self):
        start = self.base
        end = int(datetime(2024, 1, 2, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        self.record("previous-day", -1)
        self.record("midnight", 0)
        self.record("last-second", 86400 - 1)
        self.record("next-day", 86400)
        self.assertEqual(self.ids(start=start, end=end), ["last-second", "midnight"])
        count = self.store.cleanup_count(self.key, start, end, self.now)
        self.assertEqual(count, 2)
        self.assertEqual(self.store.cleanup(self.key, start, end, self.now, count), 2)
        self.assertEqual(self.ids(), ["next-day", "previous-day"])
        self.assertEqual(self.ranges(), [(self.key, start, end)])

    def test_same_timestamp_keyset_pages_have_no_duplicates_or_missing_rows(self):
        for index in range(105):
            self.record(f"m-{index:03d}", 5)
        first = self.page()
        self.assertEqual(len(first["messages"]), 50)
        self.assertEqual(first["next_cursor"], {
            "at": self.iso(self.base + 5), "id": "m-055",
        })
        # A newly archived message above the cursor must not shift the next page.
        self.record("z-new", 5)
        second = self.page(cursor=first["next_cursor"])
        third = self.page(cursor=second["next_cursor"])
        actual = [row["id"] for page in (first, second, third) for row in page["messages"]]
        self.assertEqual(actual, [f"m-{index:03d}" for index in reversed(range(105))])
        self.assertEqual(len(second["messages"]), 50)
        self.assertEqual(len(third["messages"]), 5)
        self.assertIsNone(third["next_cursor"])

    def test_cursor_crosses_timestamp_boundaries_and_preserves_message_fields(self):
        self.record("a", 1)
        self.record("b", 2, reply_to="a")
        self.record("c", 3)
        first = self.page(limit=1)
        second = self.page(limit=1, cursor=first["next_cursor"])
        third = self.page(limit=1, cursor=second["next_cursor"])
        self.assertEqual(first["messages"][0]["id"], "c")
        self.assertEqual(second["messages"], [{
            "id": "b", "at": self.iso(self.base + 2), "sender": "sender",
            "text": "text b", "reply_to": "a", "role": "user",
        }])
        self.assertEqual(third["messages"][0]["id"], "a")
        self.assertIsNone(third["next_cursor"])

    def test_effective_retention_applies_to_browse_count_and_cleanup(self):
        self.store.open(self.groups, self.base, 1)
        self.record("expired", 1)
        self.record("cutoff", 2 * 86400)
        self.record("retained", 2 * 86400 + 1)
        self.assertEqual(self.ids(), ["retained", "cutoff"])
        self.assertEqual(self.store.cleanup_count(self.key, None, self.now, self.now), 2)
        self.assertEqual(self.store.cleanup(self.key, None, self.now + 100, self.now, 2), 2)
        self.assertEqual(self.ranges(), [(self.key, self.base + 2 * 86400, self.now)])
        # Physically present but expired rows are not part of the cleared retained range.
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT message_id FROM archive_messages WHERE group_key = ?", (self.key,),
            ).fetchall(), [("expired",)])

    def test_changed_preview_count_rolls_back_without_deleting_or_auditing(self):
        self.record("first", 1)
        start, end = self.base, self.base + 10
        count = self.store.cleanup_count(self.key, start, end, self.now)
        self.record("arrived-later", 2)
        with self.assertRaisesRegex(ValueError, "重新预览"):
            self.store.cleanup(self.key, start, end, self.now, count)
        self.assertEqual(self.ids(), ["arrived-later", "first"])
        self.assertEqual(self.ranges(), [])
        self.assertEqual(self.store.cleanup(self.key, start, end, self.now, 2), 2)

    def test_reports_remain_unchanged_after_archive_cleanup(self):
        self.record("reported", 1)
        report, _ = self.store.create(
            {self.key: self.groups[self.key]}, self.base, self.base + 10, self.now,
            "provider", "Provider", "prompt",
        )
        self.store.finish(report["id"], "completed", "Saved report body", None)
        before = self.store.report(report["id"])
        self.assertEqual(self.store.cleanup(self.key, self.base, self.base + 10, self.now, 1), 1)
        self.assertEqual(self.store.report(report["id"]), before)
        self.assertEqual(len(self.store.list_reports()), 1)

    def test_clear_produces_coverage_gap_despite_continuing_collection(self):
        self.record("removed", 15)
        self.record("kept", 25)
        before = self.store.coverage(self.groups, self.base, self.base + 40, self.now)
        self.assertFalse(before[0]["partial"])
        self.store.cleanup(self.key, self.base + 10, self.base + 20, self.now, 1)
        self.store.heartbeat(self.now + 100)
        coverage = self.store.coverage(self.groups, self.base, self.base + 40, self.now + 100)
        self.assertTrue(coverage[0]["partial"])
        self.assertEqual(coverage[0]["count"], 1)
        self.assertEqual(coverage[0]["gaps"], [{
            "start": self.iso(self.base + 10), "end": self.iso(self.base + 20),
        }])
        self.assertFalse(coverage[1]["partial"])

    def test_delayed_events_do_not_reappear_and_exclusion_survives_reopen(self):
        self.record("old", 15)
        self.store.cleanup(self.key, self.base + 10, self.base + 20, self.now, 1)
        self.record("old", 15)
        self.record("new-delayed", 16)
        self.record("left-boundary", 10)
        self.record("right-boundary", 20)
        self.record("other-group-delayed", 15, key="qq:1002")
        self.assertEqual(self.ids(), ["right-boundary"])
        self.assertEqual(self.ids(key="qq:1002"), ["other-group-delayed"])
        self.store.open(self.groups, self.now, 30)
        self.record("after-restart-delayed", 17)
        self.assertEqual(self.ids(), ["right-boundary"])
        coverage = self.store.coverage(self.groups, self.base + 10, self.base + 20, self.now)
        self.assertTrue(coverage[0]["partial"])
        self.assertEqual(coverage[0]["gaps"], [{
            "start": self.iso(self.base + 10), "end": self.iso(self.base + 20),
        }])

    def test_cleared_ranges_union_and_audit_contains_only_group_and_times(self):
        for start, end in ((10, 20), (30, 40), (20, 30), (15, 35)):
            self.assertEqual(self.store.cleanup(
                self.key, self.base + start, self.base + end, self.now, 0,
            ), 0)
        self.assertEqual(self.ranges(), [(self.key, self.base + 10, self.base + 40)])
        with sqlite3.connect(self.path) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(archive_clear_ranges)")]
        self.assertEqual(columns, ["group_key", "start_at", "end_at"])
        coverage = self.store.coverage(self.groups, self.base, self.base + 50, self.now)
        self.assertEqual(coverage[0]["gaps"], [{
            "start": self.iso(self.base + 10), "end": self.iso(self.base + 40),
        }])

    def test_unknown_group_queries_are_empty_and_do_not_touch_other_groups(self):
        self.record("allowed", 5)
        self.assertEqual(self.ids(key="qq:not-allowed"), [])
        self.assertEqual(self.store.cleanup_count("qq:not-allowed", None, self.now, self.now), 0)
        self.assertEqual(self.store.cleanup("qq:not-allowed", None, self.now, self.now, 0), 0)
        self.assertEqual(self.ids(), ["allowed"])
        self.assertEqual(self.ranges(), [])

    def test_open_adds_clear_range_table_to_existing_database_idempotently(self):
        self.record("preserved", 5)
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE archive_clear_ranges")
        self.store.open(self.groups, self.base + 10, 30)
        self.store.open(self.groups, self.base + 20, 30)
        self.assertEqual(self.ranges(), [])
        self.assertEqual(self.ids(), ["preserved"])
        self.assertEqual(self.store.cleanup(self.key, self.base, self.base + 10, self.now, 1), 1)


if __name__ == "__main__":
    unittest.main()
