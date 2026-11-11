"""菲比啾比语音合成引擎注册表与工厂模块"""

from __future__ import annotations

from pathlib import Path

from .aliyun import AliyunChirpEngine
from .base import ChirpTTSEngine
from .custom_http import CustomHTTPChirpEngine
from .mimo import MiMoChirpEngine
from .openai_compat import OpenAICompatChirpEngine

ENGINE_REGISTRY: dict[str, type[ChirpTTSEngine]] = {
    "aliyun": AliyunChirpEngine,
    "openai_compat": OpenAICompatChirpEngine,
    "mimo": MiMoChirpEngine,
    "custom_http": CustomHTTPChirpEngine,
}

ENGINE_META: dict[str, dict] = {
    "aliyun": {
        "name": "阿里云百炼 (Qwen-Audio / CosyVoice)",
        "description": "支持 qwen-audio-3.1-tts-flash / cosyvoice-v3-flash 等模型，支持零样本声音复刻与自然语言语气指令控制。",
        "default_api_base": "https://dashscope.aliyuncs.com/api/v1",
        "default_model": "qwen-audio-3.1-tts-flash",
        "supports_clone": True,
        "supports_instruction": True,
    },
    "openai_compat": {
        "name": "OpenAI 兼容接口 (/v1/audio/speech)",
        "description": "兼容 OpenAI 标准语音合成接口，支持 instructions 情绪指令（如 gpt-4o-mini-tts）。",
        "default_api_base": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini-tts",
        "supports_clone": False,
        "supports_instruction": True,
    },
    "mimo": {
        "name": "小米 MiMo (V2.5 TTS / VoiceClone)",
        "description": "支持小米 MiMo V2.5 预置音色与参考音频样本克隆，支持自然语言风格指令。",
        "default_api_base": "https://api.xiaomimimo.com/v1",
        "default_model": "mimo-v2.5-tts",
        "supports_clone": True,
        "supports_instruction": True,
    },
    "custom_http": {
        "name": "自定义 HTTP 引擎",
        "description": "通过 URL 与 JSON 请求体模板接入任意第三方 TTS API，支持 ${TEXT}、${INSTRUCTION}、${VOICE} 等占位符。",
        "default_api_base": "",
        "default_model": "",
        "supports_clone": True,
        "supports_instruction": True,
    },
}


def register_engine(
    engine_type: str,
    engine_cls: type[ChirpTTSEngine],
    meta: dict | None = None,
) -> None:
    """动态注册新的 TTS 引擎适配器，方便后续扩展其他 API。"""
    key = (engine_type or "").strip().lower()
    if not key or not issubclass(engine_cls, ChirpTTSEngine):
        raise ValueError(f"无效的引擎注册参数: {engine_type}")
    ENGINE_REGISTRY[key] = engine_cls
    if meta:
        ENGINE_META[key] = meta


def create_engine(
    engine_type: str,
    config: dict,
    *,
    data_dir: Path | None = None,
) -> ChirpTTSEngine:
    """根据引擎类型与合并后的配置字典创建 TTS 引擎实例。"""
    key = (engine_type or "aliyun").strip().lower() or "aliyun"
    engine_cls = ENGINE_REGISTRY.get(key)
    if engine_cls is None:
        available = ", ".join(sorted(ENGINE_REGISTRY.keys()))
        raise ValueError(f"未知的语音合成引擎类型 '{key}'，可用类型: {available}")
    return engine_cls(config, data_dir=data_dir)


__all__ = [
    "AliyunChirpEngine",
    "ChirpTTSEngine",
    "CustomHTTPChirpEngine",
    "ENGINE_META",
    "ENGINE_REGISTRY",
    "MiMoChirpEngine",
    "OpenAICompatChirpEngine",
    "create_engine",
    "register_engine",
]
