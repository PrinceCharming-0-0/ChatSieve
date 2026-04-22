import subprocess
import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class WeChatClient:
    def __init__(self, group_name: str, days: int = 1):
        self.group_name = group_name
        self.days = days

    def get_recent_messages(self, limit: int = 200) -> List[Dict]:
        """获取最近的消息，返回结构化列表（上限 limit 条）。"""
        cmd = [
            "wechat-cli", "history", self.group_name,
            "--limit", str(limit),
            "--format", "json",
        ]
        if self.days and self.days > 0:
            start_date = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")
            cmd.extend(["--start-time", start_date])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            raw_output = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.error(
                "wechat-cli history 命令失败 (群: %s): %s\nstderr: %s",
                self.group_name, e, e.stderr,
            )
            # 尝试模糊匹配群名
            matched = self._fuzzy_match_group(self.group_name)
            if matched:
                raise ValueError(
                    f"未找到微信群: {self.group_name}，您是否在找: {matched}"
                ) from e
            raise ValueError(f"未找到微信群: {self.group_name}") from e

        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError as e:
            logger.error("解析 history JSON 失败 (群: %s): %s", self.group_name, e)
            return []

        raw_messages: List[str] = data.get("messages", [])

        parsed: List[Dict] = []
        for line in raw_messages:
            item = self._parse_message_line(line)
            if item is None:
                continue
            if not item.get("sender") or not item.get("content"):
                continue
            parsed.append(item)

        parsed.sort(key=lambda x: x["timestamp"])
        return parsed

    def get_group_list(self, limit: int = 20) -> List[Dict]:
        """获取最近会话列表（用于确认群名是否存在）。"""
        cmd = ["wechat-cli", "sessions", "--limit", str(limit)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            raw_output = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.error("wechat-cli sessions 命令失败: %s\nstderr: %s", e, e.stderr)
            return []

        try:
            sessions = json.loads(raw_output)
        except json.JSONDecodeError as e:
            logger.error("解析 sessions JSON 失败: %s", e)
            return []

        return sessions if isinstance(sessions, list) else []

    def _fuzzy_match_group(self, name: str) -> Optional[str]:
        """从 sessions 列表中模糊匹配群名，返回最接近的群名或 None。"""
        try:
            sessions = self.get_group_list(limit=50)
        except Exception:
            return None

        name_lower = name.lower()
        candidates = []
        for session in sessions:
            chat = session.get("chat", "")
            if name_lower in chat.lower() or chat.lower() in name_lower:
                candidates.append(chat)

        return candidates[0] if candidates else None

    def _parse_message_line(self, line: str) -> Optional[Dict]:
        """解析形如 '[2026-04-03 22:15] 发送者: 消息内容' 的行。支持多行消息内容。"""
        pattern = r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.+?): (.+)$"
        match = re.match(pattern, line.strip(), re.DOTALL)
        if not match:
            return None

        ts_str, sender, content = match.group(1), match.group(2), match.group(3)

        try:
            timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except ValueError:
            logger.warning("无法解析时间戳: %s", ts_str)
            return None

        return {
            "sender": sender.strip(),
            "content": content.strip(),
            "timestamp": timestamp,
            "raw": line,
        }
