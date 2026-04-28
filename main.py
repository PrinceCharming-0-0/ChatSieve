#!/usr/bin/env python3
"""
微信群每日摘要 — 主脚本
功能：定时从指定微信群提取消息 → AI 生成摘要 → Server酱³ 推送至手机
依赖：requests, python-dotenv
"""

import os
import sys
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime, date
from dotenv import load_dotenv

# ── 确保 cron 环境包含必要路径 ──────────────────────────────────────────────
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

# ── 本地模块 ────────────────────────────────────────────────────────────────
from logging_utils import configure_logger
from wechat_client import WeChatClient
from preprocessor import preprocess
from ai_client import create_ai_client, AIServiceError, _PROVIDER_MODELS
from token_tracker import TokenTracker
from balance_checker import check_balance_warning
from pusher import ServerChanPusher
from image_analyzer import ImageAnalyzer

# ── 路径常量 ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
STATE_FILE = BASE_DIR / "run_state.json"
ENV_FILE = BASE_DIR / ".env"

# ─────────────────────────────────────────────────────────────────────────────
# 日志配置：按日切分，ERROR 级别单独归档
# ─────────────────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    return configure_logger(
        log_dir=LOG_DIR,
        main_log_filename="wechat_summary.log",
        error_log_filename="error.log",
        backup_count=30,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 状态管理：跨次运行持久化
# ─────────────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 环境变量校验
# ─────────────────────────────────────────────────────────────────────────────
REQUIRED_KEYS = [
    "WECHAT_GROUP_NAME",
    "AI_PROVIDER",
    "SERVERCHAN_SENDKEY",
]

# AI_PROVIDER 对应的必需 key
PROVIDER_REQUIRED_KEYS = {
    "deepseek": ["DEEPSEEK_API_KEY"],
    "siliconflow": ["SILICONFLOW_API_KEY"],
    "zhipu": ["ZHIPU_API_KEY"],
    "qwen": ["QWEN_API_KEY"],
    "minimax": ["MINIMAX_API_KEY"],
}


def check_env() -> None:
    missing = [k for k in REQUIRED_KEYS if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"缺少必需的环境变量: {', '.join(missing)}")

    provider = os.environ["AI_PROVIDER"].lower()
    extra = PROVIDER_REQUIRED_KEYS.get(provider, [])
    missing_extra = [k for k in extra if not os.environ.get(k)]
    if missing_extra:
        raise EnvironmentError(
            f"AI_PROVIDER={provider} 缺少必需变量: {', '.join(missing_extra)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 错误日志自愈
# ─────────────────────────────────────────────────────────────────────────────
def self_heal(logger: logging.Logger, state: dict) -> bool:
    """
    检查 logs/ 目录下是否有 ERROR 日志，若有则：
    1. 打印错误日志路径和自助排查建议
    2. 若 free-code 可用，异步启动分析（不阻塞主流程）
    返回是否发现错误。
    """
    # 优先查找当日 rotate 后的归档，降级到当前 error.log
    today = datetime.now().strftime("%Y-%m-%d")
    error_log = LOG_DIR / f"error.log.{today}"   # TimedRotatingFileHandler 归档格式为 basename.YYYY-MM-DD
    if not error_log.exists():
        error_log = LOG_DIR / "error.log"

    if not error_log.exists():
        return False

    try:
        content = error_log.read_text(encoding="utf-8")
    except Exception:
        return False

    error_lines = [line.strip() for line in content.splitlines() if "ERROR" in line]
    if not error_lines:
        return False

    latest_error_sig = f"{error_log}:{error_lines[-1]}"
    if state.get("last_healed_error_sig") == latest_error_sig:
        return False

    state["last_healed_error_sig"] = latest_error_sig

    logger.warning("=== 自愈机制触发：检测到 ERROR 日志 ===")
    logger.info(
        "错误日志: %s\n"
        "自助排查：\n"
        "1. 查看上述错误日志\n"
        "2. 确认 wechat-cli 初始化正常 (wechat-cli init)\n"
        "3. 确认网络和 API Key 正确",
        error_log,
    )

    # 若 free-code 可用，异步启动分析（不阻塞主流程退出）
    try:
        subprocess.Popen(
            ["free-code", "--analyze-log", str(error_log)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("free-code 已异步启动分析，结果请查看 free-code 输出")
    except FileNotFoundError:
        logger.debug("free-code 未安装，跳过自动分析")
    except Exception as e:
        logger.debug("free-code 启动失败: %s", e)

    return True


# ─────────────────────────────────────────────────────────────────────────────
# 构建摘要文本
# ─────────────────────────────────────────────────────────────────────────────
def build_messages_text(messages: list, date_range: str) -> str:
    """将结构化消息列表转换为发送给 AI 的纯文本。"""
    if not messages:
        return "（今日无有效消息）"

    # 时间范围：最早 → 最晚
    first_ts = messages[0]["timestamp"].strftime("%Y-%m-%d %H:%M")
    last_ts = messages[-1]["timestamp"].strftime("%Y-%m-%d %H:%M")
    time_range = f"{first_ts} ~ {last_ts}"

    # 参与成员（去重，保持顺序）
    seen = set()
    members = []
    for msg in messages:
        sender = msg.get("sender", "未知")
        if sender not in seen:
            seen.add(sender)
            members.append(sender)

    lines = [f"=== {date_range} 群聊记录 ===", ""]
    lines.append(f"时间段：{time_range}")
    lines.append(f"参与成员：{', '.join(members)}")
    lines.append("")

    current_date = None
    for msg in messages:
        msg_date = msg["timestamp"].strftime("%Y-%m-%d")
        if msg_date != current_date:
            current_date = msg_date
            lines.append(f"--- {msg_date} ---")

        sender = msg.get("sender", "未知")
        content = msg.get("content", "")
        merged = msg.get("_merged_count", 1)
        suffix = f" (×{merged})" if merged > 1 else ""
        lines.append(f"[{msg['timestamp'].strftime('%H:%M')}] {sender}: {content}{suffix}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 消息文本分段（避免超长输入）
# ─────────────────────────────────────────────────────────────────────────────
def split_messages_text(messages_text: str, max_chars: int = 32000) -> list:
    """
    将超长消息文本分段，优先在消息边界（日期分隔符或消息行）处切分。
    返回分段列表，每段不超过 max_chars。
    """
    if len(messages_text) <= max_chars:
        return [messages_text]

    chunks = []
    lines = messages_text.split("\n")
    current_chunk = []
    current_length = 0

    for line in lines:
        line_length = len(line) + 1  # +1 for newline

        # 如果加上这行会超过限制
        if current_length + line_length > max_chars:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_length = 0

            # 如果单行就超过限制，强制截断
            if line_length > max_chars:
                chunks.append(line[:max_chars])
                continue

        current_chunk.append(line)
        current_length += line_length

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks if chunks else [""]


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
def run():
    logger = setup_logging()
    logger.info("========== 微信群每日摘要开始 ==========")

    # 1. 加载 .env
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
        logger.info("已加载 .env 配置文件")
    else:
        logger.warning(".env 文件不存在，将从系统环境变量读取配置")

    # 2. 校验环境变量
    try:
        check_env()
    except EnvironmentError as e:
        logger.error("环境变量校验失败: %s", e)
        sys.exit(1)

    # 支持多个群聊名称（逗号分隔）
    group_names = [
        g.strip() for g in os.environ["WECHAT_GROUP_NAME"].split(",") if g.strip()
    ]
    days = int(os.environ.get("WECHAT_DAYS", "1"))
    limit = int(os.environ.get("WECHAT_MESSAGE_LIMIT", "200"))
    warning_ratio = float(os.environ.get("TOKEN_WARNING_RATIO", "0.9"))

    logger.info(
        "配置: 群名=%s, 天数=%d, 消息上限=%d",
        group_names, days, limit,
    )

    # 3. Token 追踪器（用于 token 额度预警模式）
    tracker = TokenTracker(warning_ratio=warning_ratio, tracker_key="text")
    tracker.reset_if_new_day()

    # 视觉模型 tracker（仅当 VISION_WARNING_MODE=token 时需要）
    vision_warning_mode = os.environ.get("VISION_WARNING_MODE", "token").lower()
    vision_tracker = None
    if vision_warning_mode == "token":
        vision_tracker = TokenTracker(
            warning_ratio=warning_ratio,
            tracker_key="vision",
        )
        vision_tracker.reset_if_new_day()

    # 4. 运行前预警检查
    text_warning_mode = os.environ.get("TEXT_WARNING_MODE", "balance").lower()
    balance_threshold = float(os.environ.get("BALANCE_WARNING_THRESHOLD", "5.0"))

    if text_warning_mode == "balance":
        pre_warning = check_balance_warning(os.environ["AI_PROVIDER"].lower(), threshold=balance_threshold)
        if pre_warning:
            logger.warning(pre_warning)
    else:  # token 模式（累计总额度）
        text_total_limit = int(os.environ.get("TEXT_TOKEN_TOTAL_LIMIT", "50000"))
        pre_warning = tracker.check_total_warning(text_total_limit)
        if pre_warning:
            logger.warning(pre_warning)

    state = load_state()
    last_run = state.get("last_run")
    last_run_date = state.get("last_run_date")
    last_run_count = state.get("last_run_count", 0)

    # 判断当天是否已运行过
    today_str = date.today().strftime("%Y-%m-%d")
    if last_run_date == today_str:
        # 当天已运行过，次数递增
        run_count = last_run_count + 1
    else:
        # 新的一天，重置次数
        run_count = 1

    logger.info("上次运行时间: %s", last_run or "首次运行")

    try:
        # ── AI 客户端初始化（多个群共享） ──────────────────────────────────
        provider = os.environ["AI_PROVIDER"].lower()
        logger.info("正在初始化 AI 服务: %s", provider)

        api_key_env, model_env, default_model = _PROVIDER_MODELS[provider]
        ai_kwargs = {
            "api_key": os.environ[api_key_env],
            "model": os.environ.get(model_env, default_model),
        }
        ai_client = create_ai_client(provider, **ai_kwargs)

        # ── 循环处理每个群 ────────────────────────────────────────────────
        date_range = f"{today_str}（最近 {days} 天）"
        all_summaries = []
        total_messages_fetched = 0
        total_messages_filtered = 0
        total_tokens_used = 0
        groups_with_messages = []

        for group_name in group_names:
            logger.info("===== 开始处理群: %s =====", group_name)

            # ── 步骤 A：获取消息 ───────────────────────────────────────────
            logger.info("正在从微信群获取消息: %s", group_name)
            try:
                client = WeChatClient(group_name=group_name, days=days)
                messages = client.get_recent_messages(limit=limit)
            except Exception as e:
                logger.warning("获取群 %s 的消息失败: %s", group_name, e)
                continue

            if not messages:
                logger.warning("群 %s 未获取到任何消息，跳过", group_name)
                continue

            logger.info("获取到原始消息 %d 条", len(messages))
            total_messages_fetched += len(messages)

            # ── 步骤 B：预处理 ─────────────────────────────────────────────
            filtered = preprocess(messages, min_length=2)

            if not filtered:
                logger.warning("群 %s 预处理后无有效消息，跳过", group_name)
                continue

            logger.info("群 %s 预处理后有效消息 %d 条", group_name, len(filtered))
            total_messages_filtered += len(filtered)

            # ── 步骤 C：图片分析（在构建 AI 输入之前） ─────────────────────────
            image_result_text = ""
            image_enabled = os.environ.get("ENABLE_IMAGE_ANALYSIS", "").lower() in ("1", "true", "yes")
            if image_enabled:
                logger.info("正在为群 %s 执行图片分析", group_name)
                try:
                    # 视觉模型使用独立的 vision_tracker（若启用 token 预警）
                    img_analyzer = ImageAnalyzer(
                        group_name=group_name, days=days, limit=limit,
                        token_tracker=vision_tracker,
                    )
                    image_result_text = img_analyzer.analyze(filtered, ai_client)
                    if image_result_text:
                        total_tokens_used += img_analyzer.total_image_tokens
                        logger.info("群 %s 图片分析完成，结果将融入摘要，视觉 token: %d", group_name, img_analyzer.total_image_tokens)
                except Exception as e:
                    logger.warning("群 %s 图片分析失败（不影响主流程）: %s", group_name, e)

            # ── 步骤 D：构建 AI 输入（融合图片分析结果） ───────────────────
            messages_text = build_messages_text(filtered, date_range)

            if image_result_text:
                messages_text += f"\n\n=== 图片内容分析 ===\n{image_result_text}"

            # ── 步骤 E：分段处理超长消息 ─────────────────────────────────────
            MAX_INPUT_CHARS = 32000
            text_chunks = split_messages_text(messages_text, MAX_INPUT_CHARS)

            if len(text_chunks) > 1:
                logger.info("群 %s 消息文本过长（%d 字符），已分为 %d 段处理",
                           group_name, len(messages_text), len(text_chunks))

            # ── 步骤 F：对每段分别生成 AI 摘要 ──────────────────────────────
            logger.info("正在为群 %s 生成 AI 摘要", group_name)
            segment_summaries = []

            for idx, chunk in enumerate(text_chunks):
                try:
                    if len(text_chunks) > 1:
                        logger.info("正在处理第 %d/%d 段", idx + 1, len(text_chunks))

                    summary = ai_client.summarize(chunk, date_range)

                    # 记录 token 使用量
                    used_tokens = ai_client.last_tokens
                    tracker.add_usage(used_tokens)
                    total_tokens_used += used_tokens

                    segment_summaries.append(summary)
                    logger.info("第 %d/%d 段摘要完成，使用 token: %d",
                               idx + 1, len(text_chunks), used_tokens)

                except AIServiceError as e:
                    logger.error("群 %s 第 %d/%d 段 AI 摘要失败: %s",
                                group_name, idx + 1, len(text_chunks), e)
                    continue

            if not segment_summaries:
                logger.error("群 %s 所有分段摘要均失败，跳过", group_name)
                continue

            # 合并多段摘要
            if len(segment_summaries) > 1:
                final_summary = "\n\n---\n\n".join(
                    f"**第 {i+1} 部分**\n{s}" for i, s in enumerate(segment_summaries)
                )
            else:
                final_summary = segment_summaries[0]

            # 添加群名到摘要中
            all_summaries.append(f"### {group_name}\n{final_summary}")
            groups_with_messages.append(group_name)

        # ── 步骤 E：Server酱³ 推送 ──────────────────────────────────────
        if not all_summaries:
            logger.warning("所有群均无有效消息，脚本退出")
            state["last_run"] = datetime.now().isoformat()
            save_state(state)
            return

        sendkey = os.environ["SERVERCHAN_SENDKEY"]
        pusher = ServerChanPusher(sendkey)

        # 构建标题
        title = f"{today_str} 微信群聊天记录摘要（{run_count}）"

        # 合并摘要（不同群之间空一行）
        combined_summary = "\n\n".join(all_summaries)
        pushed = pusher.push(title, combined_summary)

        if pushed:
            logger.info("Server酱³ 推送成功")
        else:
            logger.error("Server酱³ 推送失败，请检查 SendKey")

        # ── 步骤 F：预警检查（根据配置模式） ─────────────────────────────────────
        text_warning_mode = os.environ.get("TEXT_WARNING_MODE", "balance").lower()
        vision_warning_mode = os.environ.get("VISION_WARNING_MODE", "token").lower()
        balance_threshold = float(os.environ.get("BALANCE_WARNING_THRESHOLD", "5.0"))

        # 文本模型预警
        if text_warning_mode == "balance":
            text_warning = check_balance_warning(provider, threshold=balance_threshold)
        else:  # token 模式（累计总额度）
            text_total_limit = int(os.environ.get("TEXT_TOKEN_TOTAL_LIMIT", "50000"))
            text_warning = tracker.check_total_warning(text_total_limit)
        if text_warning:
            logger.warning(text_warning)
            pusher.push("⚠️ 文本模型预警", text_warning)

        # 视觉模型预警（仅当启用了图片分析）
        if image_enabled:
            if vision_warning_mode == "balance":
                vision_provider_env = os.environ.get("VISION_BALANCE_PROVIDER", "").lower()
                if vision_provider_env:
                    vision_warning = check_balance_warning(vision_provider_env, threshold=balance_threshold)
                    if vision_warning:
                        logger.warning(vision_warning)
                        pusher.push("⚠️ 视觉模型余额预警", vision_warning)
            else:  # token 模式（累计总额度）
                if vision_tracker:
                    vision_total_limit = int(os.environ.get("VISION_TOKEN_LIMIT", "1000000"))
                    vision_warning = vision_tracker.check_total_warning(vision_total_limit)
                    if vision_warning:
                        logger.warning(vision_warning)
                        pusher.push("⚠️ 视觉模型额度预警", vision_warning)

        # ── 步骤 G：保存运行状态 ─────────────────────────────────────────
        state["last_run"] = datetime.now().isoformat()
        state["last_run_date"] = today_str
        state["last_run_count"] = run_count
        state["last_groups"] = groups_with_messages
        state["messages_fetched"] = total_messages_fetched
        state["messages_filtered"] = total_messages_filtered
        state["tokens_used"] = total_tokens_used
        save_state(state)

        logger.info("========== 微信群每日摘要完成 ==========")

    except AIServiceError as e:
        logger.error("AI 服务调用失败: %s", e)
        sys.exit(1)
    except ValueError as e:
        logger.error("数据错误: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.exception("未预期错误: %s", e)
        sys.exit(1)

    # ── 自愈机制 ────────────────────────────────────────────────────────────
    finally:
        healed = self_heal(logger, state)
        if healed:
            save_state(state)
            logger.info("自愈机制已执行（详见上方建议）")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()
