import os
import json
import logging
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# 支持多个追踪器共享同一个状态文件，用不同的 key 区分
STATE_FILE = Path(__file__).parent / "token_state.json"


class TokenTracker:
    """
    追踪 token 用量，支持日/月的累计统计。
    阈值预警：当日用量超过 daily_limit 的指定比例时触发警告。
    支持多个追踪维度（通过 tracker_key 区分）。
    """

    def __init__(
        self,
        daily_limit: int = 100000,
        warning_ratio: float = 0.9,
        tracker_key: str = "text",
    ):
        self.daily_limit = daily_limit
        self.warning_ratio = warning_ratio
        self.tracker_key = tracker_key  # 区分不同追踪维度（text / vision）
        self._load_state()

    def _load_state(self):
        """从 token_state.json 加载状态。"""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"加载 token_state.json 失败，使用默认状态: {e}")
                data = {}
        else:
            data = {}

        self._last_date = data.get("last_date", "")
        # 按 tracker_key 分区存储
        self._daily_tokens: Dict[str, int] = data.get(f"{self.tracker_key}_daily_tokens", {})
        self._monthly_tokens: Dict[str, int] = data.get(f"{self.tracker_key}_monthly_tokens", {})

    def _save_state(self):
        """保存状态到 token_state.json。"""
        # 读取现有数据，保留其他 tracker 的状态
        data = {}
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        data["last_date"] = self._last_date
        data[f"{self.tracker_key}_daily_tokens"] = self._daily_tokens
        data[f"{self.tracker_key}_monthly_tokens"] = self._monthly_tokens

        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存 token_state.json 失败: {e}")

    def add_usage(self, tokens: int, date_label: Optional[str] = None):
        """记录一次 token 使用。"""
        if date_label is None:
            date_label = datetime.now().strftime("%Y-%m-%d")

        # 更新日计数
        self._daily_tokens[date_label] = self._daily_tokens.get(date_label, 0) + tokens

        # 更新月计数 yyyy-mm
        month_label = date_label[:7]
        self._monthly_tokens[month_label] = self._monthly_tokens.get(month_label, 0) + tokens

        self._save_state()

    def get_daily_usage(self, day: Optional[str] = None) -> int:
        """获取某日的 token 用量。"""
        if day is None:
            day = datetime.now().strftime("%Y-%m-%d")
        return self._daily_tokens.get(day, 0)

    def get_total_usage(self) -> int:
        """获取所有日期的累计总用量。"""
        return sum(self._daily_tokens.values())

    def check_warning(self) -> Optional[str]:
        """
        检查是否超过日额度阈值。
        返回警告消息（str）或 None（正常）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        used = self._daily_tokens.get(today, 0)
        ratio = used / self.daily_limit if self.daily_limit > 0 else 0
        if ratio >= self.warning_ratio:
            pct = round(ratio * 100)
            label = "文本模型" if self.tracker_key == "text" else "视觉模型"
            return f"⚠️ {label} Token 预警: 今日已使用 {used}/{self.daily_limit} ({pct}%)"
        return None

    def check_total_warning(self, total_limit: int) -> Optional[str]:
        """
        检查累计总用量是否超过总额度阈值。
        返回警告消息（str）或 None（正常）。
        """
        used = self.get_total_usage()
        ratio = used / total_limit if total_limit > 0 else 0
        if ratio >= self.warning_ratio:
            pct = round(ratio * 100)
            label = "文本模型" if self.tracker_key == "text" else "视觉模型"
            return f"⚠️ {label} Token 预警: 累计已使用 {used}/{total_limit} ({pct}%)"
        return None

    def reset_if_new_day(self):
        """检查是否进入新日期，若是则清理旧日期数据。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_date and self._last_date != today:
            # 清理旧日期的日计数（保留最近 7 天以供查询）
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            self._daily_tokens = {
                d: c for d, c in self._daily_tokens.items() if d >= cutoff
            }
            logger.info("日期切换: %s -> %s，日计数已清理", self._last_date, today)
        self._last_date = today
        self._save_state()
