"""Destructive cleanup date and source-boundary regression tests."""

import importlib
import sys
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path

_PACKAGE = "_amikuda_message_request_tests"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[_PACKAGE] = _package
_validation = importlib.import_module(f"{_PACKAGE}.message_management")


class MessageRequestTests(unittest.TestCase):
    def setUp(self):
        self.groups = {"qq:1001": "授权群"}
        self.now = int(datetime(2026, 11, 19, 8, tzinfo=UTC).timestamp())

    def test_cleanup_before_excludes_chosen_shanghai_day(self):
        key, start, end = _validation.cleanup_filters(
            {"source": "plugin", "group": "qq:1001", "mode": "before", "before": "2026-11-17"},
            self.groups, self.now,
        )
        self.assertEqual(key, "qq:1001")
        self.assertIsNone(start)
        self.assertEqual(end, int(datetime(2026, 11, 16, 16, tzinfo=UTC).timestamp()))

    def test_single_day_cleanup_includes_entire_local_day(self):
        _, start, end = _validation.cleanup_filters(
            {"source": "plugin", "group": "qq:1001", "mode": "dates", "start": "2026-11-17", "end": "2026-11-17"},
            self.groups, self.now,
        )
        self.assertEqual(start, int(datetime(2026, 11, 16, 16, tzinfo=UTC).timestamp()))
        self.assertEqual(end, int(datetime(2026, 11, 17, 16, tzinfo=UTC).timestamp()))

    def test_native_and_unauthorized_cleanup_are_rejected(self):
        for source, group in (("astrbot", "qq:1001"), ("plugin", "qq:1002")):
            with self.subTest(source=source, group=group), self.assertRaises(ValueError):
                _validation.cleanup_filters(
                    {"source": source, "group": group, "mode": "before", "before": "2026-11-17"},
                    self.groups, self.now,
                )

    def test_reversed_future_and_missing_cleanup_dates_are_rejected(self):
        for payload in (
            {"mode": "dates", "start": "2026-11-18", "end": "2026-11-17"},
            {"mode": "before", "before": "2026-11-20"},
            {"mode": "before", "before": ""},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _validation.cleanup_filters(
                    {"source": "plugin", "group": "qq:1001", **payload}, self.groups, self.now,
                )

    def test_native_cursor_retains_microseconds_and_rejects_invalid_id(self):
        cursor = _validation.message_cursor(
            {"at": "2026-11-17T00:00:00.123456+08:00", "id": "astrbot-42"}, "astrbot",
        )
        self.assertEqual(cursor["at"], "2026-11-16T16:00:00.123456+00:00")
        with self.assertRaises(ValueError):
            _validation.message_cursor({"at": cursor["at"], "id": "platform-message"}, "astrbot")


if __name__ == "__main__":
    unittest.main()
