#!/usr/bin/env python3
"""
微信群聊日报导出脚本
功能：按时间段导出微信群消息 → AI 摘要生成 → Server酱³ 推送
"""

import os
import sys
import json
import logging
import subprocess
import re
import argparse
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from typing import List, Dict, Optional, Tuple
from logging_utils import configure_logger
from preprocessor import preprocess, remove_system

# ── 路径常量 ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
REPORT_DIR = BASE_DIR
ERROR_LOG_DIR = BASE_DIR / "logs"

# ── 日志配置 ─────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    return configure_logger(
        log_dir=ERROR_LOG_DIR,
        main_log_filename=None,
        error_log_filename="error.log",
        backup_count=30,
    )


logger = setup_logging()


# 预处理函数（filter_invalid / merge_consecutive / remove_system）
# 已迁移至 preprocessor.py，由上方 import 直接复用


# ── 参数解析与交互式输入 ───────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="微信群聊日报导出脚本")
    parser.add_argument("--start", type=str, help="开始时间，格式: YYYY-MM-DD HH:MM")
    parser.add_argument("--end", type=str, help="结束时间，格式: YYYY-MM-DD HH:MM")
    return parser.parse_args()


def get_time_range(args, group_keywords: Optional[List[str]] = None) -> Tuple[str, str]:
    """获取时间段，支持命令行参数或交互式输入。"""
    # 如果没有命令行参数，先显示可用时间范围
    if not args.start and not args.end and group_keywords:
        display_available_time_ranges(group_keywords)

    datetime_pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
    datetime_format = "%Y-%m-%d %H:%M"

    def validate_and_return(value: Optional[str], name: str) -> str:
        if value and re.match(datetime_pattern, value):
            return value
        while True:
            user_input = input(f"请输入{name}（格式: YYYY-MM-DD HH:MM）: ").strip()
            if re.match(datetime_pattern, user_input):
                return user_input
            print(f"格式不正确，请重新输入（例: 2026-04-13 09:00）")

    start_time = validate_and_return(args.start, "开始时间")
    end_time = validate_and_return(args.end, "结束时间")

    # 验证时间范围
    try:
        start_dt = datetime.strptime(start_time, datetime_format)
        end_dt = datetime.strptime(end_time, datetime_format)
        if start_dt > end_dt:
            raise ValueError
    except ValueError:
        print("错误：开始时间不能晚于结束时间，请重新输入")
        return get_time_range(args, group_keywords)

    return start_time, end_time


# ── 数据获取 ─────────────────────────────────────────────────────────────────
def get_sessions() -> List[Dict]:
    """获取最近会话列表。"""
    cmd = ["wechat-cli", "sessions", "--limit", "100"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        )
        raw = result.stdout.strip()
        # 尝试 JSON 解析
        try:
            return json.loads(raw) if raw else []
        except json.JSONDecodeError:
            # 解析纯文本列表
            sessions = []
            for line in raw.splitlines():
                line = line.strip()
                if line and not line.startswith("["):
                    sessions.append({"chat": line})
            return sessions
    except subprocess.CalledProcessError as e:
        logger.error("wechat-cli sessions 命令失败: %s", e)
        return []


def fuzzy_match_group(keyword: str, sessions: List[Dict]) -> Optional[str]:
    """模糊匹配群名。"""
    keyword_lower = keyword.lower()
    candidates = []
    for session in sessions:
        chat = session.get("chat", "")
        if keyword_lower in chat.lower():
            candidates.append(chat)
    return candidates[0] if candidates else None


def get_group_time_range(group_name: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    获取指定群的聊天记录时间范围。
    返回: (最早消息时间, 最新消息时间)
    """
    try:
        # 获取最新消息（limit=1, offset=0）
        result = subprocess.run(
            ["wechat-cli", "history", group_name, "--limit", "1", "--format", "json"],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout.strip())
        messages = data.get("messages", [])
        latest_time = None
        if messages:
            parsed = parse_message_line(messages[0])
            if parsed:
                latest_time = parsed.get("timestamp")

        # 获取最早消息（尝试较大 offset，若失败则尝试旧时间范围）
        earliest_time = None
        for offset in [5000, 10000, 20000]:
            result = subprocess.run(
                ["wechat-cli", "history", group_name, "--limit", "1", "--offset", str(offset), "--format", "json"],
                capture_output=True, text=True, timeout=30
            )
            data = json.loads(result.stdout.strip())
            messages = data.get("messages", [])
            if messages:
                parsed = parse_message_line(messages[0])
                if parsed:
                    earliest_time = parsed.get("timestamp")
                    break

        # 如果 offset 方式失败，尝试从当前年份 01-01 开始查找
        if earliest_time is None:
            year = datetime.now().year
            result = subprocess.run(
                ["wechat-cli", "history", group_name,
                 "--start-time", f"{year}-01-01 00:00",
                 "--end-time", f"{year}-01-02 00:00",
                 "--limit", "1", "--format", "json"],
                capture_output=True, text=True, timeout=30
            )
            try:
                data = json.loads(result.stdout.strip())
                messages = data.get("messages", [])
                if messages:
                    parsed = parse_message_line(messages[0])
                    if parsed:
                        earliest_time = parsed.get("timestamp")
            except (json.JSONDecodeError, subprocess.CalledProcessError):
                pass

        return earliest_time, latest_time

    except Exception as e:
        logger.warning(f"获取群 {group_name} 时间范围失败: {e}")
        return None, None


def display_available_time_ranges(group_keywords: List[str]) -> None:
    """显示已配置群聊的可用时间范围。"""
    print("\n" + "=" * 60)
    print("正在查询已配置群聊的聊天记录时间范围...")
    print("=" * 60)

    sessions = get_sessions()
    if not sessions:
        print("无法获取会话列表")
        return

    matched_any = False
    for keyword in group_keywords:
        matched = fuzzy_match_group(keyword, sessions)
        if matched:
            matched_any = True
            earliest, latest = get_group_time_range(matched)
            print(f"\n📌 群名: {matched}")
            if earliest:
                print(f"   最早消息: {earliest.strftime('%Y-%m-%d %H:%M')}")
            else:
                print(f"   最早消息: 未知")
            if latest:
                print(f"   最新消息: {latest.strftime('%Y-%m-%d %H:%M')}")
            else:
                print(f"   最新消息: 未知")

    if not matched_any:
        print("\n未找到任何匹配的群聊")

    print("\n" + "=" * 60)
    print("请根据以上时间范围输入要导出的时间段")
    print("=" * 60 + "\n")


def get_group_messages(group_name: str, start_time: str, end_time: str) -> Tuple[List[Dict], str, bool]:
    """
    获取指定群的消息。
    返回: (消息列表, 错误信息, 是否可能被 limit 截断)
    """
    limit = 1000
    cmd = [
        "wechat-cli", "history", group_name,
        "--limit", str(limit),
        "--start-time", start_time,
        "--end-time", end_time,
        "--format", "json",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=60
        )
        raw = result.stdout.strip()

        # 尝试 JSON 解析
        try:
            data = json.loads(raw)
            messages = data.get("messages", []) if isinstance(data, dict) else data
            parsed = []
            for line in messages:
                item = parse_message_line(line)
                if item:
                    parsed.append(item)
            truncated = len(messages) >= limit
            return parsed, "", truncated
        except json.JSONDecodeError:
            # 降级到纯文本解析
            parsed = parse_text_messages(raw)
            truncated = len(parsed) >= limit
            return parsed, "", truncated
    except subprocess.CalledProcessError as e:
        err = f"命令执行失败: {e}"
        return [], err, False
    except Exception as e:
        err = f"获取消息异常: {e}"
        return [], err, False


def parse_message_line(line: str) -> Optional[Dict]:
    """解析 JSON 格式消息行。"""
    # 如果是 JSON 对象字符串
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            # 尝试多种可能的时间字段
            ts_str = obj.get("time") or obj.get("timestamp") or obj.get("date")
            sender = obj.get("sender") or obj.get("nickname") or obj.get("name", "")
            content = obj.get("content") or obj.get("msg") or obj.get("text", "")

            if not ts_str or not content:
                return None

            # 解析时间
            ts = None
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                try:
                    ts = datetime.strptime(str(ts_str)[:19], fmt)
                    break
                except ValueError:
                    continue

            if ts is None:
                logger.warning(f"无法解析时间 '{ts_str}'，使用当前时间")
                ts = datetime.now()

            return {
                "sender": str(sender).strip(),
                "content": str(content).strip(),
                "timestamp": ts,
                "raw": line,
            }
        except Exception:
            return None

    # 文本格式: [2026-04-13 10:15] 发送者: 内容
    pattern = r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.+?): (.+)$"
    match = re.match(pattern, line.strip(), re.DOTALL)
    if match:
        ts_str, sender, content = match.group(1), match.group(2), match.group(3)
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except ValueError:
            ts = datetime.now()
        return {
            "sender": sender.strip(),
            "content": content.strip(),
            "timestamp": ts,
            "raw": line,
        }
    return None


def parse_text_messages(raw: str) -> List[Dict]:
    """解析纯文本格式消息。"""
    messages = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        item = parse_message_line(line)
        if item:
            messages.append(item)
    return messages


# ── AI 摘要生成 ──────────────────────────────────────────────────────────────
def parse_ai_response(result: dict, provider: str) -> Tuple[str, int]:
    """
    解析不同 AI 提供商的响应格式。

    Args:
        result: API 返回的 JSON 响应
        provider: 提供商名称

    Returns:
        (content, total_tokens) 内容文本和总 token 数
    """
    provider = provider.lower()

    # OpenAI 兼容格式 (DeepSeek, SiliconFlow, Qwen 等)
    if provider in ("deepseek", "siliconflow", "qwen", "minimax"):
        message = result.get("choices", [{}])[0].get("message", {})
        content = message.get("content", "")
        # DeepSeek Reasoner: 实际回答在 content，推理过程在 reasoning_content
        # 若 content 为空则回退到 reasoning_content
        if not content and message.get("reasoning_content"):
            content = message["reasoning_content"]
        total_tokens = result.get("usage", {}).get("total_tokens", 0)
        return content, total_tokens

    # 智谱 AI (GLM) 格式
    elif provider == "zhipu":
        # 智谱 API 响应格式: {"data": {"choices": [{"message": {"content": "..."}}]}}
        data = result.get("data", {})
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        total_tokens = data.get("usage", {}).get("total_tokens", 0)
        return content, total_tokens

    else:
        # 默认尝试 OpenAI 格式
        message = result.get("choices", [{}])[0].get("message", {})
        content = message.get("content", "")
        if not content and message.get("reasoning_content"):
            content = message["reasoning_content"]
        total_tokens = result.get("usage", {}).get("total_tokens", 0)
        return content, total_tokens


def create_ai_client(provider: str, api_key: str, model: str):
    """创建 AI 客户端。"""
    from ai_client import create_ai_client as _create

    class WrappedClient:
        def __init__(self, client, provider: str):
            self._client = client
            self._provider = provider.lower()
            self._token_count = 0

        def summarize_json(self, messages_text: str, date_range: str) -> dict:
            prompt = construct_summary_prompt(messages_text, date_range)
            headers = {
                "Authorization": f"Bearer {self._client.api_key}",
                "Content-Type": "application/json"
            }
            data = {
                "model": self._client.model,
                "messages": [
                    {"role": "system", "content": "你是一个微信群聊摘要助手。请严格按照指定的JSON格式输出摘要。"},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 1500,
                "temperature": 0.3
            }

            try:
                resp = requests.post(self._client._api_url, headers=headers, json=data, timeout=120)
                resp.raise_for_status()
                result = resp.json()
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"AI API 请求失败: {e}") from e

            content, total_tokens = parse_ai_response(result, self._provider)
            if total_tokens:
                self._token_count += total_tokens
            else:
                self._token_count += len(messages_text) // 2 + 200

            # 提取 JSON
            return extract_json(content)

        @property
        def estimated_tokens(self) -> int:
            return self._token_count

    client = _create(provider, api_key=api_key, model=model)
    return WrappedClient(client, provider)


def construct_summary_prompt(messages_text: str, date_range: str) -> str:
    return f"""请分析以下微信群聊消息，生成结构化摘要。

日期范围: {date_range}

消息内容:
{messages_text}

请严格按以下JSON格式输出（必须是一个有效的JSON对象，不要包含任何其他文字）：
{{
  "core_topic": "今日讨论核心主题",
  "key_decisions": ["决定1", "决定2"],
  "action_items": [{{"task": "待办事项", "assignee": "负责人或null", "deadline": "截止时间或null"}}],
  "member_activity": [{{"name": "成员昵称", "messages": 发言条数}}],
  "brief_summary": "一段话概括（200字内）"
}}"""


def extract_json(content: str) -> dict:
    """从 AI 输出中提取 JSON。"""
    # 尝试直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 包裹的内容
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试找到第一个 { 到最后一个 }
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(content[start:end+1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从AI输出中提取JSON: {content[:200]}")


def split_large_text(text: str, chunk_size: int = 4000) -> List[str]:
    """按段落边界切分大文本（fallback 到按行切分）。"""
    if len(text) <= chunk_size:
        return [text]

    # 优先按 \n\n 切分，若只有一段则按 \n 切分
    paragraphs = text.split("\n\n")
    if len(paragraphs) <= 1:
        paragraphs = text.split("\n")
        sep = "\n"
    else:
        sep = "\n\n"

    chunks = []
    current = []
    current_len = 0
    sep_len = len(sep)

    for para in paragraphs:
        para_len = len(para)
        added_len = para_len + (sep_len if current else 0)
        if current_len + added_len > chunk_size and current:
            chunks.append(sep.join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += added_len

    if current:
        chunks.append(sep.join(current))

    # 如果单个段落仍超过 chunk_size，强制按 chunk_size 截断（保留全部内容）
    final_chunks = []
    for chunk in chunks:
        while len(chunk) > chunk_size:
            # 尝试在句子/行边界截断
            truncated = chunk[:chunk_size]
            last_newline = truncated.rfind("\n")
            last_period = truncated.rfind("。")
            split_pos = max(last_newline, last_period)
            if split_pos > chunk_size * 0.5:
                final_chunks.append(chunk[:split_pos + 1])
                chunk = chunk[split_pos + 1:]
            else:
                final_chunks.append(truncated)
                chunk = chunk[chunk_size:]
        if chunk:
            final_chunks.append(chunk)

    return final_chunks


def generate_partial_summaries(client, text_chunks: List[str], date_range: str) -> List[str]:
    """生成分段摘要。"""
    partials = []
    for i, chunk in enumerate(text_chunks):
        logger.info(f"正在生成分段摘要 {i+1}/{len(text_chunks)}")
        try:
            result = client.summarize_json(chunk, date_range)
            brief = result.get("brief_summary", "")
            partials.append(f"[分段{i+1}] {brief}")
        except Exception as e:
            logger.warning(f"分段 {i+1} 摘要失败: {e}")
            partials.append(f"[分段{i+1}] （摘要生成失败）")
    return partials


def merge_partial_summaries(partials: List[str], client, date_range: str) -> dict:
    """合并分段摘要，生成最终摘要。"""
    merged_text = "\n".join(partials)
    json_template = '''{
  "core_topic": "今日讨论核心主题",
  "key_decisions": ["决定1", "决定2"],
  "action_items": [{"task": "待办事项", "assignee": "负责人或null", "deadline": "截止时间或null"}],
  "member_activity": [{"name": "成员昵称", "messages": 发言条数}],
  "brief_summary": "一段话概括（200字内）"
}'''
    prompt = f"""以下是多段消息的分段摘要：

{merged_text}

请根据以上分段摘要，生成一个完整的结构化摘要。

请严格按以下JSON格式输出：
{json_template}"""

    headers = {
        "Authorization": f"Bearer {client._client.api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": client._client.model,
        "messages": [
            {"role": "system", "content": "你是一个微信群聊摘要助手。请严格按照指定的JSON格式输出摘要。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 1500,
        "temperature": 0.3
    }

    try:
        resp = requests.post(client._client._api_url, headers=headers, json=data, timeout=120)
        resp.raise_for_status()
        result = resp.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"AI API 请求失败: {e}") from e

    content, _ = parse_ai_response(result, client._provider)
    return extract_json(content)


# ── 报告生成 ──────────────────────────────────────────────────────────────────
def build_messages_text(messages: List[Dict]) -> str:
    """将消息列表转换为纯文本。"""
    if not messages:
        return "（无有效消息）"

    lines = []
    for msg in messages:
        ts = msg["timestamp"].strftime("%H:%M")
        sender = msg.get("sender", "未知")
        content = msg.get("content", "")
        merged = msg.get("_merged_count", 1)
        suffix = f" (×{merged})" if merged > 1 else ""
        lines.append(f"[{ts}] {sender}: {content}{suffix}")
    return "\n".join(lines)


def format_action_items(action_items: List[dict]) -> str:
    """格式化待办清单。"""
    if not action_items:
        return "- （无）"
    lines = []
    for item in action_items:
        task = item.get("task", "")
        assignee = item.get("assignee") or "未指定"
        deadline = item.get("deadline") or "无"
        lines.append(f"- [{assignee}] {task}（截止: {deadline}）")
    return "\n".join(lines)


def save_markdown_report(
    group_summaries: List[dict],
    start_time: str,
    end_time: str,
    total_groups: int,
    success_groups: List[str],
    failed_groups: List[dict],
) -> str:
    """生成 Markdown 报告并保存。"""
    now = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = REPORT_DIR / f"summary_report_{now}.md"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # 生成目录
    toc_lines = ["# 微信群聊日报\n"]
    toc_lines.append(f"**生成时间**: {now}  \n")
    toc_lines.append(f"**时间段**: {start_time} ~ {end_time}  \n")
    toc_lines.append(f"**成功群数**: {len(success_groups)}/{total_groups}\n")
    toc_lines.append("\n## 目录\n")
    for i, summary in enumerate(group_summaries, 1):
        group_name = summary["group_name"]
        anchor = group_name.replace(" ", "-").replace("#", "")
        toc_lines.append(f"{i}. [{group_name}](#{anchor})")

    toc_lines.append("\n---\n")

    # 生成每个群的章节
    sections = []
    for summary in group_summaries:
        group_name = summary["group_name"]
        anchor = group_name.replace(" ", "-").replace("#", "")
        data = summary["data"]

        section = [f"## {group_name} {{#{anchor}}}\n"]
        section.append("### 元数据\n")
        section.append("| 属性 | 值 |")
        section.append("|------|-----|")
        section.append(f"| 群名 | {group_name} |")
        section.append(f"| 导出时间范围 | {start_time} ~ {end_time} |")
        section.append(f"| 消息总条数 | {summary.get('total_messages', 0)} |")
        section.append(f"| 活跃人数 | {len(data.get('member_activity', []))} |")
        section.append("\n")

        # 摘要块
        section.append("### 摘要\n")
        section.append("> **核心主题**: " + data.get("core_topic", "（无）") + "\n")
        section.append("> " + data.get("brief_summary", "（无）") + "\n")
        section.append("\n")

        # 待办清单
        section.append("### 待办清单\n")
        section.append(format_action_items(data.get("action_items", [])) + "\n")
        section.append("\n")

        # 消息总结
        section.append("### 消息总结\n")
        section.append("```\n")
        section.append(summary.get("summary_text", "（无）") + "\n")
        section.append("```\n")
        section.append("\n")

        # 原始聊天记录（折叠）
        section.append("<details>\n")
        section.append(f"<summary>原始聊天记录（共 {summary.get('total_messages', 0)} 条）</summary>\n\n")
        section.append("```\n")
        section.append(summary.get("raw_text", "（无）") + "\n")
        section.append("```\n")
        section.append("</details>\n")

        sections.append("\n".join(section))

    # 失败群组
    if failed_groups:
        fail_section = ["## 失败记录\n"]
        for fail in failed_groups:
            fail_section.append(f"- **{fail['group_name']}**: {fail['reason']}\n")
        sections.append("\n".join(fail_section))

    # 合并
    content = "\n".join(toc_lines) + "\n" + "\n".join(sections)
    report_path.write_text(content, encoding="utf-8")
    logger.info(f"报告已保存: {report_path}")

    return str(report_path)


# ── 推送通知 ──────────────────────────────────────────────────────────────────
def push_notification(
    sendkey: str,
    title: str,
    desp: str,
) -> bool:
    """通过 Server酱³ 推送通知。"""
    from pusher import ServerChanPusher
    pusher = ServerChanPusher(sendkey)
    return pusher.push(title, desp)


# ── 自愈机制 ──────────────────────────────────────────────────────────────────
def self_heal(error_log_path: str, script_filename: str, retry_count: int = 0) -> bool:
    """调用 free-code 工具进行自愈。"""
    if retry_count >= 2:
        logger.error("自愈重试次数已达上限（2次），退出")
        return False

    logger.info(f"触发自愈机制（第 {retry_count + 1} 次尝试）...")

    try:
        result = subprocess.run(
            ["free-code", "--heal", script_filename, "--error-log", error_log_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        logger.info(f"free-code 输出:\n{result.stdout}")
        if result.stderr:
            logger.warning(f"free-code 错误输出:\n{result.stderr}")
        return True
    except FileNotFoundError:
        logger.warning("free-code 工具未找到，跳过自愈")
        return False
    except subprocess.TimeoutExpired:
        logger.error("free-code 执行超时")
        return False
    except Exception as e:
        logger.error(f"free-code 执行失败: {e}")
        return False


# ── 主流程 ────────────────────────────────────────────────────────────────────
def run():
    logger.info("========== 微信群聊日报导出开始 ==========")

    # 0. 加载 .env
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
        logger.info("已加载 .env 配置文件")
    else:
        logger.error(".env 文件不存在")
        sys.exit(1)

    # 1. 解析命令行参数 & 获取配置
    args = parse_args()
    group_keywords = os.environ.get("WECHAT_GROUP_NAME", "")
    if not group_keywords:
        logger.error("WECHAT_GROUP_NAME 未配置")
        sys.exit(1)

    keywords = [k.strip() for k in group_keywords.split(",") if k.strip()]
    ai_provider = os.environ.get("AI_PROVIDER", "deepseek").lower()

    start_time, end_time = get_time_range(args, keywords)
    logger.info(f"时间段: {start_time} ~ {end_time}")

    # 获取 API Key 和 Model（适配不同 provider）
    if ai_provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v3-0324")
    elif ai_provider == "siliconflow":
        api_key = os.environ.get("SILICONFLOW_API_KEY", "")
        model = os.environ.get("SILICONFLOW_MODEL", "glm-5.1")
    elif ai_provider == "zhipu":
        api_key = os.environ.get("ZHIPU_API_KEY", "")
        model = os.environ.get("ZHIPU_MODEL", "glm-4.7")
    elif ai_provider == "qwen":
        api_key = os.environ.get("QWEN_API_KEY", "")
        model = os.environ.get("QWEN_MODEL", "qwen3.6-plus")
    elif ai_provider == "minimax":
        api_key = os.environ.get("MINIMAX_API_KEY", "")
        model = os.environ.get("MINIMAX_MODEL", "claude-sonnet-4-6")
    else:
        logger.error(f"不支持的 AI_PROVIDER: {ai_provider}")
        sys.exit(1)

    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "")

    if not api_key:
        logger.error(f"未找到 {ai_provider} 的 API Key")
        sys.exit(1)

    # 3. 获取会话列表并匹配群名
    logger.info("正在获取微信会话列表...")
    sessions = get_sessions()
    logger.info(f"获取到 {len(sessions)} 个会话")

    matched_groups = []
    for keyword in keywords:
        matched = fuzzy_match_group(keyword, sessions)
        if matched and matched not in matched_groups:
            matched_groups.append(matched)
            logger.info(f"关键词 '{keyword}' 匹配到群: {matched}")
        else:
            logger.warning(f"关键词 '{keyword}' 未匹配到任何群")

    if not matched_groups:
        logger.error("没有任何群匹配成功，退出")
        sys.exit(1)

    # 4. 初始化 AI 客户端
    try:
        ai_client = create_ai_client(ai_provider, api_key, model)
        logger.info(f"AI 客户端初始化成功: {ai_provider}/{model}")
    except Exception as e:
        logger.error(f"AI 客户端初始化失败: {e}")
        sys.exit(1)

    # 5. 遍历处理每个群
    date_range = f"{start_time} ~ {end_time}"
    group_summaries = []
    failed_groups = []
    success_groups = []
    error_log_base = str(ERROR_LOG_DIR / "error.log")

    for group_name in matched_groups:
        logger.info(f"===== 开始处理群: {group_name} =====")

        retry_count = 0
        while retry_count < 3:
            try:
                # A. 获取消息
                messages, err, truncated = get_group_messages(group_name, start_time, end_time)
                if err:
                    logger.warning(f"获取消息失败: {err}")
                    if retry_count < 2:
                        retry_count += 1
                        continue
                    else:
                        failed_groups.append({"group_name": group_name, "reason": err})
                        break

                if not messages:
                    logger.warning(f"群 {group_name} 无消息")
                    if retry_count < 2:
                        retry_count += 1
                        logger.info(f"准备重试（第 {retry_count} 次）...")
                        continue
                    failed_groups.append({"group_name": group_name, "reason": "无消息"})
                    break

                logger.info(f"获取到原始消息 {len(messages)} 条")
                if truncated:
                    logger.warning(
                        f"群 {group_name} 命中消息上限 1000 条，结果可能被截断；"
                        "建议缩小时间范围或分段导出"
                    )

                # B. 预处理（复用 preprocessor.py：过滤短消息/表情 + 合并重复 + 剔除系统通知）
                messages = preprocess(messages, min_length=2)
                messages = remove_system(messages)
                logger.info(f"预处理后有效消息 {len(messages)} 条")

                if not messages:
                    logger.warning(f"群 {group_name} 预处理后无有效消息")
                    failed_groups.append({"group_name": group_name, "reason": "预处理后无有效消息"})
                    break

                # C. 构建文本
                raw_text = build_messages_text(messages)
                messages_text = raw_text

                # D. AI 摘要（处理超长文本）
                total_chars = len(messages_text)
                if total_chars > 8000:
                    logger.info(f"消息文本 {total_chars} 字符，超过 8000，按 4000 切分")
                    chunks = split_large_text(messages_text, 4000)
                    partials = generate_partial_summaries(ai_client, chunks, date_range)
                    summary_data = merge_partial_summaries(partials, ai_client, date_range)
                else:
                    logger.info("正在生成 AI 摘要...")
                    summary_data = ai_client.summarize_json(messages_text, date_range)

                # E. 统计成员活跃度
                member_count: Dict[str, int] = {}
                for msg in messages:
                    sender = msg.get("sender", "未知")
                    member_count[sender] = member_count.get(sender, 0) + 1

                member_activity = [
                    {"name": name, "messages": count}
                    for name, count in sorted(member_count.items(), key=lambda x: -x[1])
                ]
                summary_data["member_activity"] = member_activity

                # F. 保存摘要
                group_summaries.append({
                    "group_name": group_name,
                    "data": summary_data,
                    "total_messages": len(messages),
                    "summary_text": summary_data.get("brief_summary", ""),
                    "raw_text": raw_text[:5000] + ("..." if len(raw_text) > 5000 else ""),
                })
                success_groups.append(group_name)
                logger.info(f"群 {group_name} 处理完成")
                break

            except Exception as e:
                logger.error(f"处理群 {group_name} 时发生异常: {e}")
                if retry_count < 2:
                    retry_count += 1
                    logger.info(f"准备自愈重试（第 {retry_count} 次）...")
                    healed = self_heal(error_log_base, __file__, retry_count)
                    if not healed:
                        logger.warning("自愈未成功，继续重试")
                    continue
                else:
                    failed_groups.append({"group_name": group_name, "reason": str(e)})
                    break

    # 6. 生成报告
    if group_summaries:
        report_path = save_markdown_report(
            group_summaries,
            start_time,
            end_time,
            len(matched_groups),
            success_groups,
            failed_groups,
        )
    else:
        report_path = ""
        logger.warning("没有任何群处理成功，跳过报告生成")

    # 7. 推送通知
    if sendkey:
        title = f"微信群聊日报 [{len(success_groups)}]个群 [{start_time} ~ {end_time}]"
        success_list = "、".join(success_groups) if success_groups else "无"
        failed_list = "；".join([f"{f['group_name']}: {f['reason']}" for f in failed_groups]) if failed_groups else "无"
        desp = f"报告已生成：{report_path}\n\n成功：{success_list}\n失败：{failed_list}"

        logger.info("正在推送通知...")
        pushed = push_notification(sendkey, title, desp)
        if pushed:
            logger.info("推送成功")
        else:
            logger.error("推送失败")
    else:
        logger.warning("未配置 SERVERCHAN_SENDKEY，跳过推送")

    logger.info("========== 微信群聊日报导出完成 ==========")


if __name__ == "__main__":
    run()
