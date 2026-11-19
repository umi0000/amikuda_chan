"""Platform-neutral group scopes, with adapter-specific session isolation."""

import re
from collections.abc import Mapping

_ONEBOT_MEMBER_SESSION = re.compile(r"[0-9]+_([0-9]+)")


def group_scopes(entries: list[str], platform_types: Mapping[str, str]) -> dict[str, str]:
    groups = {}
    for entry in entries:
        parts = entry.strip().split(":", 2) if isinstance(entry, str) else []
        if (
            len(parts) != 3
            or parts[1] != "GroupMessage"
            or not parts[0]
            or not parts[2]
            or any(char.isspace() or ord(char) < 32 for char in "".join(parts))
        ):
            raise ValueError("请选择群聊 UMO，格式为 平台实例ID:GroupMessage:会话ID；可从目标群的 /sid 获取。")
        platform_id, _, session_id = parts
        # Decode only the conventions of a known adapter, never arbitrary IDs.
        adapter_type = platform_types.get(platform_id)
        if adapter_type == "aiocqhttp":
            match = _ONEBOT_MEMBER_SESSION.fullmatch(session_id)
            if match:
                session_id = match[1]
        elif adapter_type in ("qq_official", "qq_official_webhook"):
            sender, separator, group = session_id.rpartition("_")
            if separator and sender and group:
                session_id = group
        key = f"{platform_id}:{session_id}"
        if len(key) > 512:
            raise ValueError("采集群 UMO 过长。")
        groups[key] = f"{platform_id} · {session_id}"
    if len(groups) > 32:
        raise ValueError("最多配置 32 个采集群。")
    return groups


def collection_key(
    platform_id: str, source_session_id: str, group_id: str, groups: Mapping[str, str]
) -> str | None:
    # Some adapters expose a channel session distinct from their guild/group ID.
    # message_obj.session_id is the original adapter session before isolation.
    for session_id in (source_session_id, group_id):
        if session_id:
            key = f"{platform_id}:{session_id}"
            if key in groups:
                return key
    return None
