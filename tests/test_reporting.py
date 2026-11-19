"""Regression tests for preserving selected messages through report generation."""

import json
import unittest

from reporting import summarize


class OriginalMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_receives_original_text_and_result_is_not_rewritten(self):
        message = "学号 2026123456；密码是 aa bb；邮箱 demo@example.org；网关 10.0.0.1"
        output = "报告原样引用：demo@example.org；学号 2026123456。"
        report = {
            "source": "astrbot",
            "start": "2026-11-18T00:00:00+00:00",
            "end": "2026-11-19T00:00:00+00:00",
            "count": 1,
            "groups": [{"key": "qq:1001", "label": "校园网群"}],
            "coverage": [{"key": "qq:1001", "label": "校园网群", "count": 1, "partial": None, "note": ""}],
            "prompt": "按证据总结问题和处理线索。",
        }
        row = {"key": "qq:1001", "message_id": "astrbot-1", "at": 1794960000,
               "sender": "10086", "text": message, "reply_to": None}
        sent = []

        async def generate(system, prompt, output_limit):
            sent.append((system, prompt, output_limit))
            return output

        result = await summarize(generate, report, [row])
        payload = json.loads(sent[0][1].split("\n\n", 1)[1])
        self.assertEqual(payload["messages"][0]["text"], message)
        self.assertEqual(payload["messages"][0]["sender"], "10086")
        self.assertEqual(payload["messages"][0]["ref"], "G1:astrbot-1")
        self.assertEqual(result, output)


if __name__ == "__main__":
    unittest.main()
