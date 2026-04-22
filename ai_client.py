import logging
from abc import ABC, abstractmethod
import requests

logger = logging.getLogger(__name__)


class AIServiceError(Exception):
    """AI 服务调用错误。"""
    pass


class BaseAIClient(ABC):
    def __init__(self):
        self._token_count = 0
        self._last_tokens = 0

    @property
    @abstractmethod
    def _api_url(self) -> str:
        """API 端点地址。"""
        pass

    @property
    @abstractmethod
    def _provider_name(self) -> str:
        """提供商名称（用于日志）。"""
        pass

    def summarize(self, messages_text: str, date_range: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一个微信群聊摘要助手。请根据以下群聊消息按不同主题分类整理摘要，格式为：\n1. 参与讨论的成员：[昵称列表]\n2. 各主题摘要（每主题列出关键信息和重要观点，500字以内）\n如有多个主题请分条列出，条理清晰。\n若消息中包含「=== 图片内容分析 ===」部分，请将图片分析结果融入对应的主题摘要中，不要单独列出图片分析章节。"},
                {"role": "user", "content": f"日期范围: {date_range}\n\n群聊消息:\n{messages_text}"}
            ],
            "max_tokens": 2000,
            "temperature": 0.3
        }
        try:
            resp = requests.post(self._api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"{self._provider_name} API 调用失败: {e}")
            raise AIServiceError(f"{self._provider_name} API 调用失败: {e}") from e

        result = resp.json()
        if "choices" not in result:
            logger.error(f"{self._provider_name} API 异常响应: {result}")
            raise AIServiceError(f"{self._provider_name} API 异常响应: {result}")

        content = result["choices"][0]["message"]["content"]
        # 优先使用 API 返回的真实 token 用量，降级到估算
        usage = result.get("usage", {})
        if usage and usage.get("total_tokens"):
            self._last_tokens = usage["total_tokens"]
            self._token_count += self._last_tokens
        else:
            self._last_tokens = len(messages_text) // 2 + 200
            self._token_count += self._last_tokens
        return content

    @property
    def estimated_tokens(self) -> int:
        return self._token_count

    @property
    def last_tokens(self) -> int:
        """本次 summarize 调用的 token 用量。"""
        return self._last_tokens


class DeepSeekClient(BaseAIClient):
    def __init__(self, api_key: str, model: str = "deepseek-v3-0324"):
        super().__init__()
        self.api_key = api_key
        self.model = model

    @property
    def _api_url(self) -> str:
        return "https://api.deepseek.com/chat/completions"

    @property
    def _provider_name(self) -> str:
        return "DeepSeek"


class SiliconFlowClient(BaseAIClient):
    def __init__(self, api_key: str, model: str = "glm-5.1"):
        super().__init__()
        self.api_key = api_key
        self.model = model

    @property
    def _api_url(self) -> str:
        return "https://api.siliconflow.cn/v1/chat/completions"

    @property
    def _provider_name(self) -> str:
        return "SiliconFlow"


class ZhipuClient(BaseAIClient):
    def __init__(self, api_key: str, model: str = "glm-4.7"):
        super().__init__()
        self.api_key = api_key
        self.model = model

    @property
    def _api_url(self) -> str:
        return "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    @property
    def _provider_name(self) -> str:
        return "智谱"


class QwenClient(BaseAIClient):
    def __init__(self, api_key: str, model: str = "qwen3.6-plus"):
        super().__init__()
        self.api_key = api_key
        self.model = model

    @property
    def _api_url(self) -> str:
        return "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    @property
    def _provider_name(self) -> str:
        return "Qwen"


class MiniMaxClient(BaseAIClient):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        super().__init__()
        self.api_key = api_key
        self.model = model

    @property
    def _api_url(self) -> str:
        return "https://api.minimax.chat/v1/text/chatcompletion_v2"

    @property
    def _provider_name(self) -> str:
        return "MiniMax"


_PROVIDER_MODELS = {
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "deepseek-v3-0324"),
    "siliconflow": ("SILICONFLOW_API_KEY", "SILICONFLOW_MODEL", "glm-5.1"),
    "zhipu": ("ZHIPU_API_KEY", "ZHIPU_MODEL", "glm-4.7"),
    "qwen": ("QWEN_API_KEY", "QWEN_MODEL", "qwen3.6-plus"),
    "minimax": ("MINIMAX_API_KEY", "MINIMAX_MODEL", "claude-sonnet-4-6"),
}

_PROVIDER_CLIENTS = {
    "deepseek": DeepSeekClient,
    "siliconflow": SiliconFlowClient,
    "zhipu": ZhipuClient,
    "qwen": QwenClient,
    "minimax": MiniMaxClient,
}


def create_ai_client(provider: str, **kwargs) -> BaseAIClient:
    """工厂函数，根据 AI_PROVIDER 创建客户端。"""
    provider = provider.lower()
    if provider not in _PROVIDER_CLIENTS:
        raise ValueError(f"不支持的 AI 服务商: {provider}，支持的: {list(_PROVIDER_CLIENTS.keys())}")
    return _PROVIDER_CLIENTS[provider](**kwargs)
