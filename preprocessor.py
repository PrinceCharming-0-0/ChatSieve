import re
import logging
from typing import List, Dict
from collections import Counter

logger = logging.getLogger(__name__)

# Unicode 纯表情符号范围（部分）
EMOJI_PATTERN = re.compile(
    r"^[\U0001F300-\U0001F9FF"
    r"\U00002600-\U000026FF"
    r"\U00002700-\U000027BF"
    r"\U0001F600-\U0001F64F"
    r"\U0001F680-\U0001F6FF"
    r"\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FA6F"
    r"\U0001FA70-\U0001FAFF"
    r"]+$"
)


def is_emoji_only(text: str) -> bool:
    """判断文本是否纯表情（无文字）。"""
    text = text.strip()
    if not text:
        return False
    # 去除 [表情] 标记后再判断
    text_clean = re.sub(r'^\[表情\]$', '', text).strip()
    if not text_clean:
        return True
    return bool(EMOJI_PATTERN.match(text_clean))


def preprocess(messages: List[Dict], min_length: int = 2) -> List[Dict]:
    """
    预处理消息列表：
    1. 过滤短消息（< min_length 字符，不计空白）
    2. 过滤纯表情消息
    3. 合并连续重复内容（同一发送者连续发送相同内容）
    """
    total = len(messages)
    short_count = 0
    emoji_count = 0

    filtered: List[Dict] = []
    for msg in messages:
        content = msg.get("content", "")
        stripped = content.strip()

        if len(stripped) < min_length:
            short_count += 1
            continue

        if is_emoji_only(stripped):
            emoji_count += 1
            continue

        filtered.append(msg)

    merged = merge_consecutive_duplicates(filtered)
    merged_reduction = len(filtered) - len(merged)

    logger.info(
        "预处理完成: 原始%d条 → 过滤后%d条 (过滤%d条短消息, %d条表情, 合并减少%d条)",
        total, len(merged), short_count, emoji_count, merged_reduction,
    )

    return merged


def merge_consecutive_duplicates(messages: List[Dict]) -> List[Dict]:
    """合并连续重复消息，保留第一条时间戳。"""
    if not messages:
        return []

    result: List[Dict] = []
    prev = dict(messages[0])
    prev.setdefault("_merged_count", 1)

    for msg in messages[1:]:
        prev_sender = prev.get("sender", "").strip()
        prev_content = prev.get("content", "").strip()
        curr_sender = msg.get("sender", "").strip()
        curr_content = msg.get("content", "").strip()

        if prev_sender == curr_sender and prev_content == curr_content:
            prev["_merged_count"] = prev.get("_merged_count", 1) + 1
        else:
            result.append(prev)
            prev = dict(msg)
            prev.setdefault("_merged_count", 1)

    result.append(prev)
    return result


_SYSTEM_PATTERNS = [
    r"撤回了一条消息",
    r"加入了群聊",
    r"离开了群聊",
    r"修改群名为",
    r"邀请.*加入了群聊",
    r"你通过.*加入了群聊",
    r"已成为群聊管理员",
    r"被群主移出群聊",
]
_SYSTEM_PATTERN = "|".join(_SYSTEM_PATTERNS)


def remove_system(messages: List[Dict]) -> List[Dict]:
    """剔除系统通知（如"撤回消息"、"加入群聊"等）。"""
    return [m for m in messages if not re.search(_SYSTEM_PATTERN, m.get("content", ""))]
