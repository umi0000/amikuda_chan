"""Native history regression tests; requires an installed AstrBot runtime."""

import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

_HAS_ASTRBOT = importlib.util.find_spec("astrbot") is not None


@unittest.skipUnless(_HAS_ASTRBOT, "requires AstrBot and its database dependencies")
class NativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from astrbot.core.db.po import PlatformMessageHistory
        from astrbot.core.db.sqlite import SQLiteDatabase

        package_name = "_amikuda_history_regression"
        package = types.ModuleType(package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules[package_name] = package
        cls.message_data = staticmethod(importlib.import_module(f"{package_name}.main")._message_data)
        cls.read_history = staticmethod(importlib.import_module(f"{package_name}.history_source").read_history)
        cls.browse_history = staticmethod(importlib.import_module(f"{package_name}.history_source").browse_history)
        cls.model = PlatformMessageHistory
        cls.database = SQLiteDatabase

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = self.database(str(Path(self.directory.name) / "native.db"))
        await self.db.initialize()
        self.manager = SimpleNamespace(db=self.db)
        self.group = "7448B45651C15773CB392D39186A6F38"
        self.key = f"xduna-bot:{self.group}"
        self.groups = {self.key: "授权群"}
        self.types = {"xduna-bot": "qq_official"}

    async def asyncTearDown(self):
        await self.db.engine.dispose()

    async def insert(self, umo, at=2000, text="校园网断网", role="user", platform="xduna-bot"):
        async with self.db.get_db() as db, db.begin():
            db.add(self.model(
                platform_id=platform, user_id=umo, sender_id="private-sender",
                content={"type": role, "message": [{"type": "plain", "text": text}]},
                created_at=datetime.fromtimestamp(at, UTC),
            ))

    async def read(self, start=1000, end=3000):
        return await self.read_history(
            self.manager, self.groups, self.types, start, end, 4000,
        )

    async def test_reads_official_member_sessions_without_cross_group_or_private_leak(self):
        for member in ("MEMBERA", "MEMBERB"):
            await self.insert(f"xduna-bot:GroupMessage:{member}_{self.group}", text="校园网断网；账号密码是 demo value；邮件 demo@example.org")
        await self.insert("xduna-bot:GroupMessage:MEMBERA_DIFFERENTGROUP", text="wrong-group")
        await self.insert(f"xduna-bot:FriendMessage:{self.group}", text="private")
        await self.insert(f"xduna-bot:GroupMessage:MEMBERA_{self.group}", text="wrong-platform", platform="other")
        coverage, rows = await self.read()
        self.assertEqual([row["text"] for row in rows], ["校园网断网；账号密码是 demo value；邮件 demo@example.org"] * 2)
        self.assertEqual(coverage[0]["count"], 2)
        self.assertIn("当前留存 2 条记录", coverage[0]["note"])
        self.assertIn("1970/01/01 08:33:20", coverage[0]["note"])
        self.assertTrue(all(row["sender"] == "private-sender" for row in rows))

    def test_live_capture_keeps_sender_mentions_and_unmodified_text(self):
        from astrbot.core.message.components import At, Plain, Reply

        original = "学号 2026123456；密码是 aa bb；邮箱 demo@example.org"
        event = SimpleNamespace(
            get_messages=lambda: [Plain(text=original), At(qq="member-88", name="Member"), Plain(text="继续"), Reply(id="quote-42")],
            get_sender_id=lambda: "actual-sender-101",
            message_obj=SimpleNamespace(raw_message={"time": 1234}, timestamp=1233),
        )
        self.assertEqual(
            self.message_data(event),
            (1234, "actual-sender-101", f"{original} @member-88 继续", "quote-42"),
        )

    async def test_out_of_range_records_report_retained_time_instead_of_absence(self):
        await self.insert(f"xduna-bot:GroupMessage:MEMBERA_{self.group}")
        coverage, rows = await self.read(start=2500)
        self.assertEqual(rows, [])
        self.assertIn("当前留存 1 条记录", coverage[0]["note"])
        self.assertIn("所选时间范围内没有留存记录", coverage[0]["note"])
        self.assertNotIn("未找到", coverage[0]["note"])

    async def test_absent_scope_and_non_text_records_have_distinct_explanations(self):
        coverage, rows = await self.read()
        self.assertEqual(rows, [])
        self.assertIn("未找到当前平台实例与群 UMO", coverage[0]["note"])
        await self.insert(f"xduna-bot:GroupMessage:{self.group}", text="[image]")
        await self.insert(f"xduna-bot:GroupMessage:{self.group}", text="bot output", role="bot")
        coverage, rows = await self.read()
        self.assertEqual(rows, [])
        self.assertIn("所选时段有 2 条记录，但没有可供分析的用户文本", coverage[0]["note"])

    async def test_browse_includes_bot_and_media_with_stable_same_second_pagination(self):
        umo = f"xduna-bot:GroupMessage:MEMBERA_{self.group}"
        await self.insert(umo, text="first")
        await self.insert(umo, text="bot response", role="bot")
        await self.insert(umo, at=2001, text="[image]")
        await self.insert("xduna-bot:GroupMessage:MEMBERA_OTHERGROUP", text="wrong group")
        await self.insert(f"xduna-bot:FriendMessage:{self.group}", text="private")
        page = await self.browse_history(self.manager, self.key, "授权群", self.types, None, None, None, 2)
        self.assertEqual([row["text"] for row in page["messages"]], ["[image]", "bot response"])
        self.assertEqual(page["messages"][1]["role"], "bot")
        await self.insert(umo, at=2500, text="newer arrival")
        next_page = await self.browse_history(self.manager, self.key, "授权群", self.types, None, None, page["next_cursor"], 2)
        self.assertEqual([row["text"] for row in next_page["messages"]], ["first"])
        self.assertIsNone(next_page["next_cursor"])

    async def test_browse_filters_dates_without_changing_native_history(self):
        from sqlalchemy import func, select

        umo = f"xduna-bot:GroupMessage:{self.group}"
        await self.insert(umo, at=1999, text="before")
        await self.insert(umo, at=2000, text="included")
        await self.insert(umo, at=2001, text="after")
        page = await self.browse_history(self.manager, self.key, "授权群", self.types, 2000, 2001, None)
        self.assertEqual([row["text"] for row in page["messages"]], ["included"])
        async with self.db.get_db() as db:
            count = (await db.execute(select(func.count()).select_from(self.model))).scalar_one()
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
