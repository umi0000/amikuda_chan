"""Validation for message browsing and explicitly confirmed archive cleanup."""

import re
from datetime import UTC, date, datetime, time, timedelta

from .reporting import SHANGHAI

MESSAGE_PAGE_SIZE = 50


def date_boundary(value: object, *, next_day: bool = False) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("请输入有效日期，格式为 YYYY-MM-DD。")
    try:
        day = date.fromisoformat(value)
        if next_day:
            day += timedelta(days=1)
        return int(datetime.combine(day, time.min, SHANGHAI).timestamp())
    except (ValueError, OverflowError) as exc:
        raise ValueError("请输入有效的日期。") from exc


def message_filters(data: object, groups: dict[str, str], now: int) -> tuple[str, str, int | None, int | None]:
    if not isinstance(data, dict):
        raise TypeError("消息查询参数无效。")
    source, key = data.get("source"), data.get("group")
    if source not in ("plugin", "astrbot"):
        raise ValueError("请选择插件归档或 AstrBot 历史。")
    if not isinstance(key, str) or key not in groups:
        raise ValueError("请选择当前已监控的群。")
    start = date_boundary(data["start"]) if data.get("start") else None
    end = date_boundary(data["end"], next_day=True) if data.get("end") else None
    if start is not None and end is not None and start >= end:
        raise ValueError("结束日期不能早于开始日期。")
    if end is not None:
        end = min(end, now + 1)
    return source, key, start, end


def message_cursor(value: object, source: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"at", "id"}:
        raise ValueError("分页游标无效，请重新查询。")
    at, ident = value["at"], value["id"]
    if not isinstance(ident, str) or not ident or len(ident) > 512 or not isinstance(at, str):
        raise ValueError("分页游标无效，请重新查询。")
    try:
        stamp = datetime.fromisoformat(at)
        if stamp.tzinfo is None:
            raise ValueError
        utc = stamp.astimezone(UTC)
        if utc.year < 1970:
            raise ValueError
        if source == "astrbot" and not re.fullmatch(r"astrbot-[1-9][0-9]*", ident):
            raise ValueError
    except ValueError as exc:
        raise ValueError("分页游标无效，请重新查询。") from exc
    return {"at": utc.isoformat(), "id": ident}


def cleanup_filters(data: object, groups: dict[str, str], now: int) -> tuple[str, int | None, int]:
    if not isinstance(data, dict):
        raise TypeError("清理参数无效。")
    if data.get("source") != "plugin":
        raise ValueError("仅允许清理插件归档，AstrBot 历史为只读。")
    key = data.get("group")
    if not isinstance(key, str) or key not in groups:
        raise ValueError("请选择当前已监控的群。")
    mode = data.get("mode")
    if mode == "before":
        start, end = None, date_boundary(data.get("before"))
    elif mode == "dates":
        start = date_boundary(data.get("start"))
        end = date_boundary(data.get("end"), next_day=True)
        if start >= end:
            raise ValueError("结束日期不能早于开始日期。")
    else:
        raise ValueError("请选择删除某日之前或指定日期范围。")
    if end > now:
        raise ValueError("清理范围不能包含尚未结束的日期，请选择已结束的日期。")
    return key, start, end
