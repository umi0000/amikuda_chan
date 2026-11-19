"""Read retained AstrBot group messages without copying them into the archive."""

from collections.abc import Mapping
from datetime import UTC, datetime

from astrbot.core.db.po import PlatformMessageHistory
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .collection import group_scopes
from .reporting import SHANGHAI, iso

_HISTORY_NOTE = (
    "AstrBot 历史按配置滚动留存，无法确认所选时段的完整性；时间按入库时间统计。"
)
_MEDIA_PLACEHOLDERS = {"[image]", "[record]", "[video]", "[file]", "[face]", "[json]"}
_PAGE_SIZE = 500


class HistoryReadError(Exception):
    """A native-history read failure safe to display in the console."""


def _text(content: dict) -> str:
    if not isinstance(content, dict) or content.get("type") != "user":
        return ""
    messages = content.get("message")
    if not isinstance(messages, list):
        return ""
    parts = []
    for part in messages:
        if not isinstance(part, dict) or part.get("type") != "plain":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip().lower() not in _MEDIA_PLACEHOLDERS:
            parts.append(text)
    return "".join(parts).strip()


async def read_history(
    manager,
    groups: Mapping[str, str],
    platform_types: Mapping[str, str],
    start: int,
    end: int,
    now: int,
) -> tuple[list[dict], list[dict]]:
    """Select authorized retained rows through AstrBot's managed DB session.

    Public manager pagination is per exact UMO and has no date or scope-discovery
    filter. Read-only ORM queries also find QQ member-isolated UMOs, and fix
    an upper row ID so concurrent new arrivals cannot shift offset pages.
    """
    platform_ids = {key.split(":", 1)[0] for key in groups}
    records = []
    retained = {key: {"count": 0, "first": None, "last": None, "in_range": 0} for key in groups}
    history = PlatformMessageHistory
    active = getattr(history, "is_active", None)
    active_filters = [active.is_(True)] if active is not None else []
    try:
        async with manager.db.get_db() as db:
            candidates = await db.execute(
                select(
                    history.platform_id, history.user_id, func.count(history.id),
                    func.min(history.created_at), func.max(history.created_at),
                )
                .where(history.platform_id.in_(platform_ids), *active_filters)
                .group_by(history.platform_id, history.user_id)
            )
            scopes = {}
            for platform_id, umo, count, first_at, last_at in candidates:
                if not isinstance(umo, str) or not umo.startswith(f"{platform_id}:GroupMessage:"):
                    continue
                try:
                    normalized = group_scopes([umo], platform_types)
                except ValueError:
                    continue
                key = next(iter(normalized))
                if key in groups:
                    scopes[(platform_id, umo)] = key
                    stats = retained[key]
                    stats["count"] += count
                    stats["first"] = min(stats["first"], first_at) if stats["first"] else first_at
                    stats["last"] = max(stats["last"], last_at) if stats["last"] else last_at

            for (platform_id, umo), key in scopes.items():
                filters = [
                    history.platform_id == platform_id,
                    history.user_id == umo,
                    history.created_at >= datetime.fromtimestamp(start, UTC),
                    history.created_at < datetime.fromtimestamp(min(end, now), UTC),
                    *active_filters,
                ]
                upper = (await db.execute(select(func.max(history.id)).where(*filters))).scalar_one()
                if upper is None:
                    continue
                after = 0
                while True:
                    batch = (await db.execute(
                        select(history.id, history.created_at, history.sender_id, history.content)
                        .where(*filters, history.id > after, history.id <= upper)
                        .order_by(history.id)
                        .limit(_PAGE_SIZE)
                    )).all()
                    if not batch:
                        break
                    for ident, created_at, sender_id, content in batch:
                        retained[key]["in_range"] += 1
                        text = _text(content)
                        if not text:
                            continue
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=UTC)
                        records.append({
                            "key": key,
                            "message_id": f"astrbot-{ident}",
                            "at": int(created_at.timestamp()),
                            "sender": str(sender_id or "unknown"),
                            "text": text,
                            # Native reply segments identify platform messages, not
                            # native rows; do not fabricate a DB-row reference.
                            "reply_to": None,
                        })
                    after = batch[-1][0]
                    if len(batch) < _PAGE_SIZE:
                        break
    except SQLAlchemyError as exc:
        raise HistoryReadError("读取 AstrBot 持久化群聊记录失败，请检查核心版本与数据库状态。") from exc

    records.sort(key=lambda row: (row["at"], row["key"], row["message_id"]))
    counts = {key: 0 for key in groups}
    first = {}
    for row in records:
        counts[row["key"]] += 1
        first.setdefault(row["key"], row["at"])

    def retention_note(key: str) -> str:
        stats = retained[key]
        if not stats["count"]:
            return "未找到当前平台实例与群 UMO 对应的持久化记录。请核对平台实例 ID、群 UMO 和持久化配置。" + _HISTORY_NOTE
        def local_stamp(value: datetime) -> str:
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(SHANGHAI).strftime("%Y/%m/%d %H:%M:%S")
        message = (
            f"该群当前留存 {stats['count']} 条记录（含机器人及媒体记录）；"
            f"入库时间范围：{local_stamp(stats['first'])} 至 {local_stamp(stats['last'])}（北京时间）。"
        )
        if not stats["in_range"]:
            message += "所选时间范围内没有留存记录。"
        elif not counts[key]:
            message += f"所选时段有 {stats['in_range']} 条记录，但没有可供分析的用户文本（可能为机器人输出或媒体）。"
        return message + _HISTORY_NOTE

    coverage = [{
        "key": key,
        "label": label,
        "count": counts[key],
        "partial": None,
        "first_at": iso(first[key]) if key in first else None,
        "started_at": None,
        "gaps": [],
        "note": retention_note(key),
    } for key, label in groups.items()]
    return coverage, records
