"""OpenAI 兼容 (/v1/audio/speech) 语音合成引擎"""

from __future__ import annotations

from pathlib import Path

import httpx

from .base import ChirpTTSEngine


class OpenAICompatChirpEngine(ChirpTTSEngine):
    """OpenAI 兼容接口引擎。

    支持标准 `/v1/audio/speech` 接口，并在提供情绪 `instruction` 时通过
    `instructions` 字段透传（适用于 `gpt-4o-mini-tts` 及兼容网关）。
    """

    engine_type = "openai_compat"
    DEFAULT_API_BASE = "https://api.openai.com/v1"

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
        self.model: str = str(self.config.get("model") or "gpt-4o-mini-tts").strip()
        self.voice_id: str = str(
            self.config.get("voice_id")
            or self.config.get("default_voice")
            or "alloy"
        ).strip()
        self.audio_format: str = (
            str(self.config.get("format") or "wav").strip().lower().lstrip(".")
        )
        self.speech_rate: float = float(
            self.config.get("speech_rate", 1.0)
            if self.config.get("speech_rate") is not None
            else 1.0
        )
        self.timeout: int = int(self.config.get("timeout", 60) or 60)
        self._client: httpx.AsyncClient | None = None

        if not self.api_key:
            raise ValueError("OpenAI 兼容 TTS: 未配置 api_key")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str = "",
    ) -> tuple[bytes, str]:
        client = self._get_client()
        url = f"{self.api_base}/audio/speech"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self.model,
            "input": text,
            "voice": (voice or "").strip() or self.voice_id or "alloy",
            "response_format": self.audio_format,
            "speed": self.speech_rate,
        }
        if instruction and instruction.strip():
            payload["instructions"] = instruction.strip()

        resp = await client.post(url, headers=headers, json=payload)
        self.check_http_response("OpenAI 兼容", resp)
        return resp.content, self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
