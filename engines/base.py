"""菲比啾比语音合成引擎抽象基类"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from pathlib import Path

import httpx


class ChirpTTSEngine(ABC):
    """语音合成引擎抽象基类。

    所有具体 TTS 引擎均需继承此类并实现 `synthesize` 方法。
    """

    engine_type: str = "base"

    def __init__(
        self,
        config: dict,
        *,
        data_dir: Path | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.data_dir = data_dir or (
            Path("data") / "plugin_data" / "astrbot_plugin_phoebe_chirp"
        )

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
        voice: str = "",
    ) -> tuple[bytes, str]:
        """将文本合成为指定情绪语气的语音。

        Args:
            text: 待合成的台词文本（如「菲比啾比」）。
            instruction: 情绪/语气自然语言指令（如「用非常开心、雀跃的语气」）。
            voice: 可选的音色标识覆盖。

        Returns:
            tuple[bytes, str]: (音频二进制数据, 音频格式扩展名如 "wav" 或 "mp3")。
        """
        ...

    async def close(self) -> None:
        """释放底层连接资源，子类可按需重写。"""
        pass

    @staticmethod
    def check_http_response(provider_name: str, response: httpx.Response) -> None:
        """检查 HTTP 响应状态码，失败时抛出可读异常。"""
        if response.status_code >= 400:
            detail = response.text[:300]
            raise RuntimeError(
                f"{provider_name} TTS 请求失败 [HTTP {response.status_code}]: {detail}"
            )

    def resolve_sample_path(self, sample_name: str) -> Path:
        """在插件 samples 目录或配置路径中解析克隆参考音频文件。"""
        clean = (sample_name or "").strip()
        if not clean:
            raise FileNotFoundError("未指定用于声音复刻的样本音频文件")

        samples_dir = self.data_dir / "samples"
        candidates = [
            samples_dir / Path(clean).name,
            self.data_dir / clean,
            Path(clean),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate

        raise FileNotFoundError(
            f"未找到音色样本文件 '{clean}'，请先在 WebUI「合成引擎与克隆样本」页上传音频样本。"
        )

    @staticmethod
    def encode_audio_data_uri(file_path: Path, with_prefix: bool = True) -> str:
        """将本地音频文件编码为 Base64 或 Data URI。"""
        raw = file_path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        if not with_prefix:
            return b64
        mime_map = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
            ".flac": "audio/flac",
        }
        mime = mime_map.get(file_path.suffix.lower(), "audio/wav")
        return f"data:{mime};base64,{b64}"
