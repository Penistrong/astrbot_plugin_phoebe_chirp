"""菲比啾比语音插件。

思路：不动文字内容，只在 on_decorating_result 阶段往消息链尾部追加一段
固定文本（默认「菲比啾比」）的语音；音频按情绪缓存到本地，缺了再实时合成。

绑定人格：取当前会话生效的人格（会话规则优先，其次会话配置，最后默认人格），
与配置里的 persona_ids 比对，不在列表内就闭嘴。
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
import time
from pathlib import Path

import httpx

from astrbot.api import logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

PLUGIN_NAME = "astrbot_plugin_phoebe_chirp"

# 情绪 -> (指令, 关键词)。关键词只用于自动挑语调，命中越多越优先。
MOODS: dict[str, dict] = {
    "happy": {
        "instruction": "用非常开心、雀跃、忍不住笑出来的语气，语速稍快，尾音上扬",
        "keywords": ["哈哈", "嘿嘿", "太好了", "笑死", "绷不住", "开心", "行啊", "不错", "牛"],
    },
    "angry": {
        "instruction": "用生气、不耐烦、嫌弃的语气，咬字重一点，语速偏快",
        "keywords": ["无语", "别", "你行", "烦", "麻了", "？", "说什么", "滚", "够了"],
    },
    "sleepy": {
        "instruction": "用刚睡醒、困倦发软、有气无力的语气，语速很慢，尾音拖长",
        "keywords": ["睡", "困", "困了", "晚安", "躺", "缓", "歇"],
    },
    "sneer": {
        "instruction": "用嘲讽、慢悠悠拖长音的语气，带一点冷笑",
        "keywords": ["缅A", "哈", "典型", "中肯", "有意思", "你无敌了", "行吧", "彳亍"],
    },
    "shy": {
        "instruction": "用害羞、小声嘟囔的语气，音量压低，略带不情愿",
        "keywords": ["谢谢", "哼", "谁关心", "别谢", "不好意思", "才不是", "丢人"],
    },
    "surprise": {
        "instruction": "用惊讶、拔高音量、不敢相信的语气",
        "keywords": ["？", "我超", "真假的", "什么", "不可能", "神了", "草"],
    },
}


@register(
    PLUGIN_NAME,
    "菲比",
    "文字照常发，额外附带一句固定语音「菲比啾比」，可按人格绑定并自动切换情绪语调",
    "1.0.0",
)
class PhoebeChirpPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.voice_dir = self.data_dir / "voice"
        self.voice_dir.mkdir(parents=True, exist_ok=True)

        self._locks: dict[str, asyncio.Lock] = {}
        self._last_sent: dict[str, float] = {}
        self._skip_patterns: list[re.Pattern] = []
        self._compile_patterns()
        logger.info(
            f"[啾比语音] 已加载，情绪数 {len(MOODS)}，音频目录 {self.voice_dir}"
        )

    # ----------------------------- 配置辅助 -----------------------------
    def _compile_patterns(self) -> None:
        self._skip_patterns = []
        for raw in self.config.get("skip_patterns") or []:
            try:
                self._skip_patterns.append(re.compile(str(raw)))
            except re.error as e:
                logger.warning(f"[啾比语音] 跳过规则 {raw!r} 不是合法正则: {e}")

    def _voice_path(self, mood: str) -> Path:
        fmt = str(self.config.get("format", "wav") or "wav").lower()
        return self.voice_dir / f"{mood}.{fmt}"

    # ----------------------------- 人格匹配 -----------------------------
    async def _current_persona_name(self, event: AstrMessageEvent) -> str:
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        umo = str(event.unified_msg_origin)
        persona_id = ""
        try:
            session_cfg = (
                await sp.get_async(
                    scope="umo", scope_id=umo, key="session_service_config", default={}
                )
                or {}
            )
            persona_id = str(session_cfg.get("persona_id") or "")
        except Exception as e:  # 会话规则读不到就退回默认人格，不影响主流程
            logger.debug(f"[啾比语音] 读取会话人格失败: {e}")

        persona = None
        if persona_id:
            try:
                persona = pm.get_persona_v3_by_id(persona_id)
            except Exception:
                persona = None
        if not persona:
            try:
                persona = await pm.get_default_persona_v3(umo)
            except Exception:
                persona = None
        if not persona:
            return ""
        return str(persona.get("name") or "") if isinstance(persona, dict) else ""

    async def _persona_allowed(self, event: AstrMessageEvent) -> bool:
        wanted = [str(x).strip() for x in (self.config.get("persona_ids") or [])]
        wanted = [x for x in wanted if x]
        if not wanted:
            return True
        name = await self._current_persona_name(event)
        if not name:
            return False
        return name in wanted

    # ----------------------------- 情绪选择 -----------------------------
    def _pick_mood(self, text: str) -> str:
        if not self.config.get("auto_mood", True):
            fixed = str(self.config.get("fixed_mood", "random") or "random")
            return fixed if fixed in MOODS else random.choice(list(MOODS))

        scores: dict[str, int] = {}
        for mood, meta in MOODS.items():
            score = sum(1 for kw in meta["keywords"] if kw and kw in text)
            if score:
                scores[mood] = score
        if not scores:
            return random.choice(list(MOODS))
        top = max(scores.values())
        best = [m for m, s in scores.items() if s == top]
        return random.choice(best)

    # ----------------------------- 语音合成 -----------------------------
    async def _ensure_audio(self, mood: str) -> Path | None:
        path = self._voice_path(mood)
        if path.exists() and path.stat().st_size > 0:
            return path

        lock = self._locks.setdefault(mood, asyncio.Lock())
        async with lock:
            if path.exists() and path.stat().st_size > 0:
                return path
            try:
                audio = await self._synthesize(mood)
            except Exception as e:
                logger.error(f"[啾比语音] 合成 {mood} 失败: {type(e).__name__}: {e}")
                return None
            if not audio:
                logger.error(f"[啾比语音] 合成 {mood} 返回空音频")
                return None
            await asyncio.to_thread(path.write_bytes, audio)
            logger.info(f"[啾比语音] 已生成 {path.name} ({len(audio)} bytes)")
            return path

    async def _synthesize(self, mood: str) -> bytes:
        api_key = str(self.config.get("api_key", "") or "").strip()
        if not api_key:
            raise ValueError("未配置 api_key")

        api_base = str(
            self.config.get("api_base", "https://dashscope.aliyuncs.com/api/v1")
        ).rstrip("/")
        model = str(self.config.get("model", "qwen-audio-3.1-tts-flash"))
        voice = str(
            self.config.get("voice_id") or self.config.get("default_voice") or ""
        ).strip()
        if not voice:
            raise ValueError("voice_id 与 default_voice 均为空")

        payload = {
            "model": model,
            "input": {
                "text": str(self.config.get("chirp_text", "菲比啾比") or "菲比啾比"),
                "voice": voice,
                "format": str(self.config.get("format", "wav") or "wav"),
                "sample_rate": int(self.config.get("sample_rate", 24000) or 24000),
                "volume": int(self.config.get("volume", 50) or 50),
                "rate": float(self.config.get("speech_rate", 1.0) or 1.0),
                "pitch": float(self.config.get("pitch", 1.0) or 1.0),
                "instruction": MOODS[mood]["instruction"],
                "language_hints": ["zh"],
            },
        }

        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{api_base}/services/audio/tts/SpeechSynthesizer",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()

        output = (data or {}).get("output") or {}
        audio_obj = output.get("audio") if isinstance(output, dict) else None
        if not isinstance(audio_obj, dict):
            raise RuntimeError(f"响应缺少 output.audio: {str(data)[:200]}")

        b64 = audio_obj.get("data")
        if isinstance(b64, str) and b64.strip():
            return base64.b64decode(b64)

        url = (audio_obj.get("url") or "").strip()
        if not url:
            raise RuntimeError("响应既无 audio.data 也无 audio.url")
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            dl = await client.get(url)
            dl.raise_for_status()
            return dl.content

    # ----------------------------- 发送钩子 -----------------------------
    @filter.on_decorating_result(priority=0)
    async def chirp_hook(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enable", True):
            return

        result = event.get_result()
        if not result or not result.chain or not result.is_llm_result():
            return

        text = "".join(
            c.text for c in result.chain if isinstance(c, Plain) and c.text
        ).strip()
        if not text:
            return
        for pattern in self._skip_patterns:
            if pattern.search(text):
                return

        if not await self._persona_allowed(event):
            return

        probability = int(self.config.get("probability", 100) or 0)
        if probability <= 0:
            return
        if probability < 100 and random.randint(1, 100) > probability:
            return

        umo = str(event.unified_msg_origin)
        cooldown = int(self.config.get("cooldown_seconds", 0) or 0)
        now = time.time()
        if cooldown > 0 and now - self._last_sent.get(umo, 0) < cooldown:
            return

        path = await self._ensure_audio(self._pick_mood(text))
        if not path:
            return
        self._last_sent[umo] = now

        record = Record(file=str(path), url=str(path))
        if str(self.config.get("send_mode", "append")) == "separate":
            # 单独发一条，文字由框架照常发出（这里先发语音会抢在文字前面，故放到发完之后）
            async def _send_later() -> None:
                await asyncio.sleep(0.6)
                try:
                    await event.send(event.chain_result([Record(file=str(path), url=str(path))]))
                except Exception as e:
                    logger.warning(f"[啾比语音] 追加语音失败: {e}")

            asyncio.create_task(_send_later())
            return

        # append：文字在前，语音挂到同一条消息链尾部
        result.chain.append(record)

    # ----------------------------- 指令 -----------------------------
    @filter.command("啾比")
    async def chirp_cmd(self, event: AstrMessageEvent):
        """啾比 on/off/status/regen"""
        args = (getattr(event, "message_str", "") or "").split()
        sub = args[1].lower() if len(args) > 1 else "status"

        if sub in {"on", "off"}:
            enabled = sub == "on"
            self.config["enable"] = enabled
            self.config.save_config()
            yield event.plain_result(
                f"菲比啾比语音已{'开启' if enabled else '关闭'}。"
            )
            return

        if sub == "regen":
            removed = 0
            for mood in MOODS:
                path = self._voice_path(mood)
                if path.exists():
                    path.unlink()
                    removed += 1
            yield event.plain_result(
                f"已清掉 {removed} 条缓存音频，下次回复时重新合成。"
            )
            return

        persona = await self._current_persona_name(event)
        files = sorted(p.name for p in self.voice_dir.glob("*") if p.is_file())
        yield event.plain_result(
            "啾比语音 "
            f"{'开启' if self.config.get('enable', True) else '关闭'}｜"
            f"人格：{persona or '未知'}｜"
            f"触发概率 {self.config.get('probability', 100)}%｜"
            f"缓存 {len(files)} 条"
        )

    async def terminate(self) -> None:
        self._locks.clear()
        self._last_sent.clear()
        logger.info("[啾比语音] 已卸载")
