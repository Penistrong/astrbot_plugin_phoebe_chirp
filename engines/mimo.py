"""小米 MiMo 语音合成与声音克隆引擎"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from .base import ChirpTTSEngine


class MiMoChirpEngine(ChirpTTSEngine):
    """小米 MiMo V2.5 语音合成与音色克隆引擎。

    支持：
    - 预置音色合成 (`mimo-v2.5-tts`)
    - 音频样本零样本克隆 (`mimo-v2.5-tts-voiceclone`)
    - 通过 user message 传入自然语言情绪 `instruction`。
    """

    engine_type = "mimo"
    DEFAULT_API_BASE = "https://api.xiaomimimo.com/v1"

    def __init__(
        self,
        config: dict,
        *,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(config, data_dir=data_dir)
        self.api_key: str = str(self.config.get("api_key") or "").strip()
        self.api_base: str = (
            str(self.config.get("api_base") or self.DEFAULT_API_BASE).strip().rstrip("/")
        )
        self.sample_name: str = str(self.config.get("sample_name") or "").strip()
        default_model = (
            "mimo-v2.5-tts-voiceclone" if self.sample_name else "mimo-v2.5-tts"
        )
        self.model: str = str(self.config.get("model") or default_model).strip()
        self.voice_id: str = str(
            self.config.get("voice_id")
            or self.config.get("default_voice")
            or "mimo_default"
        ).strip()
        self.audio_format: str = (
            str(self.config.get("format") or "wav").strip().lower().lstrip(".")
        )
        self.timeout: int = int(self.config.get("timeout", 60) or 60)
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            raise ValueError("MiMo TTS: 未配置 api_key")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    def _resolve_voice_field(self, override_voice: str = "") -> str:
        if override_voice and override_voice.strip():
            return override_voice.strip()
        if self.sample_name:
            sample_path = self.resolve_sample_path(self.sample_name)
            return self.encode_audio_data_uri(sample_path, with_prefix=True)
        return self.voice_id or "mimo_default"

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str = "",
    ) -> tuple[bytes, str]:
        client = self._get_client()
        url = f"{self.api_base}/chat/completions"
        messages: list[dict[str, str]] = []
        if instruction and instruction.strip():
            messages.append({"role": "user", "content": instruction.strip()})
        elif "voiceclone" in self.model.lower():
            messages.append({"role": "user", "content": ""})
        messages.append({"role": "assistant", "content": text})

        payload = {
            "model": self.model,
            "messages": messages,
            "audio": {
                "format": self.audio_format,
                "voice": self._resolve_voice_field(voice),
            },
        }
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }
        resp = await client.post(url, headers=headers, json=payload)
        self.check_http_response("MiMo", resp)
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"MiMo TTS 返回空 choices: {data}")
        audio_b64 = choices[0].get("message", {}).get("audio", {}).get("data")
        if not audio_b64:
            raise RuntimeError(f"MiMo TTS 返回缺少 audio.data: {data}")
        return base64.b64decode(audio_b64), self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
