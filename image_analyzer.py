#!/usr/bin/env python3
"""
微信群图片分析器
功能：按需识别并分析群聊中的普通图片（排除表情包/贴纸）
依赖：wechat-cli, screencapture, VISION_API_KEY
"""

import os
import re
import json
import base64
import logging
import subprocess
import time
import threading
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from logging_utils import configure_logger

logger = logging.getLogger(__name__)

# ── 路径常量 ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"

# ── 配置常量 ─────────────────────────────────────────────────────────────────
# 表情包判定：宽高阈值 / 文件名关键词
EMOJI_SIZE_THRESHOLD = 240          # 宽或高小于此值视为表情包
EMOJI_KEYWORDS = ["emoji", "sticker", "CustomEmotions"]
# 连续图片合并：时间间隔阈值（秒）
CONSECUTIVE_GAP_SECONDS = 30
# 上下文消息条数（批次首尾各取）
CONTEXT_MESSAGE_COUNT = 3
# 上下文时间窗口（分钟）
CONTEXT_TIME_WINDOW_MINUTES = 5
# 分析重试次数
MAX_RETRIES = 2
# 批次并发数（不同批次可并发分析，同一批次内串行截图）
MAX_CONCURRENT_BATCHES = 3
# 分析结果临时文件
ANALYSIS_TMP_DIR = BASE_DIR / "tmp"
WECHAT_WINDOW_ID_CACHE = BASE_DIR / "wechat_window_id.txt"


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _get_wechat_window_id_via_python() -> Optional[int]:
    """优先使用 Python CoreGraphics 路径获取 WeChat 窗口 ID。"""
    try:
        import Quartz
    except Exception:
        return None

    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
        for window in windows or []:
            owner = window.get("kCGWindowOwnerName")
            if owner == "微信":
                window_id = window.get("kCGWindowNumber")
                if window_id is not None:
                    return int(window_id)
    except Exception as e:
        logger.debug("Python CoreGraphics 获取 WindowID 失败: %s", e)

    return None


def _get_wechat_window_id_via_compiled_tool() -> Optional[int]:
    """回退到 gcc 编译 C 程序获取 WeChat 窗口 ID。"""
    source_file = Path("/tmp/_get_wechat_window_id.c")
    binary_file = Path("/tmp/_get_wechat_window_id")
    try:
        # 将 C 源代码写入文件（macOS clang 不支持 - 从 stdin 读取）
        source_file.write_text(_GET_WECHAT_WINDOW_C_SOURCE, encoding="utf-8")

        result = subprocess.run(
            [
                "gcc", "-o", str(binary_file),
                str(source_file),
                "-framework", "CoreGraphics", "-framework", "CoreFoundation"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning("编译 CGWindowList 工具失败: %s", result.stderr)
            return None

        result2 = subprocess.run(
            [str(binary_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result2.stdout.strip()
        if output:
            return int(output)
    except Exception as e:
        logger.warning("gcc 回退获取 WeChat WindowID 失败: %s", e)
    finally:
        # 清理临时源文件
        try:
            source_file.unlink(missing_ok=True)
        except Exception:
            pass

    return None


def _get_wechat_window_id() -> Optional[int]:
    """
    获取 WeChat 窗口 ID。
    先尝试从缓存文件读取（5 分钟内有效），再走 Python CoreGraphics，最后回退 gcc 编译路径。
    """
    cache_file = WECHAT_WINDOW_ID_CACHE

    # 检查缓存是否新鲜（5 分钟内）
    if cache_file.exists():
        try:
            content = cache_file.read_text(encoding="utf-8").strip()
            cached_time, cached_id = content.rsplit(",", 1)
            cached_dt = datetime.fromisoformat(cached_time)
            if (datetime.now() - cached_dt).total_seconds() < 300:
                logger.debug("使用缓存的 WeChat WindowID: %s", cached_id)
                return int(cached_id)
        except Exception:
            pass

    window_id = _get_wechat_window_id_via_python()
    if window_id is None:
        window_id = _get_wechat_window_id_via_compiled_tool()

    if window_id is not None:
        try:
            cache_file.write_text(
                f"{datetime.now().isoformat()},{window_id}", encoding="utf-8"
            )
        except Exception as e:
            logger.debug("写入 WindowID 缓存失败: %s", e)
        logger.info("动态获取 WeChat WindowID: %d", window_id)
        return window_id

    return None



_GET_WECHAT_WINDOW_C_SOURCE = r"""
#include <CoreGraphics/CoreGraphics.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
int main() {
    CFArrayRef wl = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly, kCGNullWindowID);
    CFIndex c = CFArrayGetCount(wl);
    for (CFIndex i = 0; i < c; i++) {
        CFDictionaryRef w = CFArrayGetValueAtIndex(wl, i);
        CFStringRef o = CFDictionaryGetValue(w, kCGWindowOwnerName);
        char buf[256];
        if (!o || !CFStringGetCString(o, buf, sizeof(buf), kCFStringEncodingUTF8))
            continue;
        if (strcmp(buf, "微信") == 0) {
            CFNumberRef wid = CFDictionaryGetValue(w, kCGWindowNumber);
            int id; CFNumberGetValue(wid, kCFNumberIntType, &id);
            printf("%d\n", id);
            CFRelease(wl);
            return 0;
        }
    }
    CFRelease(wl);
    return 1;
}
"""


def _is_emoji_by_filename(filepath: str) -> bool:
    """根据文件名判断是否为表情包。"""
    low = filepath.lower()
    return any(kw in low for kw in EMOJI_KEYWORDS)


def _get_image_dimensions(filepath: str) -> Tuple[Optional[int], Optional[int]]:
    """
    通过 sips 获取图片宽高。
    返回 (width, height)，失败返回 (None, None)。
    """
    try:
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", filepath],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None, None
        w_match = re.search(r"pixelWidth:\s*(\d+)", result.stdout)
        h_match = re.search(r"pixelHeight:\s*(\d+)", result.stdout)
        if w_match and h_match:
            return int(w_match.group(1)), int(h_match.group(1))
    except Exception as e:
        logger.debug("获取图片尺寸失败 %s: %s", filepath, e)
    return None, None


def _is_emoji_by_size(filepath: str) -> bool:
    """通过尺寸判断是否为表情包（宽或高 < EMOJI_SIZE_THRESHOLD）。"""
    w, h = _get_image_dimensions(filepath)
    if w is None or h is None:
        return False
    return w < EMOJI_SIZE_THRESHOLD or h < EMOJI_SIZE_THRESHOLD


def _capture_wechat_window(window_id: int, output_path: Path) -> bool:
    """
    使用 screencapture 截取指定窗口。
    返回 True 成功，False 失败。
    """
    try:
        result = subprocess.run(
            ["screencapture", "-l", str(window_id), str(output_path)],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0 and output_path.exists()
    except Exception as e:
        logger.warning("screencapture 失败: %s", e)
        return False


def _image_to_base64(image_path: Path) -> Optional[str]:
    """将图片转为 Base64 字符串（用于视觉模型 API）。"""
    try:
        return base64.b64encode(image_path.read_bytes()).decode("utf-8")
    except Exception as e:
        logger.warning("图片 Base64 编码失败: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 图片批次
# ─────────────────────────────────────────────────────────────────────────────

class ImageBatch:
    """
    一批连续发送的图片（时间间隔 < 30s，中间无有效文本）。
    """

    def __init__(self, first_msg: Dict):
        self.messages: List[Dict] = [first_msg]   # 原始图片消息列表
        self.sender = first_msg.get("sender", "未知")
        self.first_time = first_msg.get("timestamp")
        self.last_time = first_msg.get("timestamp")
        self.context_before: List[Dict] = []       # 批次前上下文
        self.context_after: List[Dict] = []        # 批次后上下文
        self.should_analyze: bool = True           # 经 AI 判断是否需要分析
        self.skip_reason: Optional[str] = None     # 跳过原因

    def add(self, msg: Dict):
        self.messages.append(msg)
        self.last_time = msg.get("timestamp")

    @property
    def is_single(self) -> bool:
        return len(self.messages) == 1

    @property
    def batch_size(self) -> int:
        return len(self.messages)


# ─────────────────────────────────────────────────────────────────────────────
# 视觉模型客户端（支持 OpenAI 兼容接口 / Claude 等）
# ─────────────────────────────────────────────────────────────────────────────

class VisionClient:
    """
    视觉模型客户端，支持 OpenAI 兼容格式。
    配置优先级：VISION_API_KEY > AI_PROVIDER 的 API_KEY
    """

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url or "https://api.openai.com/v1"
        self._last_tokens: int = 0

    def analyze(self, image_b64: str, prompt: str) -> str:
        """
        发送图片给视觉模型，返回文本描述。
        使用 OpenAI vision-compatible API 格式。
        """
        import requests

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 800,
            "temperature": 0.3,
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            result = resp.json()
            content = result["choices"][0]["message"]["content"]
            # 提取 token 用量
            usage = result.get("usage", {})
            self._last_tokens = usage.get("total_tokens", 0) or 0
            logger.debug("视觉模型返回: %s (tokens: %d)", content[:100], self._last_tokens)
            return content
        except Exception as e:
            logger.error("视觉模型调用失败: %s", e)
            raise

    @property
    def last_tokens(self) -> int:
        """本次 analyze 调用的 token 用量。"""
        return self._last_tokens


def _create_vision_client() -> VisionClient:
    """从环境变量创建视觉模型客户端。"""
    # 优先使用独立的 VISION 配置
    api_key = os.environ.get("VISION_API_KEY")
    model = os.environ.get("VISION_MODEL", "claude-sonnet-4-6")
    base_url = os.environ.get("VISION_BASE_URL")

    if not api_key:
        # 降级到主 AI_PROVIDER（需要有视觉能力）
        provider = os.environ.get("AI_PROVIDER", "").lower()
        key_map = {
            "minimax": ("MINIMAX_API_KEY", "https://api.minimax.chat/v1"),
            "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
            "siliconflow": ("SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1"),
            "zhipu": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4"),
            "qwen": ("QWEN_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        }
        if provider in key_map:
            env_key, default_url = key_map[provider]
            api_key = os.environ.get(env_key)
            base_url = base_url or default_url
        else:
            raise EnvironmentError(
                "未配置 VISION_API_KEY，且 AI_PROVIDER 不支持视觉，请设置 VISION_API_KEY"
            )

    return VisionClient(api_key=api_key, model=model, base_url=base_url)


# ─────────────────────────────────────────────────────────────────────────────
# AI 判断是否需要分析
# ─────────────────────────────────────────────────────────────────────────────

BATCH_SEMANTIC_TRIGGER_PROMPT = (
    "判断以下每个聊天上下文片段中，用户是否因缺少视觉信息而无法理解对话内容。"
    "对每个片段仅回答 YES（需要图片）或 NO（无需图片），用逗号分隔。"
    "例如：YES,NO,YES\n\n"
)


def _batch_ask_semantic_trigger(
    context_texts: List[str], ai_client
) -> List[bool]:
    """
    将所有批次的上下文一次性发送给文本 AI，批量判断是否需要分析图片。
    返回与 context_texts 等长的 bool 列表，True=需要分析，False=跳过。
    """
    if not context_texts:
        return []

    combined = BATCH_SEMANTIC_TRIGGER_PROMPT
    for i, text in enumerate(context_texts):
        combined += f"--- 片段 {i + 1} ---\n{text}\n\n"

    try:
        response = ai_client.summarize(combined, "批量上下文判断")
        answer = response.strip().upper()
        logger.info("批量语义触发判断结果: %s", answer)

        # 解析逗号分隔的 YES/NO 列表
        parts = [p.strip() for p in answer.split(",")]
        results = [p == "YES" for p in parts]

        # 长度不匹配时，补充为 True（保守处理：执行分析）
        while len(results) < len(context_texts):
            results.append(True)
        return results[:len(context_texts)]

    except Exception as e:
        logger.warning("批量语义触发判断失败，默认全部分析: %s", e)
        return [True] * len(context_texts)


# ─────────────────────────────────────────────────────────────────────────────
# 核心分析器
# ─────────────────────────────────────────────────────────────────────────────

class ImageAnalyzer:
    """
    图片分析器主类。
    流程：获取图片消息 → 过滤表情包 → 合并连续批次 →
          语义触发判断 → 截图 → AI 视觉分析 → 输出文本
    """

    def __init__(
        self,
        group_name: str,
        days: int = 1,
        limit: int = 200,
        token_tracker=None,
    ):
        self.group_name = group_name
        self.days = days
        self.limit = limit
        self.token_tracker = token_tracker
        self.total_image_tokens: int = 0
        self.wechat_window_id: Optional[int] = None
        self._vision_client: Optional[VisionClient] = None
        self._results_lock = threading.Lock()
        self.analysis_results: List[str] = []    # 分析结果文本列表

    @property
    def vision_client(self) -> VisionClient:
        if self._vision_client is None:
            self._vision_client = _create_vision_client()
        return self._vision_client

    def _fetch_image_messages(self) -> List[Dict]:
        """通过 wechat-cli 获取图片类型消息（带媒体路径）。"""
        start_date = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")
        cmd = [
            "wechat-cli", "history", self.group_name,
            "--start-time", start_date,
            "--limit", str(self.limit),
            "--format", "json",
            "--type", "image",
            "--media",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=30
            )
            raw = result.stdout.strip()
            data = json.loads(raw) if raw else {}
            messages = data.get("messages", [])
            logger.info("获取图片消息 %d 条", len(messages))
            return messages
        except subprocess.CalledProcessError as e:
            logger.error("wechat-cli history --type image 失败: %s\n%s", e, e.stderr)
            return []
        except json.JSONDecodeError as e:
            logger.error("解析图片消息 JSON 失败: %s", e)
            return []

    def _parse_image_message(self, raw_msg: Dict) -> Optional[Dict]:
        """
        解析图片消息记录，返回标准格式 dict 或 None。
        支持多种 raw 格式：字符串行 或 结构化 dict。
        """
        # 处理字符串格式（如 '[2026-04-12 22:00] 发送者: [图片]'）
        if isinstance(raw_msg, str):
            pattern = r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.+?): \[图片\](.*)$"
            match = re.match(pattern, raw_msg.strip())
            if not match:
                return None
            ts_str, sender, extra = match.group(1), match.group(2), match.group(3).strip()
            # 从 extra 中尝试提取文件路径（若有）
            path_match = re.search(r"(/[\w/.-]+\.(?:jpg|jpeg|png|gif|webp))", extra)
            filepath = path_match.group(1) if path_match else ""
            msg_id = ""
        else:
            # 结构化 dict（wechat-cli --media 时可能包含 _mediaPath 等字段）
            ts_str = raw_msg.get("timestamp_str") or raw_msg.get("time") or ""
            sender = raw_msg.get("sender", "未知")
            content = raw_msg.get("content", "")
            # 尝试从 content 或 _mediaPath 中取文件路径
            filepath = raw_msg.get("_mediaPath") or raw_msg.get("mediaPath") or ""
            if not filepath:
                path_m = re.search(
                    r"(/[\w/.-]+\.(?:jpg|jpeg|png|gif|webp))", str(content)
                )
                filepath = path_m.group(1) if path_m else ""
            msg_id = raw_msg.get("msg_id") or raw_msg.get("id") or ""

        if not ts_str:
            return None
        try:
            timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                timestamp = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                logger.warning("无法解析图片消息时间戳: %s", ts_str)
                return None

        return {
            "sender": sender.strip(),
            "content": "[图片]",
            "timestamp": timestamp,
            "msg_id": msg_id,
            "filepath": filepath,
            "raw": raw_msg,
        }

    def _filter_emoji(self, msgs: List[Dict]) -> List[Dict]:
        """过滤表情包：尺寸过小 或 文件名含关键词。"""
        filtered = []
        for msg in msgs:
            fp = msg.get("filepath", "")
            reasons = []
            if _is_emoji_by_filename(fp):
                reasons.append("文件名含表情关键词")
            elif _is_emoji_by_size(fp):
                reasons.append(f"尺寸 < {EMOJI_SIZE_THRESHOLD}px")
            if reasons:
                logger.info(
                    "跳过表情包 [%s] %s: %s",
                    msg.get("sender"), fp or "(无路径)", ", ".join(reasons)
                )
                continue
            filtered.append(msg)
        logger.info("表情包过滤完成: %d/%d", len(filtered), len(msgs))
        return filtered

    def _merge_consecutive_batches(
        self, msgs: List[Dict], all_text_messages: List[Dict]
    ) -> List[ImageBatch]:
        """
        将连续图片消息合并为批次。
        判断规则：间隔 < 30s，且中间无有效文本。
        同时附加上下文（每批前后各 CONTEXT_MESSAGE_COUNT 条，限 5 分钟内）。
        """
        if not msgs:
            return []

        text_messages = sorted(all_text_messages, key=lambda m: m["timestamp"])
        text_timestamps = [m["timestamp"] for m in text_messages]

        batches: List[ImageBatch] = []
        current_batch: Optional[ImageBatch] = None

        for msg in msgs:
            if current_batch is None:
                current_batch = ImageBatch(msg)
            else:
                gap = (msg["timestamp"] - current_batch.last_time).total_seconds()
                left = bisect_right(text_timestamps, current_batch.last_time)
                right = bisect_left(text_timestamps, msg["timestamp"])
                has_text_between = left < right
                if gap < CONSECUTIVE_GAP_SECONDS and not has_text_between:
                    current_batch.add(msg)
                else:
                    batches.append(current_batch)
                    current_batch = ImageBatch(msg)

        if current_batch is not None:
            batches.append(current_batch)

        # 附加上下文
        for batch in batches:
            batch.context_before = self._get_context_messages(
                batch.first_time, text_messages, direction="before"
            )
            batch.context_after = self._get_context_messages(
                batch.last_time, text_messages, direction="after"
            )

        logger.info(
            "图片批次合并完成: %d 条图片 → %d 个批次",
            len(msgs), len(batches),
        )
        return batches

    def _get_context_messages(
        self,
        anchor_time: datetime,
        all_messages: List[Dict],
        direction: str,
    ) -> List[Dict]:
        """
        获取锚点时间之前/之后的上下文消息。
        限制：最多 CONTEXT_MESSAGE_COUNT 条，且在 CONTEXT_TIME_WINDOW_MINUTES 内。
        """
        time_window = timedelta(minutes=CONTEXT_TIME_WINDOW_MINUTES)
        start_time = anchor_time - time_window
        end_time = anchor_time + time_window

        timestamps = [m["timestamp"] for m in all_messages]
        start_idx = bisect_left(timestamps, start_time)
        end_idx = bisect_right(timestamps, end_time)
        candidates = all_messages[start_idx:end_idx]

        if direction == "before":
            result = [m for m in candidates if m["timestamp"] < anchor_time]
            return result[-CONTEXT_MESSAGE_COUNT:]

        result = [m for m in candidates if m["timestamp"] > anchor_time]
        return result[:CONTEXT_MESSAGE_COUNT]

    def _build_context_text(self, batch: ImageBatch) -> str:
        """将批次的上下文消息构建为纯文本。"""
        lines = []
        for ctx in batch.context_before:
            ts = ctx["timestamp"].strftime("%H:%M")
            lines.append(f"[{ts}] {ctx.get('sender', '?')}: {ctx.get('content', '')}")
        lines.append("--- 图片批次 ---")
        for ctx in batch.context_after:
            ts = ctx["timestamp"].strftime("%H:%M")
            lines.append(f"[{ts}] {ctx.get('sender', '?')}: {ctx.get('content', '')}")
        return "\n".join(lines)

    def _semantic_filter(self, batches: List[ImageBatch], ai_client) -> List[ImageBatch]:
        """
        批量语义触发判断：一次 AI 调用判断所有批次。
        返回所有批次，通过 should_analyze 属性标记是否需要分析。
        跳过的批次设置 should_analyze=False 和 skip_reason。
        """
        analyze_count = 0
        skip_count = 0

        # 分离有/无上下文的批次
        need_judge_indices = []
        context_texts = []
        for i, batch in enumerate(batches):
            context_text = self._build_context_text(batch)
            if not context_text.strip():
                logger.info("批次（%s）无上下文，跳过语义判断，直接分析",
                            batch.first_time.strftime("%H:%M"))
                analyze_count += 1
                continue
            need_judge_indices.append(i)
            context_texts.append(context_text)

        # 批量判断
        if context_texts:
            judge_results = _batch_ask_semantic_trigger(context_texts, ai_client)
            for idx, should in zip(need_judge_indices, judge_results):
                batches[idx].should_analyze = should
                if not should:
                    batches[idx].skip_reason = "语义判断：上下文无需图片即可理解"
                    skip_count += 1
                    logger.info(
                        "批次（%s）跳过: %s",
                        batches[idx].first_time.strftime("%H:%M"),
                        batches[idx].skip_reason,
                    )
                else:
                    analyze_count += 1

        logger.info(
            "语义过滤完成: %d 批次需要分析, %d 批次跳过 (共 %d)",
            analyze_count, skip_count, len(batches),
        )
        return batches

    def _capture_batch_images(
        self, batch: ImageBatch
    ) -> List[Tuple[ImageBatch, Dict, str, int, Optional[str]]]:
        """
        串行截取批次中所有图片的预览并转为 Base64。
        返回 [(batch, msg, msg_id, index, b64), ...]，截图失败的 b64 为 None。
        此方法操作 WeChat 窗口，不可并发调用。
        """
        ANALYSIS_TMP_DIR.mkdir(exist_ok=True)
        captured = []

        if self.wechat_window_id is None:
            self.wechat_window_id = _get_wechat_window_id()
            if self.wechat_window_id is None:
                logger.error("无法获取 WeChat WindowID，跳过批次 %s", batch.first_time)
                return []

        for i, msg in enumerate(batch.messages):
            msg_id = msg.get("msg_id") or f"batch_{batch.batch_size}_idx_{i}"
            b64 = self._capture_image(msg, msg_id)
            captured.append((batch, msg, msg_id, i, b64))

        return captured

    def _capture_image(self, msg: Dict, msg_id: str) -> Optional[str]:
        """
        截取单张图片预览并转为 Base64。
        返回 Base64 字符串，失败返回 None。
        此方法操作 WeChat 窗口，不可并发调用。
        """
        preview_path = ANALYSIS_TMP_DIR / f"preview_{msg_id}.png"

        # 调用 wechat-cli preview 打开预览窗口（如支持）
        try:
            subprocess.run(
                ["wechat-cli", "preview", "--msg-id", str(msg_id)],
                capture_output=True,
                timeout=10,
            )
            time.sleep(0.8)   # 等待预览窗口打开
        except Exception as e:
            logger.debug("wechat-cli preview 不可用: %s", e)

        # 截图
        ok = _capture_wechat_window(self.wechat_window_id, preview_path)
        if not ok or not preview_path.exists():
            logger.warning("截图失败，跳过 msg_id=%s", msg_id)
            return None

        # 转 Base64
        b64 = _image_to_base64(preview_path)

        # 清理临时截图
        try:
            preview_path.unlink(missing_ok=True)
        except Exception:
            pass

        return b64

    def _analyze_single_image(
        self, msg: Dict, msg_id: str, index: int, batch: ImageBatch,
        b64: Optional[str] = None,
    ) -> Optional[str]:
        """
        分析单张图片，失败时独立重试，不影响批次中其他图片。
        若提供 b64 参数则跳过截图步骤（用于并发场景：截图串行，分析并发）。
        """
        if b64 is None:
            return None  # 外部负责截图，这里只做视觉分析

        # 调用视觉模型
        prompt = (
            "请仔细描述这张图片中的所有文字内容，以及图片的主题和关键信息。"
            "文字请完整转录，信息请准确概括。"
        )

        for attempt in range(MAX_RETRIES + 1):
            try:
                analysis = self.vision_client.analyze(b64, prompt)
                ts_str = msg["timestamp"].strftime("%Y-%m-%d %H:%M")
                sender = msg.get("sender", "未知")

                if batch.is_single:
                    return (
                        f"[图片分析] {sender} 在 {ts_str} 发送了一张图片：{analysis}"
                    )
                else:
                    return (
                        f"[图片分析] {sender} 在 {ts_str} "
                        f"发送了第 {index + 1}/{batch.batch_size} 张图片：{analysis}"
                    )

            except Exception as e:
                logger.warning("图片 %s 视觉分析失败 (尝试 %d/%d): %s",
                               msg_id, attempt + 1, MAX_RETRIES + 1, e)
                if attempt < MAX_RETRIES:
                    self_heal_image(msg_id, e)
                else:
                    logger.error("图片 %s 分析重试次数耗尽，跳过", msg_id)

        return None

    def analyze(self, all_text_messages: List[Dict], ai_client) -> str:
        """
        入口方法：执行完整图片分析流程。
        all_text_messages: 用于上下文的全部文本消息（已预处理）
        ai_client: 文本 AI 客户端（用于语义判断）
        返回拼接后的分析结果文本（可追加到摘要末尾）。
        """
        logger.info("===== 图片分析开始 =====")

        raw_msgs = self._fetch_image_messages()
        if not raw_msgs:
            logger.info("无图片消息，图片分析跳过")
            return ""

        parsed = [p for p in (self._parse_image_message(m) for m in raw_msgs) if p]
        if not parsed:
            logger.warning("无法解析任何图片消息格式")
            return ""

        filtered = self._filter_emoji(parsed)
        if not filtered:
            logger.info("过滤后无有效图片")
            return ""

        batches = self._merge_consecutive_batches(filtered, all_text_messages)
        if not batches:
            return ""

        # 语义过滤（使用文本 AI）
        all_batches = self._semantic_filter(batches, ai_client)

        # 截图 + 视觉分析（仅分析 should_analyze=True 的批次）
        batches_to_analyze = [b for b in all_batches if b.should_analyze]
        if not batches_to_analyze:
            logger.info("===== 图片分析完成: 0 条结果 =====")
            return ""

        # 串行截图（操作 WeChat 窗口，不可并发）
        all_captured = []
        for batch in batches_to_analyze:
            captured = self._capture_batch_images(batch)
            all_captured.extend(captured)

        if not all_captured:
            logger.info("===== 图片分析完成: 无可分析的截图 =====")
            return ""

        # 并发视觉分析（网络 I/O，可并发）
        worker_count = min(MAX_CONCURRENT_BATCHES, len(all_captured))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            # 为每个批次构建结果
            batch_results = {}

            future_to_data = {}
            for batch, msg, msg_id, index, b64 in all_captured:
                if b64 is None:
                    continue
                future = executor.submit(
                    self._analyze_single_image,
                    msg, msg_id, index, batch, b64
                )
                future_to_data[future] = batch

            for future in as_completed(future_to_data):
                batch = future_to_data[future]
                try:
                    result_text = future.result()
                    if result_text:
                        # 累加视觉模型 token 用量
                        vision_tokens = self.vision_client.last_tokens
                        self.total_image_tokens += vision_tokens
                        if self.token_tracker:
                            self.token_tracker.add_usage(vision_tokens)
                        with self._results_lock:
                            if batch not in batch_results:
                                batch_results[batch] = []
                            batch_results[batch].append(result_text)
                except Exception as e:
                    logger.error("批次（%s）视觉分析异常: %s",
                                 batch.first_time.strftime("%H:%M"), e)

            # 组合每个批次的结果
            for batch in batches_to_analyze:
                if batch in batch_results and batch_results[batch]:
                    if batch.is_single:
                        self.analysis_results.append(batch_results[batch][0])
                    else:
                        header = f"【图片批次】（共 {batch.batch_size} 张，{batch.sender} 于 {batch.first_time.strftime('%H:%M')} 发送）"
                        body = "\n".join(batch_results[batch])
                        self.analysis_results.append(f"{header}\n{body}")

        final_text = "\n\n".join(self.analysis_results)
        logger.info(
            "===== 图片分析完成: %d 条结果 =====", len(self.analysis_results)
        )
        return final_text


# ─────────────────────────────────────────────────────────────────────────────
# 自愈机制
# ─────────────────────────────────────────────────────────────────────────────

def self_heal_image(msg_context: str, error: Exception) -> bool:
    """
    图片分析出错时，尝试调用 free-code 分析日志并给出修复建议。
    """
    try:
        result = subprocess.run(
            ["free-code", "--analyze-log", str(BASE_DIR / "logs")],
            input=f"图片分析失败: {error}\n上下文: {msg_context}",
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info("图片分析自愈建议:\n%s", result.stdout)
            return True
    except FileNotFoundError:
        logger.debug("free-code 不可用，跳过图片自愈")
    except Exception as e:
        logger.warning("图片自愈失败: %s", e)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 日志配置（模块级）
# ─────────────────────────────────────────────────────────────────────────────

def setup_module_logging() -> logging.Logger:
    """为图片分析模块配置独立日志（沿用 main.py 的切分规则）。"""
    return configure_logger(
        log_dir=LOG_DIR,
        main_log_filename="image_analyzer.log",
        error_log_filename="image_error.log",
        backup_count=14,
        logger_name="image_analyzer",
    )


# 模块加载时初始化日志（幂等：已有 handler 则跳过）
if not logger.handlers:
    setup_module_logging()
