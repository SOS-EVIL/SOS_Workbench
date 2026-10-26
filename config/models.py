"""config/models.py - 模型清单与 API 端点。

本项目默认只对接 DeepSeek。要接其它 OpenAI 兼容服务，
改这里的 BASE_URL 与 MODELS 即可（详见 README「模型」一节与 项目结构.txt）。
"""

# OpenAI 格式端点；Anthropic 格式为 https://api.deepseek.com/anthropic
BASE_URL = "https://api.deepseek.com"

# 请求级附加参数。需要开启思考模式时，按官方文档把参数加到这里，例如：
# EXTRA_REQUEST_PARAMS = {"thinking": {"type": "enabled"}}
EXTRA_REQUEST_PARAMS = {}

MODELS = (
    {
        "id": "deepseek-flash",
        "label": "DEEPSEEK-V4.1FLASH",
        "version": "DeepSeek-V4.1-Flash-1017",
    },
    {
        "id": "deepseek-v4-pro",
        "label": "DEEPSEEK-V4-PRO",
        "version": "DeepSeek-V4-Pro-0813",
    },
)

MODEL_IDS = tuple(spec["id"] for spec in MODELS)
DEFAULT_MODEL = "deepseek-flash"

_LABELS = {spec["id"]: spec["label"] for spec in MODELS}


def label_of(model_id: str) -> str:
    return _LABELS.get(model_id, model_id or DEFAULT_MODEL)
