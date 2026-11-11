"""音色 - 情绪分类 - 语音片段 分级存储与状态管理模块"""

from __future__ import annotations

import copy
import json
import random
import re
import shutil
import time
import uuid
from pathlib import Path

from astrbot.api import logger

ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".opus", ".m4a", ".flac", ".aac"}
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,47}$")

# 默认内置的 6 种情绪分类
DEFAULT_MOODS: dict[str, dict] = {
    "happy": {
        "label": "开心",
        "enabled": True,
        "instruction": "用非常开心、雀跃、忍不住笑出来的语气，语速稍快，尾音上扬",
        "keywords": ["哈哈", "嘿嘿", "太好了", "笑死", "绷不住", "开心", "行啊", "不错", "牛", "耶"],
    },
    "angry": {
        "label": "生气",
        "enabled": True,
        "instruction": "用生气、不耐烦、嫌弃的语气，咬字重一点，语速偏快",
        "keywords": ["无语", "别", "你行", "烦", "麻了", "说什么", "滚", "够了", "气死"],
    },
    "sleepy": {
        "label": "困倦",
        "enabled": True,
        "instruction": "用刚睡醒、困倦发软、有气无力的语气，语速很慢，尾音拖长",
        "keywords": ["睡", "困", "困了", "晚安", "躺", "缓", "歇", "哈欠", "累"],
    },
    "sneer": {
        "label": "嘲讽",
        "enabled": True,
        "instruction": "用嘲讽、慢悠悠拖长音的语气，带一点冷笑",
        "keywords": ["缅A", "哈", "典型", "中肯", "有意思", "你无敌了", "行吧", "彳亍", "就这"],
    },
    "shy": {
        "label": "害羞",
        "enabled": True,
        "instruction": "用害羞、小声嘟囔的语气，音量压低，略带不情愿",
        "keywords": ["谢谢", "哼", "谁关心", "别谢", "不好意思", "才不是", "丢人", "呜"],
    },
    "surprise": {
        "label": "惊讶",
        "enabled": True,
        "instruction": "用惊讶、拔高音量、不敢相信的语气",
        "keywords": ["？", "我超", "真假的", "什么", "不可能", "神了", "草", "居然", "天哪"],
    },
}


def validate_safe_id(raw_id: str, field_name: str = "ID") -> str:
    """校验音色 ID 或情绪分类标识是否安全合法（防止路径穿越）。"""
    val = (raw_id or "").strip()
    if not val or not _SAFE_ID_RE.match(val) or ".." in val:
        raise ValueError(
            f"{field_name} '{raw_id}' 不合法：仅允许字母、数字、下划线、短横线和点，且长度不超过 48"
        )
    return val


def sanitize_filename(raw_name: str, default_ext: str = ".wav") -> str:
    """清洗音频文件名，确保无路径穿越并保留合法扩展名。"""
    clean = Path(str(raw_name or "").replace("\\", "/")).name.strip()
    if not clean or clean in {".", ".."}:
        clean = f"chirp_{int(time.time() * 1000)}{default_ext}"
    ext = Path(clean).suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTS:
        ext = default_ext if default_ext.startswith(".") else f".{default_ext}"
        stem = Path(clean).stem or "chirp"
    else:
        stem = Path(clean).stem or "chirp"
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._")[:80] or "chirp"
    return f"{safe_stem}{ext}"


class VoiceStorageManager:
    """管理所有基础音色、情绪分类目录、多语音条目以及克隆参考样本。

    目录结构：
    data_dir/
    ├── voices_registry.json
    ├── samples/
    │   └── *.wav / *.mp3
    └── voices/
        └── <voice_id>/
            └── <mood_key>/
                ├── happy_001.wav
                ├── happy_002.wav
                └── ...
    """

    def __init__(self, data_dir: Path, plugin_config: dict | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.plugin_config = plugin_config if plugin_config is not None else {}
        self.voices_dir = self.data_dir / "voices"
        self.samples_dir = self.data_dir / "samples"
        self.legacy_voice_dir = self.data_dir / "voice"
        self.registry_file = self.data_dir / "voices_registry.json"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        self.samples_dir.mkdir(parents=True, exist_ok=True)

        self._voices: dict[str, dict] = {}
        self._load_or_init()

    # ------------------------------------------------------------------
    # 初始化、持久化与旧版自动迁移
    # ------------------------------------------------------------------

    def _load_or_init(self) -> None:
        loaded = False
        if self.registry_file.exists():
            try:
                raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("voices"), dict):
                    self._voices = {}
                    for vid, vdata in raw["voices"].items():
                        if isinstance(vdata, dict):
                            self._voices[vid] = self._normalize_voice_dict(vid, vdata)
                    loaded = bool(self._voices)
            except Exception as e:
                logger.warning(f"[啾比语音] 读取 voices_registry.json 失败，将重建: {e}")

        if not loaded:
            self._init_default_voice_and_migrate()

        # 扫描 voices/ 下的物理目录，确保手动放入的文件夹和音频同步进注册表
        self.sync_filesystem()

    def _normalize_voice_dict(self, voice_id: str, data: dict) -> dict:
        vid = validate_safe_id(voice_id, "音色 ID")
        raw_moods = data.get("moods")
        moods: dict[str, dict] = {}
        if isinstance(raw_moods, dict) and raw_moods:
            for mk, mv in raw_moods.items():
                if not isinstance(mv, dict):
                    continue
                try:
                    safe_mk = validate_safe_id(mk, "情绪标识")
                except ValueError:
                    continue
                default_m = DEFAULT_MOODS.get(safe_mk, {})
                moods[safe_mk] = {
                    "label": str(mv.get("label") or default_m.get("label") or safe_mk),
                    "enabled": bool(mv.get("enabled", True)),
                    "instruction": str(
                        mv.get("instruction") or default_m.get("instruction") or ""
                    ),
                    "keywords": [
                        str(k).strip()
                        for k in (mv.get("keywords") or default_m.get("keywords") or [])
                        if str(k).strip()
                    ],
                }
        else:
            moods = copy.deepcopy(DEFAULT_MOODS)

        persona_ids = [
            str(p).strip()
            for p in (data.get("persona_ids") or [])
            if str(p).strip()
        ]
        # 去重保持顺序
        persona_ids = list(dict.fromkeys(persona_ids))

        engine_cfg = data.get("engine_config")
        if not isinstance(engine_cfg, dict):
            engine_cfg = {}

        clips = data.get("clips")
        if not isinstance(clips, dict):
            clips = {}

        return {
            "id": vid,
            "name": str(data.get("name") or vid).strip() or vid,
            "enabled": bool(data.get("enabled", True)),
            "is_default": bool(data.get("is_default", False)),
            "chirp_text": str(data.get("chirp_text") or "菲比啾比").strip() or "菲比啾比",
            "persona_ids": persona_ids,
            "engine_type": str(data.get("engine_type") or "aliyun").strip().lower()
            or "aliyun",
            "engine_config": {
                "voice_id": str(engine_cfg.get("voice_id") or "").strip(),
                "default_voice": str(engine_cfg.get("default_voice") or "").strip(),
                "sample_name": str(engine_cfg.get("sample_name") or "").strip(),
                "sample_url": str(engine_cfg.get("sample_url") or "").strip(),
                "voice_prefix": str(engine_cfg.get("voice_prefix") or vid[:10]).strip(),
                "enable_preprocess": bool(engine_cfg.get("enable_preprocess", False)),
                "max_prompt_audio_length": float(
                    engine_cfg.get("max_prompt_audio_length", 10.0) or 10.0
                ),
                "model": str(engine_cfg.get("model") or "").strip(),
                "format": str(engine_cfg.get("format") or "").strip(),
                "sample_rate": int(engine_cfg.get("sample_rate", 0) or 0),
                "volume": engine_cfg.get("volume"),
                "speech_rate": engine_cfg.get("speech_rate"),
                "pitch": engine_cfg.get("pitch"),
            },
            "moods": moods,
            "clips": clips,
        }

    def _init_default_voice_and_migrate(self) -> None:
        """创建默认「菲比啾比」音色，并自动迁移 v1.0 旧版 voice/*.wav 文件。"""
        cfg = self.plugin_config or {}
        persona_ids = [
            str(x).strip()
            for x in (cfg.get("persona_ids") or [])
            if str(x).strip()
        ]
        default_voice_profile = self._normalize_voice_dict(
            "phoebe",
            {
                "id": "phoebe",
                "name": "菲比啾比",
                "enabled": True,
                "is_default": True,
                "chirp_text": str(cfg.get("chirp_text") or "菲比啾比"),
                "persona_ids": persona_ids,
                "engine_type": str(cfg.get("engine_type") or "aliyun"),
                "engine_config": {
                    "voice_id": str(cfg.get("voice_id") or "").strip(),
                    "default_voice": str(
                        cfg.get("default_voice") or "longanhuan_v3.1"
                    ).strip(),
                    "sample_name": str(cfg.get("sample_name") or "").strip(),
                    "sample_url": str(cfg.get("sample_url") or "").strip(),
                    "voice_prefix": "phoebe",
                },
                "moods": copy.deepcopy(DEFAULT_MOODS),
                "clips": {},
            },
        )
        self._voices["phoebe"] = default_voice_profile

        # 为默认音色创建各情绪目录
        for mood_key in default_voice_profile["moods"]:
            (self.voices_dir / "phoebe" / mood_key).mkdir(parents=True, exist_ok=True)

        # 迁移旧版 data_dir/voice/<mood>.wav -> data_dir/voices/phoebe/<mood>/<mood>_001.wav
        if self.legacy_voice_dir.is_dir():
            for old_file in sorted(self.legacy_voice_dir.iterdir()):
                if (
                    not old_file.is_file()
                    or old_file.suffix.lower() not in ALLOWED_AUDIO_EXTS
                ):
                    continue
                mood_stem = old_file.stem.lower()
                if mood_stem not in default_voice_profile["moods"]:
                    try:
                        safe_m = validate_safe_id(mood_stem, "情绪标识")
                    except ValueError:
                        continue
                    default_voice_profile["moods"][safe_m] = {
                        "label": safe_m,
                        "enabled": True,
                        "instruction": f"用{safe_m}的语气说",
                        "keywords": [],
                    }
                    mood_stem = safe_m
                target_dir = self.voices_dir / "phoebe" / mood_stem
                target_dir.mkdir(parents=True, exist_ok=True)
                target_name = f"{mood_stem}_001{old_file.suffix.lower()}"
                target_path = target_dir / target_name
                if not target_path.exists():
                    try:
                        shutil.copy2(old_file, target_path)
                        rel_key = f"{mood_stem}/{target_name}"
                        default_voice_profile["clips"][rel_key] = {
                            "enabled": True,
                            "text": default_voice_profile["chirp_text"],
                            "instruction": default_voice_profile["moods"][mood_stem].get(
                                "instruction", ""
                            ),
                            "source": "migrated",
                            "created_at": old_file.stat().st_mtime,
                        }
                        logger.info(
                            f"[啾比语音] 已自动迁移旧版语音 {old_file.name} -> voices/phoebe/{rel_key}"
                        )
                    except Exception as e:
                        logger.warning(f"[啾比语音] 迁移旧语音 {old_file} 失败: {e}")

        self.save_registry()

    def save_registry(self) -> None:
        payload = {
            "version": 2,
            "updated_at": time.time(),
            "voices": self._voices,
        }
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.registry_file.with_suffix(".json.tmp")
        tmp_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_file.replace(self.registry_file)

    def sync_filesystem(self) -> None:
        """扫描磁盘 `voices/` 目录，同步音色目录、情绪子目录及音频文件元信息。"""
        changed = False

        # 1. 若磁盘上存在未登记的音色文件夹，自动纳入管理
        if self.voices_dir.is_dir():
            for vdir in sorted(self.voices_dir.iterdir()):
                if not vdir.is_dir() or vdir.name.startswith("."):
                    continue
                try:
                    vid = validate_safe_id(vdir.name, "音色 ID")
                except ValueError:
                    continue
                if vid not in self._voices:
                    self._voices[vid] = self._normalize_voice_dict(
                        vid,
                        {
                            "id": vid,
                            "name": vid,
                            "enabled": True,
                            "is_default": len(self._voices) == 0,
                        },
                    )
                    changed = True

        # 2. 确保至少有一个默认音色
        if self._voices and not any(v.get("is_default") for v in self._voices.values()):
            first_vid = next(iter(self._voices))
            self._voices[first_vid]["is_default"] = True
            changed = True

        # 3. 同步每个音色下的情绪目录和音频文件
        for vid, vdata in self._voices.items():
            vroot = self.voices_dir / vid
            vroot.mkdir(parents=True, exist_ok=True)
            moods: dict[str, dict] = vdata["moods"]
            clips: dict[str, dict] = vdata["clips"]

            # 确保已注册情绪的物理文件夹存在
            for mk in moods:
                (vroot / mk).mkdir(parents=True, exist_ok=True)

            # 发现物理文件夹中新增的情绪目录
            for mdir in sorted(vroot.iterdir()):
                if not mdir.is_dir() or mdir.name.startswith("."):
                    continue
                try:
                    mk = validate_safe_id(mdir.name, "情绪标识")
                except ValueError:
                    continue
                if mk not in moods:
                    default_m = DEFAULT_MOODS.get(mk, {})
                    moods[mk] = {
                        "label": default_m.get("label", mk),
                        "enabled": True,
                        "instruction": default_m.get("instruction", ""),
                        "keywords": list(default_m.get("keywords", [])),
                    }
                    changed = True

                # 扫描情绪目录下的音频文件
                for fpath in sorted(mdir.iterdir()):
                    if (
                        not fpath.is_file()
                        or fpath.name.startswith(".")
                        or fpath.suffix.lower() not in ALLOWED_AUDIO_EXTS
                    ):
                        continue
                    rel_key = f"{mk}/{fpath.name}"
                    if rel_key not in clips:
                        clips[rel_key] = {
                            "enabled": True,
                            "text": vdata.get("chirp_text", "菲比啾比"),
                            "instruction": moods[mk].get("instruction", ""),
                            "source": "local",
                            "created_at": fpath.stat().st_mtime,
                        }
                        changed = True

            # 清理已被从磁盘删除的音频条目
            stale_keys = [
                rel_key
                for rel_key in list(clips.keys())
                if not (vroot / rel_key).is_file()
            ]
            for rk in stale_keys:
                clips.pop(rk, None)
                changed = True

        if changed:
            self.save_registry()

    # ------------------------------------------------------------------
    # 音色 (Voice Profile) 管理
    # ------------------------------------------------------------------

    def list_voices(self) -> list[dict]:
        """列出所有基础音色及其统计摘要。"""
        self.sync_filesystem()
        result: list[dict] = []
        for vid, vdata in self._voices.items():
            clips = vdata.get("clips") or {}
            total_clips = len(clips)
            enabled_clips = sum(
                1
                for meta in clips.values()
                if isinstance(meta, dict) and meta.get("enabled", True)
            )
            mood_stats: dict[str, dict] = {}
            for mk, mmeta in (vdata.get("moods") or {}).items():
                m_clips = [
                    meta
                    for rk, meta in clips.items()
                    if rk.startswith(f"{mk}/") and isinstance(meta, dict)
                ]
                mood_stats[mk] = {
                    **copy.deepcopy(mmeta),
                    "key": mk,
                    "total_clips": len(m_clips),
                    "enabled_clips": sum(
                        1 for c in m_clips if c.get("enabled", True)
                    ),
                }
            item = copy.deepcopy(vdata)
            item["total_clips"] = total_clips
            item["enabled_clips"] = enabled_clips
            item["mood_count"] = len(vdata.get("moods") or {})
            item["mood_stats"] = mood_stats
            result.append(item)
        return result

    def get_voice(self, voice_id: str) -> dict:
        vid = validate_safe_id(voice_id, "音色 ID")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        for v in self.list_voices():
            if v["id"] == vid:
                return v
        return copy.deepcopy(self._voices[vid])

    def create_voice(
        self,
        *,
        voice_id: str = "",
        name: str,
        chirp_text: str = "菲比啾比",
        persona_ids: list[str] | None = None,
        is_default: bool = False,
        enabled: bool = True,
        engine_type: str = "aliyun",
        engine_config: dict | None = None,
    ) -> dict:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("音色名称不能为空")

        if not (voice_id or "").strip():
            # 自动生成合法的 voice_id
            slug = re.sub(r"[^a-zA-Z0-9_-]", "", clean_name.lower())[:20]
            base_id = slug or f"voice_{uuid.uuid4().hex[:6]}"
            candidate = base_id
            seq = 2
            while candidate in self._voices:
                candidate = f"{base_id}_{seq}"
                seq += 1
            vid = candidate
        else:
            vid = validate_safe_id(voice_id, "音色 ID")
            if vid in self._voices:
                raise ValueError(f"音色 ID '{vid}' 已存在")

        if is_default:
            for existing in self._voices.values():
                existing["is_default"] = False

        profile = self._normalize_voice_dict(
            vid,
            {
                "id": vid,
                "name": clean_name,
                "enabled": enabled,
                "is_default": is_default or len(self._voices) == 0,
                "chirp_text": chirp_text,
                "persona_ids": persona_ids or [],
                "engine_type": engine_type,
                "engine_config": engine_config or {},
                "moods": copy.deepcopy(DEFAULT_MOODS),
                "clips": {},
            },
        )
        self._voices[vid] = profile
        for mk in profile["moods"]:
            (self.voices_dir / vid / mk).mkdir(parents=True, exist_ok=True)
        self.save_registry()
        return self.get_voice(vid)

    def update_voice(self, voice_id: str, updates: dict) -> dict:
        vid = validate_safe_id(voice_id, "音色 ID")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        current = self._voices[vid]

        if "name" in updates:
            name = str(updates["name"] or "").strip()
            if not name:
                raise ValueError("音色名称不能为空")
            current["name"] = name

        if "enabled" in updates:
            current["enabled"] = bool(updates["enabled"])

        if "is_default" in updates and bool(updates["is_default"]):
            for v in self._voices.values():
                v["is_default"] = False
            current["is_default"] = True

        if "chirp_text" in updates:
            ct = str(updates["chirp_text"] or "").strip()
            if ct:
                current["chirp_text"] = ct

        if "persona_ids" in updates:
            raw_personas = updates["persona_ids"]
            if isinstance(raw_personas, str):
                raw_personas = [
                    x.strip()
                    for x in re.split(r"[,，\n]", raw_personas)
                    if x.strip()
                ]
            elif isinstance(raw_personas, list):
                raw_personas = [str(x).strip() for x in raw_personas if str(x).strip()]
            else:
                raw_personas = []
            current["persona_ids"] = list(dict.fromkeys(raw_personas))

        if "engine_type" in updates:
            et = str(updates["engine_type"] or "aliyun").strip().lower()
            if et:
                current["engine_type"] = et

        if "engine_config" in updates and isinstance(updates["engine_config"], dict):
            merged_ec = dict(current.get("engine_config") or {})
            merged_ec.update(updates["engine_config"])
            current["engine_config"] = self._normalize_voice_dict(
                vid, {**current, "engine_config": merged_ec}
            )["engine_config"]

        self.save_registry()
        return self.get_voice(vid)

    def delete_voice(self, voice_id: str) -> None:
        vid = validate_safe_id(voice_id, "音色 ID")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        if len(self._voices) <= 1:
            raise ValueError("至少需要保留一个基础音色，无法删除最后一个音色")

        was_default = bool(self._voices[vid].get("is_default"))
        self._voices.pop(vid, None)
        vroot = self.voices_dir / vid
        if vroot.is_dir():
            shutil.rmtree(vroot, ignore_errors=True)

        if was_default and self._voices:
            first_vid = next(iter(self._voices))
            self._voices[first_vid]["is_default"] = True

        self.save_registry()

    def match_voice_for_persona(self, persona_name: str) -> dict | None:
        """根据当前会话生效的人设名称匹配对应的基础音色。

        匹配规则：
        1. 优先匹配 `enabled=True` 且 `persona_ids` 中包含 `persona_name` 的音色（若有多个匹配，默认音色优先）。
        2. 若无显式绑定该人设的音色：
           查找 `enabled=True` 且 `is_default=True` 的默认音色；
           只有当该默认音色的 `persona_ids` 为空（表示不限人设）时才回退使用它，
           否则返回 None（不发出语音）。
        """
        clean_persona = (persona_name or "").strip()
        enabled_voices = [
            v for v in self._voices.values() if v.get("enabled", True)
        ]
        if not enabled_voices:
            return None

        if clean_persona:
            explicit_matches = [
                v
                for v in enabled_voices
                if clean_persona in (v.get("persona_ids") or [])
            ]
            if explicit_matches:
                for v in explicit_matches:
                    if v.get("is_default"):
                        return v
                return explicit_matches[0]

        # 回退检查默认音色是否允许全部人设（即 persona_ids 为空）
        default_voices = [v for v in enabled_voices if v.get("is_default")]
        fallback = default_voices[0] if default_voices else enabled_voices[0]
        if not (fallback.get("persona_ids") or []):
            return fallback

        return None

    # ------------------------------------------------------------------
    # 情绪分类 (Mood Category) 管理
    # ------------------------------------------------------------------

    def upsert_mood(
        self,
        voice_id: str,
        mood_key: str,
        *,
        label: str = "",
        instruction: str = "",
        keywords: list[str] | str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        vid = validate_safe_id(voice_id, "音色 ID")
        mk = validate_safe_id(mood_key, "情绪分类标识")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")

        moods = self._voices[vid]["moods"]
        existing = moods.get(mk, {})
        default_m = DEFAULT_MOODS.get(mk, {})

        if isinstance(keywords, str):
            kw_list = [
                x.strip()
                for x in re.split(r"[,，\n]", keywords)
                if x.strip()
            ]
        elif isinstance(keywords, list):
            kw_list = [str(x).strip() for x in keywords if str(x).strip()]
        else:
            kw_list = list(existing.get("keywords") or default_m.get("keywords") or [])

        moods[mk] = {
            "label": (label or "").strip()
            or existing.get("label")
            or default_m.get("label")
            or mk,
            "enabled": bool(
                existing.get("enabled", True) if enabled is None else enabled
            ),
            "instruction": (
                instruction.strip()
                if instruction is not None and instruction != ""
                else existing.get("instruction") or default_m.get("instruction") or ""
            ),
            "keywords": list(dict.fromkeys(kw_list)),
        }
        (self.voices_dir / vid / mk).mkdir(parents=True, exist_ok=True)
        self.save_registry()
        return copy.deepcopy(moods[mk])

    def delete_mood(self, voice_id: str, mood_key: str) -> None:
        vid = validate_safe_id(voice_id, "音色 ID")
        mk = validate_safe_id(mood_key, "情绪分类标识")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        vdata = self._voices[vid]
        if mk not in vdata["moods"]:
            raise KeyError(f"情绪分类 '{mk}' 不存在")
        if len(vdata["moods"]) <= 1:
            raise ValueError("每个音色至少需要保留一个情绪分类")

        vdata["moods"].pop(mk, None)
        for rk in [k for k in vdata["clips"] if k.startswith(f"{mk}/")]:
            vdata["clips"].pop(rk, None)

        mdir = self.voices_dir / vid / mk
        if mdir.is_dir():
            shutil.rmtree(mdir, ignore_errors=True)
        self.save_registry()

    def pick_mood_for_text(
        self,
        voice_id: str,
        text: str,
        *,
        auto_mood: bool = True,
        fixed_mood: str = "random",
    ) -> str | None:
        """为指定音色与回复文本挑选一个已启用的情绪分类 key。"""
        vid = validate_safe_id(voice_id, "音色 ID")
        vdata = self._voices.get(vid)
        if not vdata:
            return None
        enabled_moods = {
            mk: mmeta
            for mk, mmeta in (vdata.get("moods") or {}).items()
            if mmeta.get("enabled", True)
        }
        if not enabled_moods:
            return None

        if not auto_mood:
            fm = (fixed_mood or "random").strip()
            if fm in enabled_moods:
                return fm
            return random.choice(list(enabled_moods.keys()))

        scores: dict[str, int] = {}
        for mk, mmeta in enabled_moods.items():
            kws = mmeta.get("keywords") or []
            score = sum(1 for kw in kws if kw and kw in text)
            if score > 0:
                scores[mk] = score

        if not scores:
            return random.choice(list(enabled_moods.keys()))

        max_score = max(scores.values())
        best = [mk for mk, s in scores.items() if s == max_score]
        return random.choice(best)

    # ------------------------------------------------------------------
    # 情绪语音片段 (Audio Clips) 管理与随机选择
    # ------------------------------------------------------------------

    def list_clips(self, voice_id: str, mood_key: str | None = None) -> list[dict]:
        """列出指定音色（及可选指定情绪分类）下的全部语音文件详情。"""
        vid = validate_safe_id(voice_id, "音色 ID")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        self.sync_filesystem()

        vdata = self._voices[vid]
        vroot = self.voices_dir / vid
        moods = vdata.get("moods") or {}
        clips_meta = vdata.get("clips") or {}

        items: list[dict] = []
        for rel_key, meta in sorted(
            clips_meta.items(),
            key=lambda kv: (kv[1].get("created_at", 0) if isinstance(kv[1], dict) else 0),
            reverse=True,
        ):
            if "/" not in rel_key:
                continue
            mk, fname = rel_key.split("/", 1)
            if mood_key and mk != mood_key:
                continue
            fpath = vroot / mk / fname
            if not fpath.is_file():
                continue
            stat = fpath.stat()
            m_info = moods.get(mk, {})
            items.append(
                {
                    "id": rel_key,
                    "voice_id": vid,
                    "mood": mk,
                    "mood_label": m_info.get("label", mk),
                    "filename": fname,
                    "enabled": bool(meta.get("enabled", True)),
                    "text": str(meta.get("text") or vdata.get("chirp_text") or "菲比啾比"),
                    "instruction": str(
                        meta.get("instruction") or m_info.get("instruction") or ""
                    ),
                    "source": str(meta.get("source") or "engine"),
                    "size": stat.st_size,
                    "created_at": float(meta.get("created_at") or stat.st_mtime),
                    "format": fpath.suffix.lower().lstrip("."),
                }
            )
        return items

    def get_clip_path(self, voice_id: str, mood_key: str, filename: str) -> Path:
        vid = validate_safe_id(voice_id, "音色 ID")
        mk = validate_safe_id(mood_key, "情绪分类标识")
        safe_fname = Path(str(filename or "").replace("\\", "/")).name
        if not safe_fname or safe_fname in {".", ".."}:
            raise ValueError("无效的语音文件名")
        fpath = (self.voices_dir / vid / mk / safe_fname).resolve()
        vroot = (self.voices_dir / vid).resolve()
        fpath.relative_to(vroot)
        if not fpath.is_file():
            raise FileNotFoundError(f"语音文件不存在: {vid}/{mk}/{safe_fname}")
        return fpath

    def pick_random_clip(self, voice_id: str, mood_key: str) -> tuple[Path | None, bool]:
        """从指定音色的指定情绪目录中随机挑选一条「已启用」的语音文件。

        Returns:
            tuple[Path | None, bool]:
                - (Path, False): 成功随机命中一条已启用的语音文件
                - (None, False): 该情绪下有语音文件，但被用户全部设为了「禁用」（此时不应自动合成）
                - (None, True): 该情绪目录为空，允许触发实时自动合成
        """
        vid = validate_safe_id(voice_id, "音色 ID")
        mk = validate_safe_id(mood_key, "情绪分类标识")
        vdata = self._voices.get(vid)
        if not vdata:
            return None, False

        mdir = self.voices_dir / vid / mk
        if not mdir.is_dir():
            return None, True

        clips_meta = vdata.get("clips") or {}
        all_files: list[Path] = []
        enabled_files: list[Path] = []

        for fpath in sorted(mdir.iterdir()):
            if (
                not fpath.is_file()
                or fpath.name.startswith(".")
                or fpath.suffix.lower() not in ALLOWED_AUDIO_EXTS
                or fpath.stat().st_size == 0
            ):
                continue
            all_files.append(fpath)
            rel_key = f"{mk}/{fpath.name}"
            meta = clips_meta.get(rel_key)
            is_enabled = True if not isinstance(meta, dict) else bool(meta.get("enabled", True))
            if is_enabled:
                enabled_files.append(fpath)

        if enabled_files:
            return random.choice(enabled_files), False
        if all_files:
            # 目录里有文件但全部处于禁用状态
            return None, False
        return None, True

    def add_clip_bytes(
        self,
        voice_id: str,
        mood_key: str,
        audio_bytes: bytes,
        *,
        ext: str = "wav",
        filename: str = "",
        source: str = "engine",
        text: str = "",
        instruction: str = "",
        enabled: bool = True,
    ) -> dict:
        """向 `<voice_id>/<mood_key>/` 目录保存一条新增语音（引擎生成或用户上传）。"""
        if not audio_bytes:
            raise ValueError("音频内容不能为空")
        vid = validate_safe_id(voice_id, "音色 ID")
        mk = validate_safe_id(mood_key, "情绪分类标识")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        vdata = self._voices[vid]
        if mk not in vdata["moods"]:
            self.upsert_mood(vid, mk)

        mdir = self.voices_dir / vid / mk
        mdir.mkdir(parents=True, exist_ok=True)

        clean_ext = "." + (ext or "wav").strip().lower().lstrip(".")
        if clean_ext not in ALLOWED_AUDIO_EXTS:
            clean_ext = ".wav"

        if filename and filename.strip():
            candidate = sanitize_filename(filename, clean_ext)
        else:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            candidate = f"{mk}_{stamp}{clean_ext}"

        stem = Path(candidate).stem
        suffix = Path(candidate).suffix
        final_name = candidate
        seq = 2
        while (mdir / final_name).exists():
            final_name = f"{stem}_{seq}{suffix}"
            seq += 1

        target_path = mdir / final_name
        target_path.write_bytes(audio_bytes)

        rel_key = f"{mk}/{final_name}"
        now = time.time()
        m_info = vdata["moods"].get(mk, {})
        vdata["clips"][rel_key] = {
            "enabled": bool(enabled),
            "text": (text or "").strip() or vdata.get("chirp_text", "菲比啾比"),
            "instruction": (instruction or "").strip() or m_info.get("instruction", ""),
            "source": source,
            "created_at": now,
        }
        self.save_registry()

        return {
            "id": rel_key,
            "voice_id": vid,
            "mood": mk,
            "mood_label": m_info.get("label", mk),
            "filename": final_name,
            "path": str(target_path),
            "enabled": bool(enabled),
            "text": vdata["clips"][rel_key]["text"],
            "instruction": vdata["clips"][rel_key]["instruction"],
            "source": source,
            "size": len(audio_bytes),
            "created_at": now,
            "format": suffix.lstrip("."),
        }

    def set_clip_enabled(
        self, voice_id: str, mood_key: str, filename: str, enabled: bool
    ) -> dict:
        fpath = self.get_clip_path(voice_id, mood_key, filename)
        vid = validate_safe_id(voice_id, "音色 ID")
        mk = validate_safe_id(mood_key, "情绪分类标识")
        rel_key = f"{mk}/{fpath.name}"
        vdata = self._voices[vid]
        meta = vdata["clips"].setdefault(
            rel_key,
            {
                "enabled": True,
                "text": vdata.get("chirp_text", "菲比啾比"),
                "instruction": vdata["moods"].get(mk, {}).get("instruction", ""),
                "source": "local",
                "created_at": fpath.stat().st_mtime,
            },
        )
        meta["enabled"] = bool(enabled)
        self.save_registry()
        return {
            "id": rel_key,
            "voice_id": vid,
            "mood": mk,
            "filename": fpath.name,
            "enabled": bool(enabled),
        }

    def batch_set_clips_enabled(
        self, voice_id: str, clip_ids: list[str], enabled: bool
    ) -> int:
        vid = validate_safe_id(voice_id, "音色 ID")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        updated = 0
        for cid in clip_ids:
            if not isinstance(cid, str) or "/" not in cid:
                continue
            mk, fname = cid.split("/", 1)
            try:
                self.set_clip_enabled(vid, mk, fname, enabled)
                updated += 1
            except Exception:
                continue
        return updated

    def move_clip(
        self, voice_id: str, from_mood: str, filename: str, to_mood: str
    ) -> dict:
        vid = validate_safe_id(voice_id, "音色 ID")
        src_mk = validate_safe_id(from_mood, "源情绪分类")
        dst_mk = validate_safe_id(to_mood, "目标情绪分类")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        vdata = self._voices[vid]
        if dst_mk not in vdata["moods"]:
            raise KeyError(f"目标情绪分类 '{dst_mk}' 不存在")
        src_path = self.get_clip_path(vid, src_mk, filename)
        if src_mk == dst_mk:
            return {"id": f"{src_mk}/{src_path.name}", "filename": src_path.name}

        dst_dir = self.voices_dir / vid / dst_mk
        dst_dir.mkdir(parents=True, exist_ok=True)
        stem = src_path.stem
        suffix = src_path.suffix
        dst_name = src_path.name
        seq = 2
        while (dst_dir / dst_name).exists():
            dst_name = f"{stem}_{seq}{suffix}"
            seq += 1

        dst_path = dst_dir / dst_name
        shutil.move(str(src_path), str(dst_path))

        old_key = f"{src_mk}/{src_path.name}"
        new_key = f"{dst_mk}/{dst_name}"
        old_meta = vdata["clips"].pop(old_key, {})
        vdata["clips"][new_key] = {
            "enabled": bool(old_meta.get("enabled", True)),
            "text": old_meta.get("text") or vdata.get("chirp_text", "菲比啾比"),
            "instruction": old_meta.get("instruction") or "",
            "source": old_meta.get("source") or "local",
            "created_at": old_meta.get("created_at") or dst_path.stat().st_mtime,
        }
        self.save_registry()
        return {
            "old_id": old_key,
            "id": new_key,
            "mood": dst_mk,
            "filename": dst_name,
        }

    def delete_clip(self, voice_id: str, mood_key: str, filename: str) -> None:
        fpath = self.get_clip_path(voice_id, mood_key, filename)
        vid = validate_safe_id(voice_id, "音色 ID")
        mk = validate_safe_id(mood_key, "情绪分类标识")
        rel_key = f"{mk}/{fpath.name}"
        fpath.unlink(missing_ok=True)
        self._voices[vid]["clips"].pop(rel_key, None)
        self.save_registry()

    def batch_delete_clips(self, voice_id: str, clip_ids: list[str]) -> int:
        vid = validate_safe_id(voice_id, "音色 ID")
        if vid not in self._voices:
            raise KeyError(f"音色 '{vid}' 不存在")
        deleted = 0
        for cid in clip_ids:
            if not isinstance(cid, str) or "/" not in cid:
                continue
            mk, fname = cid.split("/", 1)
            try:
                self.delete_clip(vid, mk, fname)
                deleted += 1
            except Exception:
                continue
        return deleted

    # ------------------------------------------------------------------
    # 克隆参考音频样本 (Samples) 管理
    # ------------------------------------------------------------------

    def list_samples(self) -> list[dict]:
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        referenced_by: dict[str, list[str]] = {}
        for vdata in self._voices.values():
            sname = str(
                (vdata.get("engine_config") or {}).get("sample_name") or ""
            ).strip()
            if sname:
                referenced_by.setdefault(sname, []).append(vdata.get("name") or vdata["id"])

        items: list[dict] = []
        for fpath in sorted(self.samples_dir.iterdir()):
            if (
                not fpath.is_file()
                or fpath.name.startswith(".")
                or fpath.suffix.lower() not in ALLOWED_AUDIO_EXTS
            ):
                continue
            stat = fpath.stat()
            items.append(
                {
                    "name": fpath.name,
                    "size": stat.st_size,
                    "created_at": stat.st_mtime,
                    "referenced_by": referenced_by.get(fpath.name, []),
                }
            )
        return items

    def save_sample(self, filename: str, data: bytes) -> dict:
        if not data:
            raise ValueError("上传的样本音频内容为空")
        safe_name = sanitize_filename(filename, ".wav")
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        candidate = safe_name
        seq = 2
        while (self.samples_dir / candidate).exists():
            candidate = f"{stem}_{seq}{suffix}"
            seq += 1

        target = self.samples_dir / candidate
        target.write_bytes(data)
        return {
            "name": candidate,
            "size": len(data),
            "path": str(target),
        }

    def delete_sample(self, filename: str) -> None:
        safe_name = Path(str(filename or "").replace("\\", "/")).name
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("无效的样本文件名")
        target = self.samples_dir / safe_name
        if not target.is_file():
            raise FileNotFoundError(f"样本文件不存在: {safe_name}")
        refs = [
            vdata.get("name") or vdata["id"]
            for vdata in self._voices.values()
            if str((vdata.get("engine_config") or {}).get("sample_name") or "").strip()
            == safe_name
        ]
        if refs:
            raise ValueError(
                f"该样本正被音色 {', '.join(refs)} 引用，请先解除引用后再删除"
            )
        target.unlink()
