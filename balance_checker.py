#!/usr/bin/env python3
"""
API 账户余额查询与预警。
支持 DeepSeek / 硅基流动 / 智谱 / 通义千问 / MiniMax 等服务商的余额查询。
"""

import os
import logging
import requests
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── 各服务商余额查询配置 ─────────────────────────────────────────────────────
# 格式: (api_key_env, balance_url, currency_field, balance_field)
PROVIDER_BALANCE_CONFIG = {
    "deepseek": (
        "DEEPSEEK_API_KEY",
        "https://api.deepseek.com/user/balance",
        "currency",
        "total_balance",
    ),
    "siliconflow": (
        "SILICONFLOW_API_KEY",
        "https://api.siliconflow.cn/v1/user/info",
        "currency",
        "totalBalance",
    ),
    "zhipu": (
        "ZHIPU_API_KEY",
        None,  # 智谱暂无公开余额查询 API
        None,
        None,
    ),
    "qwen": (
        "QWEN_API_KEY",
        None,  # 通义千问余额需从阿里云控制台查看
        None,
        None,
    ),
    "minimax": (
        "MINIMAX_API_KEY",
        None,  # MiniMax 暂无公开余额查询 API
        None,
        None,
    ),
}


def query_balance(provider: str) -> Optional[Tuple[str, float]]:
    """
    查询指定 provider 的账户余额。
    返回 (currency, balance) 或 None（不支持查询 / 查询失败）。
    """
    provider = provider.lower()
    if provider not in PROVIDER_BALANCE_CONFIG:
        return None

    api_key_env, balance_url, currency_field, balance_field = PROVIDER_BALANCE_CONFIG[provider]

    if balance_url is None:
        logger.debug("provider %s 不支持余额查询", provider)
        return None

    api_key = os.environ.get(api_key_env)
    if not api_key:
        logger.warning("未设置 %s，无法查询余额", api_key_env)
        return None

    try:
        resp = requests.get(
            balance_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("查询 %s 余额失败: %s", provider, e)
        return None

    # ── DeepSeek: {"balance_infos": [{"currency": "CNY", "total_balance": "2.42", ...}]} ──
    if provider == "deepseek":
        infos = data.get("balance_infos", [])
        # 优先取 CNY
        for info in infos:
            if info.get(currency_field) == "CNY":
                return "CNY", float(info.get(balance_field, 0))
        # 降级取第一条
        if infos:
            return infos[0].get(currency_field, "?"), float(infos[0].get(balance_field, 0))

    # ── SiliconFlow: {"totalBalance": "0.50", "currency": "CNY"} ──
    if provider == "siliconflow":
        currency = data.get(currency_field, "CNY")
        balance = float(data.get(balance_field, 0))
        return currency, balance

    return None


def check_balance_warning(provider: str, threshold: float = 5.0) -> Optional[str]:
    """
    检查余额是否低于阈值，返回预警消息或 None。
    threshold: 余额阈值（CNY），低于此值触发预警。
    """
    result = query_balance(provider)
    if result is None:
        return None

    currency, balance = result
    if balance < threshold:
        return (
            f"⚠️ {provider} 账户余额不足: {balance:.2f} {currency} "
            f"(阈值: {threshold:.2f} {currency})，请及时充值！"
        )

    logger.info("%s 余额: %.2f %s", provider, balance, currency)
    return None