"""Regression coverage for cross-platform group collection boundaries."""

import tempfile
import unittest
from pathlib import Path

from collection import collection_key, group_scopes
from storage import Store


class GroupScopeTests(unittest.TestCase):
    def test_non_numeric_group_scopes_are_archived_without_id_rewriting(self):
        sessions = {
            "telegram": "-100123456",
            "matrix": "!room_with_underscores:example.org",
            "official": "OPAQUEGROUP123456",
            "other": "123_456",
        }
        groups = group_scopes(
            [f"{platform}:GroupMessage:{session}" for platform, session in sessions.items()],
            {"telegram": "telegram", "matrix": "matrix", "official": "qq_official"},
        )
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "archive.db")
            store.open(groups, 1000, 30)
            for platform, session in sessions.items():
                key = collection_key(platform, session, session, groups)
                self.assertIsNotNone(key)
                self.assertEqual(key, f"{platform}:{session}")
                store.record(key, "same-id", 1001, "anonymous", "校园网无法连接", None)
            coverage = store.coverage(groups, 1000, 1003, 1003)
            self.assertTrue(all(group["count"] == 1 for group in coverage))
            self.assertIsNone(collection_key("unauthorized", "-100123456", "-100123456", groups))
            self.assertIsNone(collection_key("other", "456", "456", groups))

    def test_onebot_member_umo_matches_other_members_and_preserves_archive_key(self):
        groups = group_scopes(
            ["qq:GroupMessage:8888_1001", "qq:GroupMessage:1001"],
            {"qq": "aiocqhttp"},
        )
        self.assertEqual(list(groups), ["qq:1001"])
        self.assertEqual(collection_key("qq", "1001", "1001", groups), "qq:1001")
        self.assertIsNone(collection_key("qq", "1002", "1002", groups))

    def test_official_qq_member_umos_resolve_to_authorized_group(self):
        group = "7448B45651C15773CB392D39186A6F38"
        for adapter in ("qq_official", "qq_official_webhook"):
            with self.subTest(adapter=adapter):
                scopes = group_scopes([
                    f"xduna-bot:GroupMessage:{group}",
                    f"xduna-bot:GroupMessage:MEMBER_A_{group}",
                    f"xduna-bot:GroupMessage:MEMBER_B_{group}",
                ], {"xduna-bot": adapter})
                self.assertEqual(list(scopes), [f"xduna-bot:{group}"])
                self.assertEqual(collection_key("xduna-bot", group, group, scopes), f"xduna-bot:{group}")

    def test_channel_session_does_not_collect_other_channels_in_same_guild(self):
        groups = group_scopes(["discord:GroupMessage:channel-a"], {})
        self.assertEqual(collection_key("discord", "channel-a", "guild", groups), "discord:channel-a")
        self.assertIsNone(collection_key("discord", "channel-b", "guild", groups))

    def test_private_and_malformed_sources_are_rejected(self):
        for value in ("tg:FriendMessage:-1001", "tg:OtherMessage:room", "tg:GroupMessage:",
                      ":GroupMessage:room", "tg:GroupMessage:two words", "tg:room", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                group_scopes([value], {})


if __name__ == "__main__":
    unittest.main()
