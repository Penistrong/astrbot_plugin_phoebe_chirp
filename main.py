"""菲比啾比角色情绪语音插件。

核心特性：
1. 模块化语音合成引擎（`engines/`）：支持阿里云百炼（含声音复刻 + 指令控制）、
   OpenAI 兼容接口、小米 MiMo、自定义 HTTP 模板，且可通过 `register_engine` 扩展。
2. 音色 - 情绪分类分级管理（`storage.py`）：
   - 按 `voices/<voice_id>/<mood_key>/*.wav` 分门别类存储；
   - 每个基础音色支持绑定多个人设（`persona_ids`）；
   - 每个情绪目录下可存放多条语音（支持本地上传或引擎生成），触发时随机挑选一条已启用语音发送。
3. 可视化 WebUI 管理页（`views/voices/`）：
   - 参考表情包管理器的分类卡片布局，支持按音色与情绪分类浏览、在线试听、
     上传新增、调用引擎生成新增、启用/禁用、跨情绪移动、删除以及人设多选绑定。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
import uuid
from pathlib import Path

from astrbot.api import logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .engines import ENGINE_META, ChirpTTSEngine, create_engine
from .storage import (
    ALLOWED_AUDIO_EXTS,
    DEFAULT_MOODS,
    VoiceStorageManager,
    validate_safe_id,
)

try:
    from astrbot.api.web import error_response, json_response, request
except Exception:  # pragma: no cover - 旧版本降级保护
    error_response = json_response = request = None

try:
    from astrbot.api.web import file_response
except Exception:  # pragma: no cover
    file_response = None

PLUGIN_NAME = "astrbot_plugin_phoebe_chirp"
GENERATE_JOB_TTL = 300
GENERATE_MAX_JOBS = 30
GENERATE_TIMEOUT = 90
UPLOAD_MAX_BYTES = 20 * 1024 * 1024

_AUDIO_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "aac": "audio/aac",
}


@register(
    PLUGIN_NAME,
    "菲比",
    "文字照常发，按回复蕴含的情绪附带角色口癖语音（如「菲比啾比」），支持多音色绑定多人设、多语音随机发送与 WebUI 管理",
    "2.0.0",
)
class PhoebeChirpPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        try:
            raw_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:
            raw_data_dir = Path("data") / "plugin_data" / PLUGIN_NAME
        self.data_dir = Path(raw_data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.storage = VoiceStorageManager(self.data_dir, self.config)
        self._locks: dict[str, asyncio.Lock] = {}
        self._engines: dict[str, ChirpTTSEngine] = {}
        self._last_sent: dict[str, float] = {}
        self._skip_patterns: list[re.Pattern] = []
        self._gen_jobs: dict[str, dict] = {}

        self._compile_patterns()
        self._register_web_apis()

        voices = self.storage.list_voices()
        total_clips = sum(v.get("total_clips", 0) for v in voices)
        logger.info(
            f"[啾比语音] 已加载：基础音色 {len(voices)} 个，语音片段共 {total_clips} 条，数据目录 {self.data_dir}"
        )

    # ------------------------------------------------------------------
    # 配置与引擎管理
    # ------------------------------------------------------------------

    def _compile_patterns(self) -> None:
        self._skip_patterns = []
        for raw in self.config.get("skip_patterns") or []:
            if not str(raw).strip():
                continue
            try:
                self._skip_patterns.append(re.compile(str(raw)))
            except re.error as e:
                logger.warning(f"[啾比语音] 跳过规则 {raw!r} 不是合法正则: {e}")

    async def _persist_config(self) -> None:
        save_async = getattr(self.config, "save_config_async", None)
        if callable(save_async):
            await save_async()
            return
        save_sync = getattr(self.config, "save_config", None)
        if callable(save_sync):
            save_sync()

    def _build_engine_config_for_voice(self, voice_profile: dict) -> tuple[str, dict]:
        """合并插件全局引擎参数与指定音色的专属引擎覆盖参数。"""
        v_engine_cfg = dict(voice_profile.get("engine_config") or {})
        engine_type = (
            str(
                voice_profile.get("engine_type")
                or self.config.get("engine_type")
                or "aliyun"
            )
            .strip()
            .lower()
            or "aliyun"
        )

        merged: dict = {
            "api_key": str(self.config.get("api_key") or "").strip(),
            "api_base": str(
                self.config.get("api_base")
                or "https://dashscope.aliyuncs.com/api/v1"
            ).strip(),
            "model": str(
                v_engine_cfg.get("model")
                or self.config.get("model")
                or "qwen-audio-3.1-tts-flash"
            ).strip(),
            "voice_id": str(
                v_engine_cfg.get("voice_id")
                or self.config.get("voice_id")
                or ""
            ).strip(),
            "default_voice": str(
                v_engine_cfg.get("default_voice")
                or self.config.get("default_voice")
                or "longanhuan_v3.1"
            ).strip(),
            "sample_name": str(v_engine_cfg.get("sample_name") or "").strip(),
            "sample_url": str(v_engine_cfg.get("sample_url") or "").strip(),
            "voice_prefix": str(
                v_engine_cfg.get("voice_prefix")
                or voice_profile.get("id")
                or "phoebe"
            ).strip(),
            "enable_preprocess": bool(v_engine_cfg.get("enable_preprocess", False)),
            "max_prompt_audio_length": float(
                v_engine_cfg.get("max_prompt_audio_length", 10.0) or 10.0
            ),
            "format": str(
                v_engine_cfg.get("format") or self.config.get("format") or "wav"
            )
            .strip()
            .lower(),
            "sample_rate": int(
                v_engine_cfg.get("sample_rate")
                or self.config.get("sample_rate")
                or 24000
            ),
            "volume": int(
                v_engine_cfg.get("volume")
                if v_engine_cfg.get("volume") is not None
                else (
                    self.config.get("volume")
                    if self.config.get("volume") is not None
                    else 50
                )
            ),
            "speech_rate": float(
                v_engine_cfg.get("speech_rate")
                if v_engine_cfg.get("speech_rate") is not None
                else (
                    self.config.get("speech_rate")
                    if self.config.get("speech_rate") is not None
                    else 1.0
                )
            ),
            "pitch": float(
                v_engine_cfg.get("pitch")
                if v_engine_cfg.get("pitch") is not None
                else (
                    self.config.get("pitch")
                    if self.config.get("pitch") is not None
                    else 1.0
                )
            ),
        }
        return engine_type, merged

    async def _close_cached_engines(self) -> None:
        engines = list(self._engines.values())
        self._engines.clear()
        for eng in engines:
            try:
                await eng.close()
            except Exception:
                pass

    def _get_engine_for_voice(self, voice_profile: dict) -> ChirpTTSEngine:
        vid = voice_profile["id"]
        engine_type, merged_cfg = self._build_engine_config_for_voice(voice_profile)
        cache_sig = f"{vid}:{engine_type}:{json.dumps(merged_cfg, sort_keys=True)}"
        cached = self._engines.get(cache_sig)
        if cached is not None:
            return cached
        engine = create_engine(engine_type, merged_cfg, data_dir=self.data_dir)
        self._engines[cache_sig] = engine
        return engine

    # ------------------------------------------------------------------
    # 人设解析与匹配
    # ------------------------------------------------------------------

    def _list_available_personas(self) -> list[str]:
        """从 AstrBot PersonaManager 读取当前系统中已创建的所有人设名称。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return []
        names: list[str] = []
        for item in getattr(pm, "personas_v3", []) or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        for item in getattr(pm, "personas", []) or []:
            pid = getattr(item, "persona_id", None)
            if pid:
                names.append(str(pid))
        return list(dict.fromkeys(names))

    async def _current_persona_name(self, event: AstrMessageEvent) -> str:
        """获取当前对话事件实际生效的人设名称。"""
        trace_pid = None
        if hasattr(event, "get_extra"):
            try:
                trace_pid = event.get_extra("_trace_persona_id")
            except Exception:
                trace_pid = None
        if trace_pid and isinstance(trace_pid, str) and trace_pid != "[%None]":
            return trace_pid

        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        persona_id = ""
        try:
            session_cfg = (
                await sp.get_async(
                    scope="umo",
                    scope_id=umo,
                    key="session_service_config",
                    default={},
                )
                or {}
            )
            persona_id = str(session_cfg.get("persona_id") or "")
        except Exception as e:
            logger.debug(f"[啾比语音] 读取会话人设失败: {e}")

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

    # ------------------------------------------------------------------
    # 语音合成与获取
    # ------------------------------------------------------------------

    async def synthesize_and_store_clip(
        self,
        voice_id: str,
        mood_key: str,
        *,
        custom_text: str = "",
        custom_instruction: str = "",
    ) -> dict:
        """调用指定音色对应的 TTS 引擎生成一条情绪语音并存入 `<voice_id>/<mood_key>/` 目录。"""
        voice_profile = self.storage.get_voice(voice_id)
        moods = voice_profile.get("moods") or {}
        if mood_key not in moods:
            default_m = DEFAULT_MOODS.get(mood_key, {})
            self.storage.upsert_mood(
                voice_id,
                mood_key,
                label=default_m.get("label", mood_key),
                instruction=default_m.get("instruction", ""),
                keywords=default_m.get("keywords", []),
            )
            voice_profile = self.storage.get_voice(voice_id)
            moods = voice_profile.get("moods") or {}

        mood_meta = moods[mood_key]
        text = (
            (custom_text or "").strip()
            or str(voice_profile.get("chirp_text") or "").strip()
            or str(self.config.get("chirp_text") or "菲比啾比").strip()
            or "菲比啾比"
        )
        instruction = (
            (custom_instruction or "").strip()
            or str(mood_meta.get("instruction") or "").strip()
        )

        engine = self._get_engine_for_voice(voice_profile)
        audio_bytes, fmt = await engine.synthesize(text, instruction=instruction)
        if not audio_bytes:
            raise RuntimeError(f"引擎合成返回了空音频 ({voice_id}/{mood_key})")

        clip_info = await asyncio.to_thread(
            self.storage.add_clip_bytes,
            voice_id,
            mood_key,
            audio_bytes,
            ext=fmt,
            source="engine",
            text=text,
            instruction=instruction,
            enabled=True,
        )
        logger.info(
            f"[啾比语音] 已生成语音 {voice_id}/{mood_key}/{clip_info['filename']} ({len(audio_bytes)} bytes)"
        )
        return clip_info

    async def _resolve_or_generate_clip(
        self, voice_id: str, mood_key: str
    ) -> Path | None:
        """从 `<voice_id>/<mood_key>/` 随机挑一条已启用语音；若目录为空且允许自动生成，则实时合成。"""
        picked, is_dir_empty = self.storage.pick_random_clip(voice_id, mood_key)
        if picked is not None:
            return picked
        if not is_dir_empty:
            # 目录里有语音文件，但全部被用户手动禁用了 -> 尊重禁用设置，不发送也不自动生成
            logger.debug(
                f"[啾比语音] 音色 {voice_id} 的情绪 {mood_key} 下所有语音均已禁用，跳过发送"
            )
            return None

        if not self.config.get("auto_generate_when_empty", True):
            return None

        lock_key = f"{voice_id}:{mood_key}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            picked, is_dir_empty = self.storage.pick_random_clip(voice_id, mood_key)
            if picked is not None:
                return picked
            if not is_dir_empty:
                return None
            try:
                clip = await self.synthesize_and_store_clip(voice_id, mood_key)
                return Path(clip["path"])
            except Exception as e:
                logger.error(
                    f"[啾比语音] 自动合成 {voice_id}/{mood_key} 失败: {type(e).__name__}: {e}"
                )
                return None

    # ------------------------------------------------------------------
    # 发送钩子
    # ------------------------------------------------------------------

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

        persona_name = await self._current_persona_name(event)
        matched_voice = self.storage.match_voice_for_persona(persona_name)
        if matched_voice is None:
            return

        probability = int(self.config.get("probability", 100) or 0)
        if probability <= 0:
            return
        if probability < 100 and random.randint(1, 100) > probability:
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        cooldown = int(self.config.get("cooldown_seconds", 0) or 0)
        now = time.time()
        if cooldown > 0 and now - self._last_sent.get(umo, 0) < cooldown:
            return

        voice_id = matched_voice["id"]
        mood_key = self.storage.pick_mood_for_text(
            voice_id,
            text,
            auto_mood=bool(self.config.get("auto_mood", True)),
            fixed_mood=str(self.config.get("fixed_mood", "random") or "random"),
        )
        if not mood_key:
            return

        clip_path = await self._resolve_or_generate_clip(voice_id, mood_key)
        if not clip_path:
            return

        self._last_sent[umo] = now
        path_str = str(clip_path)
        record = Record(file=path_str, url=path_str)
        send_mode = str(self.config.get("send_mode", "append") or "append").lower()

        if send_mode == "separate":
            async def _send_later() -> None:
                await asyncio.sleep(0.6)
                try:
                    await event.send(
                        event.chain_result([Record(file=path_str, url=path_str)])
                    )
                except Exception as e:
                    logger.warning(f"[啾比语音] 单独补发语音失败: {e}")

            asyncio.create_task(_send_later())
            return

        if send_mode == "prepend":
            result.chain.insert(0, record)
        else:
            result.chain.append(record)

    # ------------------------------------------------------------------
    # 聊天指令
    # ------------------------------------------------------------------

    @filter.command("啾比")
    async def chirp_cmd(self, event: AstrMessageEvent):
        """啾比 status / voices / gen [音色ID] [情绪] / on / off"""
        args = (getattr(event, "message_str", "") or "").split()
        sub = args[1].lower() if len(args) > 1 else "status"

        if sub in {"on", "off"}:
            enabled = sub == "on"
            self.config["enable"] = enabled
            await self._persist_config()
            yield event.plain_result(
                f"菲比啾比情绪语音已{'开启' if enabled else '关闭'}。"
            )
            return

        if sub == "voices":
            voices = self.storage.list_voices()
            lines = ["🎙️ 角色情绪音色包列表："]
            for v in voices:
                personas_str = "、".join(v.get("persona_ids") or []) or "全部人设(默认兜底)"
                default_tag = " [默认]" if v.get("is_default") else ""
                status_tag = "启用" if v.get("enabled", True) else "禁用"
                lines.append(
                    f"• {v['name']} ({v['id']}){default_tag} [{status_tag}]"
                    f"｜台词：「{v.get('chirp_text', '菲比啾比')}」"
                    f"｜绑定人设：{personas_str}"
                    f"｜语音：{v.get('enabled_clips', 0)}/{v.get('total_clips', 0)} 条"
                )
            yield event.plain_result("\n".join(lines))
            return

        if sub in {"gen", "regen"}:
            voices = self.storage.list_voices()
            target_vid = args[2] if len(args) > 2 else (voices[0]["id"] if voices else "phoebe")
            try:
                v_profile = self.storage.get_voice(target_vid)
            except KeyError:
                yield event.plain_result(f"未找到音色 ID: {target_vid}")
                return

            mood_keys = (
                [args[3]]
                if len(args) > 3
                else list((v_profile.get("moods") or {}).keys())
            )
            generated = 0
            failed: list[str] = []
            for mk in mood_keys:
                try:
                    await self.synthesize_and_store_clip(target_vid, mk)
                    generated += 1
                except Exception as e:
                    failed.append(f"{mk}: {e}")
            msg = f"已为音色「{v_profile['name']}」生成 {generated} 条情绪语音。"
            if failed:
                msg += "\n失败项：" + "；".join(failed[:3])
            yield event.plain_result(msg)
            return

        persona = await self._current_persona_name(event)
        matched = self.storage.match_voice_for_persona(persona)
        voices = self.storage.list_voices()
        total_clips = sum(v.get("total_clips", 0) for v in voices)
        enabled_clips = sum(v.get("enabled_clips", 0) for v in voices)
        yield event.plain_result(
            f"啾比语音：{'开启' if self.config.get('enable', True) else '关闭'}｜"
            f"当前人设：{persona or '默认'}｜"
            f"匹配音色：{matched['name'] if matched else '无匹配'}｜"
            f"触发概率：{self.config.get('probability', 100)}%｜"
            f"音色包：{len(voices)} 个（启用语音 {enabled_clips}/{total_clips} 条）"
        )

    # ------------------------------------------------------------------
    # WebUI API 注册与处理
    # ------------------------------------------------------------------

    def _register_web_apis(self) -> None:
        register_fn = getattr(self.context, "register_web_api", None)
        if not callable(register_fn) or json_response is None:
            return

        routes = [
            ("overview", self.web_get_overview, ["GET"], "获取音色列表、情绪分类与全局概览"),
            ("voices/create", self.web_create_voice, ["POST"], "新建基础音色"),
            ("voices/update", self.web_update_voice, ["POST"], "更新基础音色配置与人设绑定"),
            ("voices/delete", self.web_delete_voice, ["POST"], "删除基础音色"),
            ("moods/save", self.web_save_mood, ["POST"], "新建或更新音色下的情绪分类"),
            ("moods/delete", self.web_delete_mood, ["POST"], "删除情绪分类及其语音"),
            ("clips", self.web_list_clips, ["GET"], "列出指定音色下的情绪语音列表"),
            ("clips/audio", self.web_get_clip_audio, ["GET"], "获取单个语音的试听 Data URI"),
            ("clips/download", self.web_download_clip, ["GET"], "下载单个语音文件"),
            ("clips/upload", self.web_upload_clip, ["POST"], "上传语音文件到指定音色的情绪目录"),
            ("clips/generate", self.web_generate_clips, ["POST"], "提交引擎合成情绪语音任务"),
            ("clips/generate/result", self.web_generate_result, ["GET"], "查询引擎合成任务结果"),
            ("clips/toggle", self.web_toggle_clip, ["POST"], "启用或禁用单条语音"),
            ("clips/batch_toggle", self.web_batch_toggle_clips, ["POST"], "批量启用或禁用语音"),
            ("clips/move", self.web_move_clip, ["POST"], "将语音移动到另一情绪分类"),
            ("clips/delete", self.web_delete_clip, ["POST"], "删除单条语音"),
            ("clips/batch_delete", self.web_batch_delete_clips, ["POST"], "批量删除语音"),
            ("samples", self.web_list_samples, ["GET"], "列出克隆参考音频样本"),
            ("samples/upload", self.web_upload_sample, ["POST"], "上传克隆参考音频样本"),
            ("samples/delete", self.web_delete_sample, ["POST"], "删除克隆参考音频样本"),
            ("samples/audio", self.web_get_sample_audio, ["GET"], "试听克隆参考音频样本"),
            ("settings/save", self.web_save_settings, ["POST"], "保存全局设置与默认引擎参数"),
        ]
        for ep, handler, methods, desc in routes:
            register_fn(f"/{PLUGIN_NAME}/{ep}", handler, methods, desc)

    def _build_overview_payload(self, selected_voice_id: str = "") -> dict:
        voices = self.storage.list_voices()
        if not selected_voice_id and voices:
            default_v = next((v for v in voices if v.get("is_default")), voices[0])
            selected_voice_id = default_v["id"]

        clips: list[dict] = []
        if selected_voice_id:
            try:
                clips = self.storage.list_clips(selected_voice_id)
            except KeyError:
                if voices:
                    selected_voice_id = voices[0]["id"]
                    clips = self.storage.list_clips(selected_voice_id)

        api_key_val = str(self.config.get("api_key") or "").strip()
        settings = {
            "enable": bool(self.config.get("enable", True)),
            "chirp_text": str(self.config.get("chirp_text") or "菲比啾比"),
            "probability": int(self.config.get("probability", 100) or 0),
            "cooldown_seconds": int(self.config.get("cooldown_seconds", 0) or 0),
            "send_mode": str(self.config.get("send_mode") or "append"),
            "auto_mood": bool(self.config.get("auto_mood", True)),
            "fixed_mood": str(self.config.get("fixed_mood") or "random"),
            "auto_generate_when_empty": bool(
                self.config.get("auto_generate_when_empty", True)
            ),
            "skip_patterns": list(self.config.get("skip_patterns") or []),
            "engine_type": str(self.config.get("engine_type") or "aliyun"),
            "api_key": "",
            "api_key_configured": bool(api_key_val),
            "api_base": str(
                self.config.get("api_base")
                or "https://dashscope.aliyuncs.com/api/v1"
            ),
            "model": str(self.config.get("model") or "qwen-audio-3.1-tts-flash"),
            "voice_id": str(self.config.get("voice_id") or ""),
            "default_voice": str(
                self.config.get("default_voice") or "longanhuan_v3.1"
            ),
            "format": str(self.config.get("format") or "wav"),
            "sample_rate": int(self.config.get("sample_rate", 24000) or 24000),
            "volume": int(
                self.config.get("volume", 50)
                if self.config.get("volume") is not None
                else 50
            ),
            "speech_rate": float(
                self.config.get("speech_rate", 1.0)
                if self.config.get("speech_rate") is not None
                else 1.0
            ),
            "pitch": float(
                self.config.get("pitch", 1.0)
                if self.config.get("pitch") is not None
                else 1.0
            ),
        }

        return {
            "voices": voices,
            "selected_voice_id": selected_voice_id,
            "clips": clips,
            "samples": self.storage.list_samples(),
            "personas": self._list_available_personas(),
            "engines": ENGINE_META,
            "settings": settings,
        }

    async def web_get_overview(self):
        vid = str((request.query or {}).get("voice_id") or "").strip()
        return json_response(self._build_overview_payload(vid))

    async def web_create_voice(self):
        payload = await request.json(default={}) or {}
        try:
            voice = self.storage.create_voice(
                voice_id=str(payload.get("id") or "").strip(),
                name=str(payload.get("name") or "").strip(),
                chirp_text=str(payload.get("chirp_text") or "菲比啾比").strip(),
                persona_ids=payload.get("persona_ids") or [],
                is_default=bool(payload.get("is_default", False)),
                enabled=bool(payload.get("enabled", True)),
                engine_type=str(payload.get("engine_type") or "aliyun").strip(),
                engine_config=payload.get("engine_config") or {},
            )
            await self._close_cached_engines()
            return json_response(self._build_overview_payload(voice["id"]))
        except Exception as e:
            return error_response(str(e))

    async def web_update_voice(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("id") or payload.get("voice_id") or "").strip()
        if not vid:
            return error_response("缺少音色 ID")
        try:
            voice = self.storage.update_voice(vid, payload)
            await self._close_cached_engines()
            return json_response(self._build_overview_payload(voice["id"]))
        except Exception as e:
            return error_response(str(e))

    async def web_delete_voice(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("id") or payload.get("voice_id") or "").strip()
        if not vid:
            return error_response("缺少音色 ID")
        try:
            self.storage.delete_voice(vid)
            await self._close_cached_engines()
            return json_response(self._build_overview_payload())
        except Exception as e:
            return error_response(str(e))

    async def web_save_mood(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        mk = str(payload.get("mood") or payload.get("key") or "").strip()
        if not vid or not mk:
            return error_response("缺少 voice_id 或情绪分类标识")
        try:
            self.storage.upsert_mood(
                vid,
                mk,
                label=str(payload.get("label") or "").strip(),
                instruction=str(payload.get("instruction") or ""),
                keywords=payload.get("keywords"),
                enabled=payload.get("enabled"),
            )
            return json_response(self._build_overview_payload(vid))
        except Exception as e:
            return error_response(str(e))

    async def web_delete_mood(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        mk = str(payload.get("mood") or payload.get("key") or "").strip()
        if not vid or not mk:
            return error_response("缺少 voice_id 或情绪分类标识")
        try:
            self.storage.delete_mood(vid, mk)
            return json_response(self._build_overview_payload(vid))
        except Exception as e:
            return error_response(str(e))

    async def web_list_clips(self):
        query = request.query or {}
        vid = str(query.get("voice_id") or "").strip()
        mk = str(query.get("mood") or "").strip() or None
        if not vid:
            return error_response("缺少 voice_id")
        try:
            clips = self.storage.list_clips(vid, mk)
            voice = self.storage.get_voice(vid)
            return json_response({"voice": voice, "clips": clips})
        except Exception as e:
            return error_response(str(e))

    async def web_get_clip_audio(self):
        query = request.query or {}
        vid = str(query.get("voice_id") or "").strip()
        mk = str(query.get("mood") or "").strip()
        fname = str(query.get("filename") or "").strip()
        if not vid or not mk or not fname:
            return error_response("缺少 voice_id、mood 或 filename 参数")
        try:
            fpath = self.storage.get_clip_path(vid, mk, fname)
            raw = await asyncio.to_thread(fpath.read_bytes)
            fmt = fpath.suffix.lower().lstrip(".")
            mime = _AUDIO_MIME.get(fmt, "audio/wav")
            b64 = base64.b64encode(raw).decode("ascii")
            return json_response(
                {
                    "audio": f"data:{mime};base64,{b64}",
                    "format": fmt,
                    "filename": fpath.name,
                    "bytes": len(raw),
                }
            )
        except Exception as e:
            return error_response(str(e))

    async def web_download_clip(self):
        if file_response is None:
            return error_response("当前 AstrBot 版本不支持文件下载")
        query = request.query or {}
        vid = str(query.get("voice_id") or "").strip()
        mk = str(query.get("mood") or "").strip()
        fname = str(query.get("filename") or "").strip()
        try:
            fpath = self.storage.get_clip_path(vid, mk, fname)
            fmt = fpath.suffix.lower().lstrip(".")
            mime = _AUDIO_MIME.get(fmt, "application/octet-stream")
            return file_response(str(fpath), filename=fpath.name, content_type=mime)
        except Exception as e:
            return error_response(str(e))

    async def web_upload_clip(self):
        """上传音频到指定音色与情绪目录。

        支持两种调用方式：
        1. `bridge.upload("clips/upload", file)`（文件名可编码为 `voiceId__moodKey__origName.wav`）
        2. `bridge.apiPost("clips/upload", {"voice_id": ..., "mood": ..., "filename": ..., "data_base64": ...})`
        """
        vid = ""
        mk = ""
        orig_name = ""
        raw_bytes = b""

        try:
            files = await request.files()
        except Exception:
            files = {}

        upload = files.get("file") if files else None
        if upload is not None:
            raw_fname = str(upload.filename or "upload.wav")
            # 解析前端编码在 fileName 中的 voice_id 与 mood
            if "__" in raw_fname:
                parts = raw_fname.split("__", 2)
                if len(parts) == 3:
                    vid, mk, orig_name = parts[0], parts[1], parts[2]
            if not vid:
                vid = str((request.query or {}).get("voice_id") or "phoebe")
            if not mk:
                mk = str((request.query or {}).get("mood") or "happy")
            if not orig_name:
                orig_name = raw_fname
            raw_bytes = await upload.read()
        else:
            payload = await request.json(default={}) or {}
            vid = str(payload.get("voice_id") or "").strip()
            mk = str(payload.get("mood") or "").strip()
            orig_name = str(payload.get("filename") or "upload.wav").strip()
            b64_str = str(payload.get("data_base64") or "").strip()
            if "," in b64_str and b64_str.startswith("data:"):
                b64_str = b64_str.split(",", 1)[1]
            if b64_str:
                try:
                    raw_bytes = base64.b64decode(b64_str)
                except Exception as e:
                    return error_response(f"Base64 音频解码失败: {e}")

        if not vid or not mk:
            return error_response("缺少目标音色 ID 或情绪分类")
        if not raw_bytes:
            return error_response("未收到有效的音频文件数据")
        if len(raw_bytes) > UPLOAD_MAX_BYTES:
            return error_response(f"音频文件超过上限 ({UPLOAD_MAX_BYTES // (1024 * 1024)} MB)")

        ext = Path(orig_name).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTS:
            return error_response(
                f"不支持的音频格式 '{ext or '(无)'}'，仅支持: {', '.join(sorted(ALLOWED_AUDIO_EXTS))}"
            )

        try:
            clip = await asyncio.to_thread(
                self.storage.add_clip_bytes,
                vid,
                mk,
                raw_bytes,
                ext=ext,
                filename=orig_name,
                source="upload",
                enabled=True,
            )
            return json_response(
                {
                    "clip": clip,
                    "overview": self._build_overview_payload(vid),
                }
            )
        except Exception as e:
            return error_response(str(e))

    def _prune_gen_jobs(self) -> None:
        now = time.time()
        for k in [
            jid
            for jid, j in self._gen_jobs.items()
            if now - j.get("created", now) > GENERATE_JOB_TTL
        ]:
            self._gen_jobs.pop(k, None)
        while len(self._gen_jobs) >= GENERATE_MAX_JOBS:
            oldest = min(self._gen_jobs, key=lambda k: self._gen_jobs[k].get("created", 0))
            self._gen_jobs.pop(oldest, None)

    async def web_generate_clips(self):
        """提交后台引擎生成语音任务（支持单情绪生成 N 条或一键补齐全部情绪）。"""
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        if not vid:
            return error_response("缺少 voice_id")
        try:
            voice_profile = self.storage.get_voice(vid)
        except KeyError as e:
            return error_response(str(e))

        mood_param = str(payload.get("mood") or "").strip()
        count = max(1, min(int(payload.get("count", 1) or 1), 5))
        custom_text = str(payload.get("text") or "").strip()
        custom_instruction = str(payload.get("instruction") or "").strip()

        if mood_param and mood_param != "__all__":
            try:
                validate_safe_id(mood_param, "情绪标识")
            except ValueError as e:
                return error_response(str(e))
            tasks_list = [(mood_param, count)]
        else:
            moods = voice_profile.get("moods") or {}
            tasks_list = [(mk, 1) for mk, m in moods.items() if m.get("enabled", True)]
            if not tasks_list:
                return error_response("该音色下没有已启用的情绪分类")

        self._prune_gen_jobs()
        job_id = uuid.uuid4().hex
        total_items = sum(c for _, c in tasks_list)
        self._gen_jobs[job_id] = {
            "status": "pending",
            "created": time.time(),
            "voice_id": vid,
            "total": total_items,
            "completed": 0,
            "generated": [],
            "errors": [],
        }
        asyncio.create_task(
            self._run_generate_job(
                job_id,
                vid,
                tasks_list,
                custom_text=custom_text,
                custom_instruction=custom_instruction,
            )
        )
        return json_response({"job_id": job_id, "total": total_items})

    async def _run_generate_job(
        self,
        job_id: str,
        voice_id: str,
        tasks_list: list[tuple[str, int]],
        *,
        custom_text: str = "",
        custom_instruction: str = "",
    ) -> None:
        job = self._gen_jobs.get(job_id)
        if not job:
            return
        started = time.monotonic()
        for mk, cnt in tasks_list:
            for _ in range(cnt):
                try:
                    clip = await asyncio.wait_for(
                        self.synthesize_and_store_clip(
                            voice_id,
                            mk,
                            custom_text=custom_text,
                            custom_instruction=custom_instruction,
                        ),
                        timeout=GENERATE_TIMEOUT,
                    )
                    job["generated"].append(clip)
                except asyncio.TimeoutError:
                    job["errors"].append(f"[{mk}] 合成超时（>{GENERATE_TIMEOUT}s）")
                except Exception as e:
                    logger.warning(f"[啾比语音] 引擎合成失败 ({voice_id}/{mk}): {e}")
                    job["errors"].append(f"[{mk}] {e}")
                finally:
                    job["completed"] += 1

        job["took_ms"] = int((time.monotonic() - started) * 1000)
        if job["generated"]:
            job["status"] = "done"
        else:
            job["status"] = "error"
            job["error"] = "；".join(job["errors"]) or "生成失败"

    async def web_generate_result(self):
        job_id = str((request.query or {}).get("job_id") or "").strip()
        if not job_id:
            return error_response("缺少 job_id")
        job = self._gen_jobs.get(job_id)
        if not job:
            return error_response("任务不存在或已过期")
        res = dict(job)
        if res.get("status") in {"done", "error"}:
            res["overview"] = self._build_overview_payload(job["voice_id"])
        return json_response(res)

    async def web_toggle_clip(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        mk = str(payload.get("mood") or "").strip()
        fname = str(payload.get("filename") or "").strip()
        enabled = bool(payload.get("enabled", True))
        if not vid or not mk or not fname:
            return error_response("缺少 voice_id、mood 或 filename")
        try:
            self.storage.set_clip_enabled(vid, mk, fname, enabled)
            return json_response(self._build_overview_payload(vid))
        except Exception as e:
            return error_response(str(e))

    async def web_batch_toggle_clips(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        clip_ids = payload.get("clip_ids") or []
        enabled = bool(payload.get("enabled", True))
        if not vid or not isinstance(clip_ids, list):
            return error_response("参数错误")
        try:
            updated = self.storage.batch_set_clips_enabled(vid, clip_ids, enabled)
            data = self._build_overview_payload(vid)
            data["updated"] = updated
            return json_response(data)
        except Exception as e:
            return error_response(str(e))

    async def web_move_clip(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        from_mood = str(payload.get("from_mood") or "").strip()
        to_mood = str(payload.get("to_mood") or "").strip()
        fname = str(payload.get("filename") or "").strip()
        if not vid or not from_mood or not to_mood or not fname:
            return error_response("缺少移动参数")
        try:
            self.storage.move_clip(vid, from_mood, fname, to_mood)
            return json_response(self._build_overview_payload(vid))
        except Exception as e:
            return error_response(str(e))

    async def web_delete_clip(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        mk = str(payload.get("mood") or "").strip()
        fname = str(payload.get("filename") or "").strip()
        if not vid or not mk or not fname:
            return error_response("缺少删除参数")
        try:
            self.storage.delete_clip(vid, mk, fname)
            return json_response(self._build_overview_payload(vid))
        except Exception as e:
            return error_response(str(e))

    async def web_batch_delete_clips(self):
        payload = await request.json(default={}) or {}
        vid = str(payload.get("voice_id") or "").strip()
        clip_ids = payload.get("clip_ids") or []
        if not vid or not isinstance(clip_ids, list):
            return error_response("参数错误")
        try:
            deleted = self.storage.batch_delete_clips(vid, clip_ids)
            data = self._build_overview_payload(vid)
            data["deleted"] = deleted
            return json_response(data)
        except Exception as e:
            return error_response(str(e))

    async def web_list_samples(self):
        return json_response({"samples": self.storage.list_samples()})

    async def web_upload_sample(self):
        filename = ""
        raw_bytes = b""
        try:
            files = await request.files()
        except Exception:
            files = {}
        upload = files.get("file") if files else None
        if upload is not None:
            filename = str(upload.filename or "sample.wav")
            raw_bytes = await upload.read()
        else:
            payload = await request.json(default={}) or {}
            filename = str(payload.get("filename") or "sample.wav")
            b64_str = str(payload.get("data_base64") or "").strip()
            if "," in b64_str and b64_str.startswith("data:"):
                b64_str = b64_str.split(",", 1)[1]
            if b64_str:
                raw_bytes = base64.b64decode(b64_str)

        if not raw_bytes:
            return error_response("未收到样本音频数据")
        if len(raw_bytes) > UPLOAD_MAX_BYTES:
            return error_response("样本文件过大（上限 20MB）")
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTS:
            return error_response(f"不支持的音频格式: {ext}")

        try:
            sample = await asyncio.to_thread(
                self.storage.save_sample, filename, raw_bytes
            )
            return json_response(
                {
                    "sample": sample,
                    "samples": self.storage.list_samples(),
                }
            )
        except Exception as e:
            return error_response(str(e))

    async def web_delete_sample(self):
        payload = await request.json(default={}) or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("缺少样本文件名")
        try:
            self.storage.delete_sample(name)
            return json_response({"samples": self.storage.list_samples()})
        except Exception as e:
            return error_response(str(e))

    async def web_get_sample_audio(self):
        name = str((request.query or {}).get("name") or "").strip()
        if not name:
            return error_response("缺少样本文件名")
        safe_name = Path(name.replace("\\", "/")).name
        fpath = self.storage.samples_dir / safe_name
        if not fpath.is_file():
            return error_response(f"样本不存在: {safe_name}")
        raw = await asyncio.to_thread(fpath.read_bytes)
        fmt = fpath.suffix.lower().lstrip(".")
        mime = _AUDIO_MIME.get(fmt, "audio/wav")
        b64 = base64.b64encode(raw).decode("ascii")
        return json_response(
            {
                "audio": f"data:{mime};base64,{b64}",
                "format": fmt,
                "name": safe_name,
                "bytes": len(raw),
            }
        )

    async def web_save_settings(self):
        payload = await request.json(default={}) or {}
        values = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        if not isinstance(values, dict):
            return error_response("请求格式错误")

        try:
            if "enable" in values:
                self.config["enable"] = bool(values["enable"])
            if "chirp_text" in values and str(values["chirp_text"]).strip():
                self.config["chirp_text"] = str(values["chirp_text"]).strip()
            if "probability" in values:
                self.config["probability"] = max(0, min(100, int(values["probability"])))
            if "cooldown_seconds" in values:
                self.config["cooldown_seconds"] = max(0, int(values["cooldown_seconds"]))
            if "send_mode" in values and values["send_mode"] in {
                "append",
                "prepend",
                "separate",
            }:
                self.config["send_mode"] = str(values["send_mode"])
            if "auto_mood" in values:
                self.config["auto_mood"] = bool(values["auto_mood"])
            if "fixed_mood" in values:
                self.config["fixed_mood"] = str(values["fixed_mood"]).strip() or "random"
            if "auto_generate_when_empty" in values:
                self.config["auto_generate_when_empty"] = bool(
                    values["auto_generate_when_empty"]
                )
            if "skip_patterns" in values:
                raw_pats = values["skip_patterns"]
                if isinstance(raw_pats, str):
                    raw_pats = [x.strip() for x in raw_pats.splitlines() if x.strip()]
                elif isinstance(raw_pats, list):
                    raw_pats = [str(x).strip() for x in raw_pats if str(x).strip()]
                else:
                    raw_pats = []
                self.config["skip_patterns"] = raw_pats
                self._compile_patterns()

            if "engine_type" in values:
                self.config["engine_type"] = str(values["engine_type"]).strip() or "aliyun"
            # api_key 留空表示保持原密钥不修改
            if "api_key" in values and str(values["api_key"]).strip():
                self.config["api_key"] = str(values["api_key"]).strip()
            if "api_base" in values and str(values["api_base"]).strip():
                self.config["api_base"] = str(values["api_base"]).strip()
            if "model" in values and str(values["model"]).strip():
                self.config["model"] = str(values["model"]).strip()
            if "voice_id" in values:
                self.config["voice_id"] = str(values["voice_id"]).strip()
            if "default_voice" in values:
                self.config["default_voice"] = str(values["default_voice"]).strip()
            if "format" in values and values["format"] in {"wav", "mp3"}:
                self.config["format"] = str(values["format"])
            if "sample_rate" in values:
                self.config["sample_rate"] = int(values["sample_rate"] or 24000)
            if "volume" in values:
                self.config["volume"] = max(0, min(100, int(values["volume"])))
            if "speech_rate" in values:
                self.config["speech_rate"] = float(values["speech_rate"] or 1.0)
            if "pitch" in values:
                self.config["pitch"] = float(values["pitch"] or 1.0)

            await self._persist_config()
            await self._close_cached_engines()
            return json_response(self._build_overview_payload())
        except Exception as e:
            return error_response(f"保存设置失败: {e}")

    async def terminate(self) -> None:
        await self._close_cached_engines()
        self._locks.clear()
        self._last_sent.clear()
        logger.info("[啾比语音] 已卸载")
