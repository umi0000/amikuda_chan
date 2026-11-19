"""Regression coverage for report sources and legacy report migration."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage import Store


class SourceStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "archive.db"
        self.store = Store(self.path)
        self.groups = {"qq:1001": "Allowed group"}
        self.store.open(self.groups, 1000, 30)

    def snapshot(self, *, key="qq:1001", count=1, at=1001):
        return ([{
            "key": key, "label": "Allowed group", "count": count,
            "partial": None, "started_at": None, "gaps": [],
            "first_at": "1970-01-01T00:16:41+00:00",
        }], [{
            "key": key, "message_id": "astrbot-1", "at": at,
            "sender": "anonymous", "text": "retained text", "reply_to": None,
        }])

    def create(self, **kwargs):
        return self.store.create(self.groups, 1000, 1004, 1003, "provider", "Provider", "prompt", **kwargs)

    def test_astrbot_snapshot_stays_out_of_archive_and_reserves_running_report(self):
        report, rows = self.create(source="astrbot", snapshot=self.snapshot())
        self.assertEqual(report["source"], "astrbot")
        self.assertEqual(report["count"], 1)
        self.assertEqual(rows[0]["message_id"], "astrbot-1")
        self.assertEqual(self.store.list_reports()[0]["source"], "astrbot")
        with self.assertRaises(ValueError):
            self.create(source="astrbot", snapshot=self.snapshot())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_messages").fetchone()[0], 0)
        self.store.finish(report["id"], "completed", "report body", None)
        self.assertEqual(self.store.report(report["id"])["body"], "report body")
        with self.assertRaises(ValueError):
            self.create()

    def test_snapshot_rejects_wrong_counts_scopes_and_time_without_reservation(self):
        cases = [self.snapshot(count=0), self.snapshot(count=2),
                 self.snapshot(key="qq:other"), self.snapshot(key="qq:FriendMessage:1001"),
                 self.snapshot(at=999), self.snapshot(at=1003), self.snapshot(at=1004)]
        coverage, rows = self.snapshot()
        cases.extend([([], rows), (coverage + coverage, rows), (coverage, [])])
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.create(source="astrbot", snapshot=snapshot)
            self.assertEqual(self.store.list_reports(), [])
        coverage, _ = self.snapshot(count=0)
        with self.assertRaises(ValueError):
            self.create(source="astrbot", snapshot=(coverage, []))
        self.assertEqual(self.store.list_reports(), [])

    def test_row_scope_must_match_selected_coverage(self):
        coverage, rows = self.snapshot()
        rows[0]["key"] = "qq:FriendMessage:1001"
        with self.assertRaises(ValueError):
            self.create(source="astrbot", snapshot=(coverage, rows))
        self.assertEqual(self.store.list_reports(), [])

    def test_sources_are_strict_and_plugin_queries_only_selected_archive(self):
        for kwargs in ({"source": "unknown"}, {"source": "astrbot"},
                       {"source": "plugin", "snapshot": self.snapshot()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.create(**kwargs)
        self.store.open({**self.groups, "qq:other": "Other"}, 1000, 30)
        self.store.record("qq:other", "other", 1001, "anonymous", "not selected", None)
        self.store.record("qq:1001", "local", 1001, "anonymous", "local text", None)
        report, rows = self.create()
        self.assertEqual(report["source"], "plugin")
        self.assertEqual([row["message_id"] for row in rows], ["local"])


class SourceMigrationTests(unittest.TestCase):
    def test_open_migrates_legacy_reports_idempotently_without_losing_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.db"
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE reports (
                    id TEXT PRIMARY KEY NOT NULL, status TEXT NOT NULL,
                    start_at INTEGER NOT NULL, end_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, message_count INTEGER NOT NULL,
                    groups_json TEXT NOT NULL, provider_id TEXT NOT NULL,
                    provider_label TEXT NOT NULL, prompt TEXT NOT NULL,
                    body TEXT, error TEXT, coverage_json TEXT NOT NULL
                )""")
                db.execute("""INSERT INTO reports VALUES (
                    'legacy', 'completed', 1000, 1002, 1002, 1, '[]',
                    'provider', 'Provider', 'prompt', 'original report body', NULL, '[]'
                )""")
            store = Store(path)
            store.open({}, 1003, 30)
            store.open({}, 1004, 30)
            self.assertEqual(store.report("legacy")["body"], "original report body")
            self.assertEqual(store.report("legacy")["source"], "plugin")
            self.assertEqual(store.list_reports()[0]["source"], "plugin")


if __name__ == "__main__":
    unittest.main()
