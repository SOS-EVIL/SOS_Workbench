"""core/deepseek_client.py - DeepSeek（OpenAI 兼容）客户端。"""

from openai import OpenAI

from config.models import BASE_URL, DEFAULT_MODEL, EXTRA_REQUEST_PARAMS

DEFAULT_MAX_TOKENS = 4096
REQUEST_TIMEOUT = 120


class DeepSeekClient:
    def __init__(self, api_key=None, model=None, base_url=None, timeout=REQUEST_TIMEOUT):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.base_url = base_url or BASE_URL
        self.timeout = timeout
        self.client = None
        self.history = []
        if api_key:
            try:
                self.client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)
            except Exception:
                self.client = None

    def _create(self, messages, max_tokens=None):
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "temperature": 0.2,
        }
        if EXTRA_REQUEST_PARAMS:
            kwargs.update(EXTRA_REQUEST_PARAMS)
        return self.client.chat.completions.create(**kwargs)

    @staticmethod
    def _extract_text(resp):
        """取回复正文。思考模式下正文可能为空，此时回退到 reasoning_content。"""
        try:
            message = resp.choices[0].message
        except (AttributeError, IndexError):
            return "模型返回为空"
        content = (getattr(message, "content", None) or "").strip()
        if content:
            return content
        reasoning = (getattr(message, "reasoning_content", None) or "").strip()
        return reasoning or "模型返回空内容"

    def send(self, prompt, context="", max_tokens=None):
        if not self.client:
            return "聊天功能未启用，请设置 API Key"

        messages = [{"role": "system", "content": "你是一个通用任务编排助手。"}]
        if context:
            messages.append({"role": "user", "content": f"背景：{context}"})

        self.history.append({"role": "user", "content": prompt})
        messages.extend(self.history[-5:])

        try:
            resp = self._create(messages, max_tokens=max_tokens)
        except Exception as e:
            return f"模型调用失败：{e}"

        reply = self._extract_text(resp)
        self.history.append({"role": "assistant", "content": reply})
        return reply
