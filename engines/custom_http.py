"""自定义 HTTP 模板语音合成引擎"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx

from .base import ChirpTTSEngine

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


class CustomHTTPChirpEngine(ChirpTTSEngine):
    """自定义 HTTP 模板引擎，用于快速扩展接入任意第三方 TTS API。

    支持的模板占位符：
    - `${TEXT}` / `${TEXT_JSON}`：待合成台词文本
    - `${INSTRUCTION}` / `${INSTRUCTION_JSON}`：情绪语气指令
    - `${VOICE}`：音色 ID
    - `${API_KEY}`：API 密钥
    - `${VOICE_SAMPLE_BASE64}`：克隆样本 Data URI
    - `${VOICE_SAMPLE_BASE64_RAW}`：克隆样本纯 Base64
    """

    engine_type = "custom_http"

    def __init__(
        self,
        config: dict,
        *,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(config, data_dir=data_dir)
        self.url: str = str(
            self.config.get("url") or self.config.get("api_base") or ""
        ).strip()
        self.method: str = str(self.config.get("method") or "POST").strip().upper()
        self.headers_raw: str = str(
            self.config.get("headers")
            or "Authorization: Bearer ${API_KEY}\nContent-Type: application/json"
        )
        self.body_template: str = str(
            self.config.get("body_template")
            or '{"text": ${TEXT_JSON}, "voice": "${VOICE}", "instruction": ${INSTRUCTION_JSON}}'
        )
        self.response_type: str = str(
            self.config.get("response_type") or "binary"
        ).strip().lower()
        self.response_audio_path: str = str(
            self.config.get("response_audio_path") or ""
        ).strip()
        self.response_audio_encoding: str = str(
            self.config.get("response_audio_encoding") or "raw"
        ).strip().lower()
        self.audio_format: str = (
            str(self.config.get("format") or "wav").strip().lower().lstrip(".")
        )
        self.api_key: str = str(self.config.get("api_key") or "").strip()
        self.voice_id: str = str(
            self.config.get("voice_id") or self.config.get("default_voice") or "default"
        ).strip()
        self.sample_name: str = str(self.config.get("sample_name") or "").strip()
        self.timeout: int = int(self.config.get("timeout", 60) or 60)
        self._client: httpx.AsyncClient | None = None

        if not self.url:
            raise ValueError("自定义 HTTP TTS: 未配置请求 URL")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    def _substitute(self, template: str, text: str, instruction: str, voice: str) -> str:
        def _repl(match: re.Match) -> str:
            key = match.group(1)
            if key == "TEXT":
                return text
            if key == "TEXT_JSON":
                return json.dumps(text, ensure_ascii=False)
            if key == "INSTRUCTION":
                return instruction
            if key == "INSTRUCTION_JSON":
                return json.dumps(instruction, ensure_ascii=False)
            if key == "VOICE":
                return voice or self.voice_id
            if key == "API_KEY":
                return self.api_key
            if key == "VOICE_SAMPLE_BASE64":
                path = self.resolve_sample_path(self.sample_name)
                return self.encode_audio_data_uri(path, with_prefix=True)
            if key == "VOICE_SAMPLE_BASE64_RAW":
                path = self.resolve_sample_path(self.sample_name)
                return self.encode_audio_data_uri(path, with_prefix=False)
            return match.group(0)

        return _PLACEHOLDER_RE.sub(_repl, template)

    @staticmethod
    def _extract_json_path(data, dotted_path: str):
        cur = data
        for part in [p for p in dotted_path.split(".") if p]:
            if isinstance(cur, list):
                cur = cur[int(part)]
            elif isinstance(cur, dict):
                cur = cur[part]
            else:
                raise KeyError(f"路径 {dotted_path} 在 {part} 处不可索引")
        return cur

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str = "",
    ) -> tuple[bytes, str]:
        client = self._get_client()
        resolved_voice = (voice or "").strip() or self.voice_id
        url = self._substitute(self.url, text, instruction, resolved_voice)

        headers: dict[str, str] = {}
        for line in self.headers_raw.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                if k.strip():
                    headers[k.strip()] = self._substitute(
                        v.strip(), text, instruction, resolved_voice
                    )

        if self.method == "GET":
            resp = await client.get(url, headers=headers)
        else:
            body = self._substitute(
                self.body_template, text, instruction, resolved_voice
            )
            headers.setdefault("Content-Type", "application/json")
            resp = await client.post(url, headers=headers, content=body.encode("utf-8"))

        self.check_http_response("自定义 HTTP", resp)

        if self.response_type == "json":
            data = resp.json()
            val = self._extract_json_path(data, self.response_audio_path)
            if self.response_audio_encoding == "base64":
                return base64.b64decode(str(val)), self.audio_format
            if isinstance(val, str) and val.startswith(("http://", "https://")):
                dl = await client.get(val)
                self.check_http_response("自定义 HTTP 音频下载", dl)
                return dl.content, self.audio_format
            return str(val).encode("utf-8"), self.audio_format

        if self.response_audio_encoding == "base64":
            return base64.b64decode(resp.content), self.audio_format
        return resp.content, self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
