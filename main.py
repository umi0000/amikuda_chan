"""AstrBot adapter for the campus-network report console."""

import asyncio
import sqlite3
import time
from collections.abc import Callable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

from .collection import collection_key, group_scopes
from .history_source import HistoryReadError, read_history
from .reporting import (
    DEFAULT_PROMPT,
    MAX_PROMPT,
    ReportGenerationError,
    iso,
    resolve_range,
    summarize,
)
from .storage import Store

PLUGIN_NAME = "astrbot_plugin_amikuda_chan"
_HEARTBEAT_SECONDS = 30


def _message_data(event: AstrMessageEvent) -> tuple[int, str, str, str | None]:
    reply_to = None
    text = []
    has_text = False
    for component in event.get_messages():
        if isinstance(component, Plain):
            text.append(component.text)
            has_text |= bool(component.text.strip())
        elif isinstance(component, At):
            target = str(component.qq)
            text.append(f" @{target} " if target != "all" else " @全体成员 ")
        elif isinstance(component, Reply) and component.id:
            reply_to = str(component.id)
    joined = "".join(text).strip() if has_text else ""
    raw = event.message_obj.raw_message
    at = raw.get("time") if isinstance(raw, dict) else None
    if type(at) is not int or at <= 0:
        at = event.message_obj.timestamp or int(time.time())
    return int(at), str(event.get_sender_id()), joined, reply_to


class AmikudaChan(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._platform_types = {
            platform["id"]: platform["type"]
            for platform in context.get_config().get("platform", [])
        }
        self._groups = group_scopes(config.get("collect_groups", []), self._platform_types)
        self._retention = config.get("retention_days", 30)
        if type(self._retention) is not int or not 1 <= self._retention <= 366:
            raise ValueError("正文保留天数须为 1–366 的整数。")
        self._task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._closing = False
        self._store: Store

    async def initialize(self) -> None:
        path = StarTools.get_data_dir(PLUGIN_NAME)
        path.mkdir(parents=True, exist_ok=True)
        self._store = Store(path / "reports.sqlite3")
        await asyncio.to_thread(self._store.open, self._groups, int(time.time()), self._retention)
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        routes: tuple[tuple[str, str, Callable], ...] = (
            ("bootstrap", "GET", self.bootstrap),
            ("settings", "POST", self.save_settings),
            ("preview", "POST", self.preview),
            ("reports", "GET", self.reports),
            ("reports", "POST", self.create_report),
            ("reports/<report_id>", "GET", self.report),
        )
        for path, method, handler in routes:
            self.context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, [method], path)

    async def terminate(self) -> None:
        self._closing = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if hasattr(self, "_store"):
            await asyncio.to_thread(self._store.close, int(time.time()))

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            try:
                await asyncio.to_thread(self._store.heartbeat, int(time.time()))
            except sqlite3.Error as exc:
                logger.error(f"{PLUGIN_NAME}: 更新采集状态失败 ({type(exc).__name__})")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def collect(self, event: AstrMessageEvent) -> None:
        key = collection_key(
            event.get_platform_id(), event.message_obj.session_id, event.get_group_id(), self._groups,
        )
        if self._closing or key is None or event.get_sender_id() == event.get_self_id():
            return
        message_id = event.message_obj.message_id
        if not message_id:
            return
        at, sender, text, reply_to = _message_data(event)
        if text:
            try:
                await asyncio.to_thread(self._store.record, key, str(message_id), at, sender, text, reply_to)
            except sqlite3.Error as exc:
                logger.error(f"{PLUGIN_NAME}: 群消息写入失败 ({type(exc).__name__})")

    def _models(self) -> dict[str, str]:
        models = {}
        for provider in self.context.get_all_providers():
            meta = provider.meta()
            models[meta.id] = f"{meta.model} · {meta.id}" if meta.model and meta.model != meta.id else meta.id
        return models

    async def bootstrap(self):
        settings = await asyncio.to_thread(self._store.settings, DEFAULT_PROMPT)
        return json_response({
            "groups": [{"key": key, "label": label} for key, label in self._groups.items()],
            "models": [{"id": ident, "label": label} for ident, label in self._models().items()],
            "settings": settings,
        })

    async def save_settings(self):
        if self._closing:
            return error_response("插件正在重载，请稍后再试。", status_code=503)
        data = await request.json(default={})
        if not isinstance(data, dict):
            return error_response("配置格式无效。", status_code=400)
        provider_id, prompt = data.get("provider_id"), data.get("prompt")
        if not isinstance(provider_id, str) or provider_id not in self._models():
            return error_response("请选择 AstrBot 中已启用的聊天模型。", status_code=400)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > MAX_PROMPT:
            return error_response(f"请输入总结提示词，最多 {MAX_PROMPT} 字符。", status_code=400)
        prompt = prompt.strip()
        await asyncio.to_thread(self._store.save_settings, provider_id, prompt)
        return json_response({"provider_id": provider_id, "prompt": prompt})

    def _selection(self, data: object, now: int) -> tuple[dict[str, str], int, int, str]:
        if not isinstance(data, dict):
            raise TypeError("报告参数无效。")
        source = data.get("source")
        if source not in ("plugin", "astrbot"):
            raise ValueError("请选择消息来源：插件归档或 AstrBot 历史。")
        keys = data.get("groups")
        if not isinstance(keys, list) or not keys or len(keys) > len(self._groups):
            raise ValueError("请至少选择一个已授权采集的群。")
        if any(not isinstance(key, str) or key not in self._groups for key in keys):
            raise ValueError("请只选择当前允许采集的群。")
        groups = {key: self._groups[key] for key in keys}
        start, end = resolve_range(data.get("range"), now)
        return groups, start, end, source

    async def _native_snapshot(self, groups: dict[str, str], start: int, end: int, now: int):
        return await read_history(
            self.context.message_history_manager, groups, self._platform_types, start, end, now,
        )

    async def preview(self):
        now = int(time.time())
        try:
            groups, start, end, source = self._selection(await request.json(default={}), now)
            if source == "astrbot":
                coverage, _ = await self._native_snapshot(groups, start, end, now)
            else:
                coverage = await asyncio.to_thread(self._store.coverage, groups, start, end, now)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except HistoryReadError as exc:
            return error_response(str(exc), status_code=503)
        return json_response({"source": source, "start": iso(start), "end": iso(end), "count": sum(group["count"] for group in coverage), "coverage": coverage})

    async def reports(self):
        return json_response({"reports": await asyncio.to_thread(self._store.list_reports)})

    async def report(self, report_id: str):
        result = await asyncio.to_thread(self._store.report, report_id)
        if result is None:
            return error_response("找不到这份报告。", status_code=404)
        return json_response(result)

    async def create_report(self):
        now = int(time.time())
        if self._closing:
            return error_response("插件正在重载，请稍后再试。", status_code=503)
        try:
            groups, start, end, source = self._selection(await request.json(default={}), now)
            settings = await asyncio.to_thread(self._store.settings, DEFAULT_PROMPT)
            provider_id = settings["provider_id"]
            provider_label = self._models().get(provider_id)
            if provider_label is None:
                raise ValueError("请先在配置页保存一个可用的总结模型。")
            snapshot = await self._native_snapshot(groups, start, end, now) if source == "astrbot" else None
            report, rows = await asyncio.to_thread(
                self._store.create, groups, start, end, now, provider_id, provider_label, settings["prompt"],
                source=source, snapshot=snapshot,
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)
        except HistoryReadError as exc:
            return error_response(str(exc), status_code=503)
        if self._closing:
            await asyncio.to_thread(self._store.finish, report["id"], "failed", None, "插件重载，生成已中断。")
            return error_response("插件正在重载，请稍后再试。", status_code=503)
        self._task = asyncio.create_task(self._generate(report, rows))
        return json_response({key: value for key, value in report.items() if key not in {"prompt", "body", "error", "coverage"}})

    async def _generate(self, report: dict, rows: list[dict]) -> None:
        async def call_model(system: str, prompt: str, output_limit: int) -> str:
            async with asyncio.timeout(120):
                resp = await self.context.llm_generate(
                    chat_provider_id=report["provider_id"], system_prompt=system, prompt=prompt,
                    max_tokens=output_limit,
                )
            text = resp.completion_text
            if not text or not text.strip():
                raise ReportGenerationError("模型没有返回可读内容。")
            return text.strip()

        ident = report["id"]
        try:
            body = await summarize(call_model, report, rows)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._store.finish, ident, "failed", None, "插件重载，生成已中断。")
            raise
        except TimeoutError:
            await asyncio.to_thread(self._store.finish, ident, "failed", None, "模型请求超时，请重试或缩短统计范围。")
        except ReportGenerationError as exc:
            await asyncio.to_thread(self._store.finish, ident, "failed", None, str(exc))
        except Exception as exc:  # noqa: BLE001 - providers expose different SDK errors
            logger.warning(f"{PLUGIN_NAME}: 报告 {ident} 的模型请求失败 ({type(exc).__name__})")
            await asyncio.to_thread(self._store.finish, ident, "failed", None, "模型调用失败，请检查 AstrBot 的模型配置、额度或上下文上限。")
        else:
            await asyncio.to_thread(self._store.finish, ident, "completed", body, None)
