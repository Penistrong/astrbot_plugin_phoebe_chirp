"""阿里云百炼 (DashScope) 语音合成与声音复刻引擎"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path

import httpx
from astrbot.api import logger

from .base import ChirpTTSEngine

_PREFIX_CLEAN_RE = re.compile(r"[^a-zA-Z0-9]")


class AliyunChirpEngine(ChirpTTSEngine):
    """阿里云百炼语音合成引擎。

    支持：
    1. Qwen-Audio-TTS (`qwen-audio-3.1-tts-flash` / `qwen-audio-3.1-tts-plus`)
       与 CosyVoice (`cosyvoice-v3-flash` / `cosyvoice-v3.5-flash` / `cosyvoice-v2`)
       的 `/services/audio/tts/SpeechSynthesizer` 接口（支持 `instruction` 语气指令控制）。
    2. Qwen3-TTS (`qwen3-tts-flash` / `qwen3-tts-vc-*`) 的
       `/services/aigc/multimodal-generation/generation` 接口。
    3. 零样本声音复刻 (`/services/audio/tts/customization`)：
       支持通过上传的本地参考音频 (`sample_name`) 或公网音频 URL (`sample_url`)
       自动创建绑定 `model` 的专属复刻音色，轮询等待审核通过 (`OK`) 后持久化缓存到
       `aliyun_cloned_voices.json`，避免重复消耗百炼账号的 1000 个音色配额。
    """

    engine_type = "aliyun"
    DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
    POLL_INTERVAL = 1.0
    POLL_MAX_ATTEMPTS = 30

    def __init__(
        self,
        config: dict,
        *,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(config, data_dir=data_dir)
        self.api_key: str = str(self.config.get("api_key") or "").strip()
        self.api_base: str = self._normalize_api_base(
            str(self.config.get("api_base") or self.DEFAULT_API_BASE)
        )
        self.model: str = str(
            self.config.get("model") or "qwen-audio-3.1-tts-flash"
        ).strip()
        self.voice_id: str = str(self.config.get("voice_id") or "").strip()
        self.default_voice: str = str(
            self.config.get("default_voice") or "longanhuan_v3.1"
        ).strip()
        self.sample_name: str = str(self.config.get("sample_name") or "").strip()
        self.sample_url: str = str(self.config.get("sample_url") or "").strip()
        self.voice_prefix: str = self._sanitize_prefix(
            str(self.config.get("voice_prefix") or "phoebe")
        )
        self.enable_preprocess: bool = bool(
            self.config.get("enable_preprocess", False)
        )
        self.max_prompt_audio_length: float = float(
            self.config.get("max_prompt_audio_length", 10.0) or 10.0
        )
        self.audio_format: str = (
            str(self.config.get("format") or "wav").strip().lower().lstrip(".")
        )
        if self.audio_format not in {"wav", "mp3", "pcm", "opus"}:
            self.audio_format = "wav"
        self.sample_rate: int = int(self.config.get("sample_rate", 24000) or 24000)
        self.volume: int = int(
            self.config.get("volume", 50)
            if self.config.get("volume") is not None
            else 50
        )
        self.speech_rate: float = float(
            self.config.get("speech_rate", 1.0)
            if self.config.get("speech_rate") is not None
            else 1.0
        )
        self.pitch: float = float(
            self.config.get("pitch", 1.0)
            if self.config.get("pitch") is not None
            else 1.0
        )
        self.language_hint: str = str(
            self.config.get("language_hint") or "zh"
        ).strip()
        self.timeout: int = int(self.config.get("timeout", 60) or 60)

        self._client: httpx.AsyncClient | None = None
        self._enrolled_voice_id: str | None = None
        self._enroll_lock = asyncio.Lock()

        if not self.api_key:
            raise ValueError("阿里云百炼 TTS: 未配置 api_key")

    @classmethod
    def _normalize_api_base(cls, raw_base: str) -> str:
        base = (raw_base or "").strip().rstrip("/")
        if not base:
            return cls.DEFAULT_API_BASE
        if base.endswith("/compatible-mode/v1"):
            return base[: -len("/compatible-mode/v1")] + "/api/v1"
        return base

    @staticmethod
    def _sanitize_prefix(prefix: str) -> str:
        cleaned = _PREFIX_CLEAN_RE.sub("", (prefix or "").strip())[:10]
        return cleaned or "phoebe"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    @staticmethod
    def _is_multimodal_qwen_tts(model: str) -> bool:
        m = (model or "").strip().lower()
        return m.startswith("qwen-tts") or m.startswith("qwen3-tts")

    @staticmethod
    def _is_qwen_voice_enrollment_model(model: str) -> bool:
        m = (model or "").strip().lower()
        return m.startswith("qwen3-tts-vc") or "omni" in m

    def _cache_file(self) -> Path:
        return self.data_dir / "aliyun_cloned_voices.json"

    def _load_clone_cache(self) -> dict[str, dict]:
        path = self._cache_file()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_clone_cache(self, cache: dict[str, dict]) -> None:
        path = self._cache_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[啾比语音] 保存百炼复刻音色缓存失败: {e}")

    def _get_clone_audio_source(self) -> tuple[str, str]:
        """获取复刻音频输入及唯一指纹 (audio_input, audio_hash)。"""
        if self.sample_url:
            digest = hashlib.sha256(self.sample_url.encode("utf-8")).hexdigest()[:16]
            return self.sample_url, f"url:{digest}"
        sample_path = self.resolve_sample_path(self.sample_name)
        raw_bytes = sample_path.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
        data_uri = self.encode_audio_data_uri(sample_path, with_prefix=True)
        return data_uri, f"file:{sample_path.name}:{digest}"

    def _build_cache_key(self, audio_hash: str) -> str:
        key_hash = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:8]
        raw = (
            f"{self.api_base}|{key_hash}|{self.model}|{self.voice_prefix}|"
            f"{audio_hash}|prep={int(self.enable_preprocess)}|"
            f"len={self.max_prompt_audio_length}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    async def _enroll_voice(self, audio_input: str) -> str:
        client = self._get_client()
        url = f"{self.api_base}/services/audio/tts/customization"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if self._is_qwen_voice_enrollment_model(self.model):
            create_payload = {
                "model": "qwen-voice-enrollment",
                "input": {
                    "action": "create",
                    "target_model": self.model,
                    "preferred_name": self.voice_prefix,
                    "audio": {"data": audio_input},
                },
            }
            resp = await client.post(url, headers=headers, json=create_payload)
            self.check_http_response("阿里云百炼声音复刻", resp)
            data = resp.json()
            if data.get("code"):
                raise RuntimeError(
                    f"阿里云百炼声音复刻错误 [{data.get('code')}]: {data.get('message', '')}"
                )
            output = data.get("output") or {}
            voice_id = output.get("voice") or output.get("voice_id")
            if not voice_id:
                raise RuntimeError(f"阿里云百炼声音复刻未返回 voice: {data}")
            return str(voice_id)

        input_obj: dict = {
            "action": "create_voice",
            "target_model": self.model,
            "prefix": self.voice_prefix,
            "audio": audio_input,
            "url": audio_input,
        }
        if self.language_hint:
            input_obj["language_hints"] = [self.language_hint]
        if self.max_prompt_audio_length:
            input_obj["max_prompt_audio_length"] = self.max_prompt_audio_length
        if self.enable_preprocess:
            input_obj["enable_preprocess"] = True

        resp = await client.post(
            url,
            headers=headers,
            json={"model": "voice-enrollment", "input": input_obj},
        )
        self.check_http_response("阿里云百炼声音复刻", resp)
        data = resp.json()
        if data.get("code"):
            raise RuntimeError(
                f"阿里云百炼声音复刻错误 [{data.get('code')}]: {data.get('message', '')}"
            )
        voice_id = (data.get("output") or {}).get("voice_id")
        if not voice_id:
            raise RuntimeError(f"阿里云百炼声音复刻未返回 voice_id: {data}")

        # 轮询状态直到 OK
        query_payload = {
            "model": "voice-enrollment",
            "input": {"action": "query_voice", "voice_id": voice_id},
        }
        for _ in range(self.POLL_MAX_ATTEMPTS):
            q_resp = await client.post(url, headers=headers, json=query_payload)
            self.check_http_response("阿里云百炼音色状态查询", q_resp)
            q_data = q_resp.json()
            status = (q_data.get("output") or {}).get("status", "OK")
            if status == "OK":
                break
            if status and status != "DEPLOYING":
                raise RuntimeError(
                    f"阿里云百炼复刻音色状态异常 (status={status}, voice_id={voice_id})"
                )
            await asyncio.sleep(self.POLL_INTERVAL)
        else:
            raise RuntimeError(f"阿里云百炼复刻音色等待审核超时 (voice_id={voice_id})")

        return str(voice_id)

    async def resolve_voice(self, override_voice: str = "") -> str:
        """按优先级解析最终用于合成的百炼音色 ID：
        1. 调用时显式传入的 override_voice
        2. 配置的专属/复刻 voice_id
        3. 若配置了克隆样本 (sample_url / sample_name)，自动复刻或读取持久化缓存
        4. 回退到系统默认音色 default_voice
        """
        if override_voice and override_voice.strip():
            return override_voice.strip()
        if self.voice_id:
            return self.voice_id

        if self.sample_url or self.sample_name:
            if self._enrolled_voice_id:
                return self._enrolled_voice_id
            async with self._enroll_lock:
                if self._enrolled_voice_id:
                    return self._enrolled_voice_id
                audio_input, audio_hash = self._get_clone_audio_source()
                ckey = self._build_cache_key(audio_hash)
                cache = self._load_clone_cache()
                cached_entry = cache.get(ckey)
                if isinstance(cached_entry, dict):
                    cached_vid = str(cached_entry.get("voice_id") or "").strip()
                    if cached_vid:
                        self._enrolled_voice_id = cached_vid
                        return cached_vid

                logger.info(
                    f"[啾比语音] 正在为模型 {self.model} 复刻新音色 (前缀: {self.voice_prefix})..."
                )
                vid = await self._enroll_voice(audio_input)
                self._enrolled_voice_id = vid
                cache[ckey] = {
                    "voice_id": vid,
                    "target_model": self.model,
                    "prefix": self.voice_prefix,
                    "audio_hash": audio_hash,
                }
                self._save_clone_cache(cache)
                logger.info(f"[啾比语音] 阿里云百炼音色复刻成功: {vid}")
                return vid

        if self.default_voice:
            return self.default_voice
        raise ValueError("阿里云百炼 TTS: 未配置 voice_id、克隆样本或 default_voice")

    async def _extract_audio_bytes(
        self, client: httpx.AsyncClient, response: httpx.Response
    ) -> bytes:
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type.startswith("audio/") or content_type == "application/octet-stream":
            return response.content

        data = response.json()
        if isinstance(data, dict) and data.get("code"):
            raise RuntimeError(
                f"阿里云百炼业务错误 [{data.get('code')}]: {data.get('message', '')}"
            )

        output = (data or {}).get("output") or {}
        audio_obj = output.get("audio") if isinstance(output, dict) else None
        if not isinstance(audio_obj, dict):
            raise RuntimeError(f"响应缺少 output.audio: {str(data)[:200]}")

        b64 = audio_obj.get("data")
        if isinstance(b64, str) and b64.strip():
            return base64.b64decode(b64)

        url = (audio_obj.get("url") or "").strip()
        if not url:
            raise RuntimeError(f"响应既无 audio.data 也无 audio.url: {str(data)[:200]}")
        dl = await client.get(url)
        self.check_http_response("阿里云百炼音频下载", dl)
        return dl.content

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str = "",
    ) -> tuple[bytes, str]:
        client = self._get_client()
        resolved_voice = await self.resolve_voice(voice)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if self._is_multimodal_qwen_tts(self.model):
            url = f"{self.api_base}/services/aigc/multimodal-generation/generation"
            input_obj: dict = {
                "text": text,
                "voice": resolved_voice,
            }
            if self.language_hint:
                input_obj["language_type"] = self.language_hint
            payload = {"model": self.model, "input": input_obj}
        else:
            url = f"{self.api_base}/services/audio/tts/SpeechSynthesizer"
            input_obj = {
                "text": text,
                "voice": resolved_voice,
                "format": self.audio_format,
                "sample_rate": self.sample_rate,
                "volume": self.volume,
                "rate": self.speech_rate,
                "pitch": self.pitch,
            }
            if instruction and instruction.strip():
                input_obj["instruction"] = instruction.strip()
            if self.language_hint:
                input_obj["language_hints"] = [self.language_hint]
            payload = {"model": self.model, "input": input_obj}

        resp = await client.post(url, headers=headers, json=payload)
        self.check_http_response("阿里云百炼", resp)
        audio_bytes = await self._extract_audio_bytes(client, resp)
        return audio_bytes, self.audio_format

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
