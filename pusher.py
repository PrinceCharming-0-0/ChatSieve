import logging
import time
import requests

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF = 2   # 首次退避秒数


class ServerChanPusher:
    """
    Server酱³ 推送。
    文档: https://doc.sc3.ft07.com/zh/serverchan3
    """

    def __init__(self, sendkey: str):
        self.sendkey = sendkey
        self.api_url = f"https://sctapi.ftqq.com/{sendkey}.send"
        self.chunk_size = 21000  # Server酱³ desp 上限 21785字，留余量

    def push(self, title: str, content: str) -> bool:
        """
        发送推送，失败时按指数退避重试。
        返回 True 成功，False 失败。
        """
        # 按字数分段
        chunks = self._split_content(content)
        all_ok = True

        for i, chunk in enumerate(chunks):
            chunk_title = title if i == 0 else f"{title} ({(i + 1)}/{len(chunks)})"
            payload = {
                "title": chunk_title,
                "desp": chunk,
            }
            if not self._send_with_retry(payload, i, len(chunks)):
                all_ok = False

        return all_ok

    def _send_with_retry(self, payload: dict, chunk_idx: int, total_chunks: int) -> bool:
        """发送单段推送，失败时指数退避重试。"""
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = requests.post(self.api_url, json=payload, timeout=30)
                resp.raise_for_status()
                result = resp.json()
                if result.get("code") != 0:
                    logger.error("Server酱推送失败 [%d/%d]: %s", chunk_idx + 1, total_chunks, result)
                    raise ValueError(f"Server酱返回错误码: {result.get('code')}")
                logger.info("Server酱推送成功 [%d/%d]", chunk_idx + 1, total_chunks)
                return True
            except Exception as e:
                if attempt < MAX_RETRIES:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "Server酱推送失败 [%d/%d]，%d秒后重试 (%d/%d): %s",
                        chunk_idx + 1, total_chunks, backoff, attempt + 1, MAX_RETRIES, e,
                    )
                    time.sleep(backoff)
                else:
                    logger.error(
                        "Server酱推送最终失败 [%d/%d]，重试耗尽: %s",
                        chunk_idx + 1, total_chunks, e,
                    )
        return False

    def _split_content(self, content: str) -> list:
        """
        按 chunk_size 分段，优先以空行（段落）为切分点，尽量保留 Markdown 结构。
        """
        if not content:
            return [""]

        # 策略1：按空行切分段，优先保留段落完整性
        paragraphs = content.split("\n\n")
        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            if len(current) + len(para) + 2 <= self.chunk_size:
                current = (current + "\n\n" + para).strip()
            else:
                if current:
                    chunks.append(current)
                # 如果单个段落就超过 chunk_size，进入策略2
                if len(para) > self.chunk_size:
                    chunks.extend(self._split_block(para))
                    current = ""
                else:
                    current = para

        if current:
            chunks.append(current)

        # 兜底：仍有超长块时强制截断
        final: list[str] = []
        for chunk in chunks:
            if len(chunk) <= self.chunk_size:
                final.append(chunk)
            else:
                final.extend(self._split_block(chunk))

        return final if final else [""]

    def _split_block(self, block: str) -> list:
        """强制截断超长块，在行边界或句末（。！？）处切分。"""
        parts = []
        while len(block) > self.chunk_size:
            cutoff = block[:self.chunk_size]
            # 找最后一个换行或句末标点
            split_pos = max(
                cutoff.rfind("\n"),
                cutoff.rfind("。"),
                cutoff.rfind("！"),
                cutoff.rfind("？"),
            )
            if split_pos > self.chunk_size * 0.3:
                split_pos += 1  # 含标点本身
            else:
                split_pos = self.chunk_size
            parts.append(block[:split_pos].strip())
            block = block[split_pos:].lstrip()
        if block:
            parts.append(block)
        return parts
