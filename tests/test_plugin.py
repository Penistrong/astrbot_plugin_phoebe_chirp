"""菲比啾比情绪语音包插件：模块化引擎、音色-情绪分级存储、多人设绑定与 WebUI 自动化测试"""

import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG = "astrbot_plugin_phoebe_chirp"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}  {detail}")


# --------------------------------------------------------------------------
# 构造最小 AstrBot + httpx Stub 环境
# --------------------------------------------------------------------------

HTTPX_CALLS: list[tuple] = []
HTTPX_SCRIPT: list = []


class _FakeHttpxResp:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(json_data) if json_data is not None else ""

    def json(self):
        if self._json is None:
            raise ValueError("Not JSON")
        return self._json


class _FakeAsyncClient:
    def __init__(self, timeout=None, trust_env=False):
        self.timeout = timeout
        self.is_closed = False

    async def post(self, url, headers=None, json=None, content=None, data=None):
        HTTPX_CALLS.append(("POST", url, headers, json))
        if HTTPX_SCRIPT:
            return HTTPX_SCRIPT.pop(0)
        b64 = base64.b64encode(b"RIFF-default-synth-wav").decode("ascii")
        return _FakeHttpxResp(200, {"output": {"audio": {"data": b64}}})

    async def get(self, url, headers=None):
        HTTPX_CALLS.append(("GET", url, headers, None))
        if HTTPX_SCRIPT:
            return HTTPX_SCRIPT.pop(0)
        return _FakeHttpxResp(
            200, content=b"RIFF-downloaded-wav", headers={"content-type": "audio/wav"}
        )

    async def aclose(self):
        self.is_closed = True


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Config(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.saved = 0

    def save_config(self):
        self.saved += 1

    async def save_config_async(self):
        self.saved += 1


class _DummyFilter:
    def on_decorating_result(self, *_a, **_k):
        return lambda fn: fn

    def command(self, *_a, **_k):
        return lambda fn: fn


class _Plain:
    def __init__(self, text: str):
        self.text = text


class _Record:
    def __init__(self, file: str = "", url: str = ""):
        self.file = file
        self.url = url


class _Result:
    def __init__(self, chain: list, is_llm: bool = True):
        self.chain = chain
        self._is_llm = is_llm

    def is_llm_result(self):
        return self._is_llm


class _MsgEvent:
    def __init__(self, text: str, persona_id: str = "", umo: str = "qq:Group:1001"):
        self.unified_msg_origin = umo
        self.message_str = text
        self._extra = {"_trace_persona_id": persona_id} if persona_id else {}
        self._result = _Result([_Plain(text)], is_llm=True)

    def get_result(self):
        return self._result

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)

    def plain_result(self, text: str):
        return text

    def chain_result(self, chain: list):
        return chain


class _PersonaManager:
    def __init__(self):
        self.personas_v3 = [
            {"name": "菲比啾比-Phoebe", "prompt": "..."},
            {"name": "赞妮-Zani", "prompt": "..."},
            {"name": "卡提希娅-Cartethyia", "prompt": "..."},
        ]

    def get_persona_v3_by_id(self, pid: str):
        return next((p for p in self.personas_v3 if p["name"] == pid), None)

    async def get_default_persona_v3(self, umo=None):
        return self.personas_v3[0]


class _Context:
    def __init__(self):
        self.routes = {}
        self.persona_manager = _PersonaManager()

    def register_web_api(self, route, handler, methods, desc):
        self.routes[route] = (handler, tuple(methods), desc)


class _Request:
    def __init__(self):
        self.payload = {}
        self.query = {}
        self.upload = None

    async def json(self, default=None):
        return self.payload if self.payload is not None else default

    async def files(self):
        return {"file": self.upload} if self.upload is not None else {}


def _install_stubs(data_dir: Path):
    fake_httpx = types.ModuleType("httpx")
    fake_httpx.AsyncClient = _FakeAsyncClient
    fake_httpx.Response = _FakeHttpxResp
    sys.modules["httpx"] = fake_httpx

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()

    class _SP:
        async def get_async(self, **_kw):
            return {}

    api.sp = _SP()
    astrbot.api = api

    comp = types.ModuleType("astrbot.api.message_components")
    comp.Plain = _Plain
    comp.Record = _Record

    event = types.ModuleType("astrbot.api.event")
    event.filter = _DummyFilter()
    event.AstrMessageEvent = _MsgEvent

    star = types.ModuleType("astrbot.api.star")
    star.Context = _Context

    class _Star:
        def __init__(self, context):
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(_name):
            return data_dir

    star.Star = _Star
    star.StarTools = _StarTools
    star.register = lambda *_a, **_k: (lambda cls: cls)

    web = types.ModuleType("astrbot.api.web")
    req = _Request()
    web.request = req
    web.json_response = lambda d: {"__ok__": True, "data": d}
    web.error_response = lambda m, status_code=400: {"__ok__": False, "error": m}
    web.file_response = lambda p, filename=None, content_type=None: {
        "__ok__": True,
        "data": {"__file__": str(p), "filename": filename, "content_type": content_type},
    }

    core = types.ModuleType("astrbot.core")
    core_cfg = types.ModuleType("astrbot.core.config")
    astr_cfg = types.ModuleType("astrbot.core.config.astrbot_config")
    astr_cfg.AstrBotConfig = _Config

    for mod_name, mod_obj in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.message_components": comp,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.web": web,
        "astrbot.core": core,
        "astrbot.core.config": core_cfg,
        "astrbot.core.config.astrbot_config": astr_cfg,
    }.items():
        sys.modules[mod_name] = mod_obj

    return web


def _load_package_modules():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG] = pkg

    # 加载 engines 子包
    engines_pkg = types.ModuleType(f"{PKG}.engines")
    engines_pkg.__path__ = [str(PLUGIN_DIR / "engines")]
    sys.modules[f"{PKG}.engines"] = engines_pkg

    for sub in ("base", "aliyun", "openai_compat", "mimo", "custom_http", "__init__"):
        mod_name = f"{PKG}.engines" if sub == "__init__" else f"{PKG}.engines.{sub}"
        spec = importlib.util.spec_from_file_location(
            mod_name, PLUGIN_DIR / "engines" / f"{sub}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    # 加载 storage
    storage_spec = importlib.util.spec_from_file_location(
        f"{PKG}.storage", PLUGIN_DIR / "storage.py"
    )
    storage_mod = importlib.util.module_from_spec(storage_spec)
    sys.modules[f"{PKG}.storage"] = storage_mod
    storage_spec.loader.exec_module(storage_mod)

    # 加载 main
    main_spec = importlib.util.spec_from_file_location(
        f"{PKG}.main", PLUGIN_DIR / "main.py"
    )
    main_mod = importlib.util.module_from_spec(main_spec)
    sys.modules[f"{PKG}.main"] = main_mod
    main_spec.loader.exec_module(main_mod)
    return main_mod, storage_mod, sys.modules[f"{PKG}.engines"]


def test_frontend_consistency():
    print("\n[6] WebUI 前端静态一致性检查")
    html = (PLUGIN_DIR / "views" / "voices" / "index.html").read_text(encoding="utf-8")
    js = (PLUGIN_DIR / "views" / "voices" / "app.js").read_text(encoding="utf-8")
    css = (PLUGIN_DIR / "views" / "voices" / "style.css").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_byids = set(re.findall(r'byId\("([^"]+)"\)', js))
    missing_ids = sorted(js_byids - html_ids)
    check("JS 中所有 byId(...) 均存在于 HTML", not missing_ids, str(missing_ids))

    els_block = js[js.index("Object.assign(els, {") :]
    els_block = els_block[: els_block.index("});")]
    assigned_els = set(re.findall(r"^\s{4}(\w+):\s*byId\(", els_block, re.M))
    used_els = set(re.findall(r"\bels\.(\w+)", js))
    missing_els = sorted(used_els - assigned_els)
    check("所有 els.xxx 属性均已绑定", not missing_els, str(missing_els))

    check("CSS 包含 [hidden] 保护", "[hidden]" in css)

    stripped = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
    for forbidden in ("window.confirm", "window.alert", "window.prompt", "localStorage"):
        check(f"未使用沙箱禁用的 {forbidden}", forbidden not in stripped)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="phoebe_chirp_test_"))
    try:
        # 预置一条旧版 v1.0 的 voice/happy.wav 以验证自动迁移
        legacy_dir = workdir / "voice"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "happy.wav").write_bytes(b"RIFF-legacy-happy-wav")

        web = _install_stubs(workdir)
        main_mod, storage_mod, engines_mod = _load_package_modules()

        ctx = _Context()
        cfg = _Config(
            {
                "enable": True,
                "chirp_text": "菲比啾比",
                "persona_ids": ["菲比啾比-Phoebe", "小菲比"],
                "probability": 100,
                "cooldown_seconds": 0,
                "send_mode": "append",
                "auto_mood": True,
                "fixed_mood": "random",
                "auto_generate_when_empty": True,
                "engine_type": "aliyun",
                "api_key": "sk-test-bailian",
                "api_base": "https://dashscope.aliyuncs.com/api/v1",
                "model": "qwen-audio-3.1-tts-flash",
                "default_voice": "longanhuan_v3.1",
            }
        )
        plugin = main_mod.PhoebeChirpPlugin(ctx, cfg)

        print("\n[1] 旧版音频迁移与分级目录初始化")
        voices = plugin.storage.list_voices()
        check("默认创建了 phoebe 音色", len(voices) == 1 and voices[0]["id"] == "phoebe")
        check(
            "旧版 voice/happy.wav 自动迁移到 voices/phoebe/happy/happy_001.wav",
            (workdir / "voices" / "phoebe" / "happy" / "happy_001.wav").is_file(),
        )
        check(
            "默认音色继承了多人设绑定",
            voices[0]["persona_ids"] == ["菲比啾比-Phoebe", "小菲比"],
            str(voices[0]["persona_ids"]),
        )
        check("注册了 22 条 WebUI API 路由", len(ctx.routes) == 22, str(len(ctx.routes)))

        print("\n[2] 模块化语音合成引擎 (Aliyun / OpenAI / MiMo / CustomHTTP)")
        # 2.1 阿里云百炼克隆 + SpeechSynthesizer
        sample_res = plugin.storage.save_sample("ref_sample.wav", b"RIFF-sample-bytes")
        check("保存克隆样本成功", sample_res["name"] == "ref_sample.wav")

        HTTPX_CALLS.clear()
        HTTPX_SCRIPT[:] = [
            _FakeHttpxResp(200, {"output": {"voice_id": "qwen-audio-3.1-tts-flash-phoebe-999"}}),
            _FakeHttpxResp(200, {"output": {"status": "OK"}}),
            _FakeHttpxResp(200, {"output": {"audio": {"url": "https://oss.example/chirp.wav"}}}),
            _FakeHttpxResp(200, content=b"RIFF-cloned-chirp", headers={"content-type": "audio/wav"}),
        ]
        aliyun_eng = engines_mod.create_engine(
            "aliyun",
            {
                "api_key": "sk-test",
                "model": "qwen-audio-3.1-tts-flash",
                "sample_name": "ref_sample.wav",
                "voice_prefix": "phoebe",
            },
            data_dir=workdir,
        )
        audio_bytes, fmt = asyncio.run(
            aliyun_eng.synthesize("菲比啾比", instruction="用开心的语气")
        )
        check(
            "百炼引擎自动执行 create_voice -> query_voice -> SpeechSynthesizer",
            len(HTTPX_CALLS) == 4
            and HTTPX_CALLS[0][1].endswith("/services/audio/tts/customization")
            and HTTPX_CALLS[2][1].endswith("/services/audio/tts/SpeechSynthesizer")
            and HTTPX_CALLS[2][3]["input"]["voice"] == "qwen-audio-3.1-tts-flash-phoebe-999"
            and HTTPX_CALLS[2][3]["input"]["instruction"] == "用开心的语气",
        )
        check("百炼引擎返回音频正确", audio_bytes == b"RIFF-cloned-chirp" and fmt == "wav")

        # 2.2 OpenAI 兼容引擎
        HTTPX_CALLS.clear()
        HTTPX_SCRIPT[:] = [
            _FakeHttpxResp(200, content=b"RIFF-oai-audio", headers={"content-type": "audio/wav"})
        ]
        oai_eng = engines_mod.create_engine(
            "openai_compat",
            {"api_key": "sk-oai", "model": "gpt-4o-mini-tts", "voice_id": "nova"},
            data_dir=workdir,
        )
        oai_audio, _ = asyncio.run(oai_eng.synthesize("菲比啾比", instruction="cheerful"))
        check(
            "OpenAI 兼容引擎透传 instructions",
            HTTPX_CALLS[0][1].endswith("/audio/speech")
            and HTTPX_CALLS[0][3]["instructions"] == "cheerful"
            and oai_audio == b"RIFF-oai-audio",
        )

        print("\n[3] 多音色创建、多人设绑定匹配与同一情绪多语音随机选择")
        # 创建第二个音色「赞妮」，绑定 ["赞妮-Zani", "打工人赞妮"]
        web.request.payload = {
            "id": "zani",
            "name": "赞妮",
            "chirp_text": "叹气…加班",
            "persona_ids": ["赞妮-Zani", "打工人赞妮"],
            "engine_type": "aliyun",
            "engine_config": {"default_voice": "longanyang"},
        }
        res = asyncio.run(plugin.web_create_voice())
        check("通过 Web API 创建第二个音色成功", res.get("__ok__") is True, str(res)[:160])

        # 测试人设路由
        m_phoebe = plugin.storage.match_voice_for_persona("菲比啾比-Phoebe")
        m_phoebe2 = plugin.storage.match_voice_for_persona("小菲比")
        m_zani = plugin.storage.match_voice_for_persona("打工人赞妮")
        m_unmatched = plugin.storage.match_voice_for_persona("路人甲")
        check("人设 1 匹配到 phoebe", m_phoebe and m_phoebe["id"] == "phoebe")
        check("人设 2 匹配到 phoebe（一人多绑）", m_phoebe2 and m_phoebe2["id"] == "phoebe")
        check("人设 3 匹配到 zani", m_zani and m_zani["id"] == "zani")
        check("未绑定人设且默认音色有限定列表时不触发", m_unmatched is None)

        # 向 phoebe/happy 再添加 2 条语音（共 3 条：happy_001.wav, happy_a.wav, happy_b.wav）
        plugin.storage.add_clip_bytes(
            "phoebe", "happy", b"RIFF-happy-A", filename="happy_a.wav", source="upload"
        )
        plugin.storage.add_clip_bytes(
            "phoebe", "happy", b"RIFF-happy-B", filename="happy_b.wav", source="engine"
        )
        picked_names = {
            plugin.storage.pick_random_clip("phoebe", "happy")[0].name
            for _ in range(40)
        }
        check(
            "同一情绪目录下多条语音可随机命中不同文件",
            len(picked_names) >= 2 and picked_names <= {"happy_001.wav", "happy_a.wav", "happy_b.wav"},
            str(picked_names),
        )

        print("\n[4] 语音禁用、批量启停、移动、上传与引擎生成 Web API")
        # 禁用 happy_a.wav 和 happy_b.wav，只留 happy_001.wav
        web.request.payload = {
            "voice_id": "phoebe",
            "clip_ids": ["happy/happy_a.wav", "happy/happy_b.wav"],
            "enabled": False,
        }
        res = asyncio.run(plugin.web_batch_toggle_clips())
        check("批量禁用语音成功", res.get("__ok__") is True and res["data"]["updated"] == 2)
        only_picked = {
            plugin.storage.pick_random_clip("phoebe", "happy")[0].name
            for _ in range(15)
        }
        check("被禁用的语音不会被随机选中", only_picked == {"happy_001.wav"}, str(only_picked))

        # 再把最后一条 happy_001.wav 也禁用 -> 该情绪下全禁用，不应触发自动合成
        web.request.payload = {
            "voice_id": "phoebe",
            "mood": "happy",
            "filename": "happy_001.wav",
            "enabled": False,
        }
        asyncio.run(plugin.web_toggle_clip())
        p_none, is_empty = plugin.storage.pick_random_clip("phoebe", "happy")
        check("全部禁用时返回 (None, is_empty=False)", p_none is None and is_empty is False)
        resolved = asyncio.run(plugin._resolve_or_generate_clip("phoebe", "happy"))
        check("全部禁用时不会违背用户意愿自动生成新语音", resolved is None)

        # 重新启用 happy_001.wav
        web.request.payload["enabled"] = True
        asyncio.run(plugin.web_toggle_clip())

        # 测试试听 API
        web.request.query = {
            "voice_id": "phoebe",
            "mood": "happy",
            "filename": "happy_001.wav",
        }
        audio_res = asyncio.run(plugin.web_get_clip_audio())
        check(
            "试听接口返回 Data URI",
            audio_res.get("__ok__") is True
            and str(audio_res["data"]["audio"]).startswith("data:audio/wav;base64,"),
        )

        # 测试跨情绪移动语音：把 happy_a.wav 移到 shy
        web.request.payload = {
            "voice_id": "phoebe",
            "from_mood": "happy",
            "to_mood": "shy",
            "filename": "happy_a.wav",
        }
        move_res = asyncio.run(plugin.web_move_clip())
        check(
            "跨情绪移动语音成功",
            move_res.get("__ok__") is True
            and (workdir / "voices" / "phoebe" / "shy" / "happy_a.wav").is_file(),
        )

        # 测试 WebUI 后台引擎生成任务 (generate + poll，必须在同一个事件循环中推进后台 task)
        async def _submit_and_poll_gen():
            web.request.payload = {
                "voice_id": "phoebe",
                "mood": "angry",
                "count": 2,
                "text": "菲比啾比！",
            }
            gen_start = await plugin.web_generate_clips()
            job_id = gen_start["data"]["job_id"]
            for _ in range(50):
                web.request.query = {"job_id": job_id}
                res = await plugin.web_generate_result()
                if res["data"]["status"] != "pending":
                    return res
                await asyncio.sleep(0.01)
            return res

        gen_res = asyncio.run(_submit_and_poll_gen())
        check(
            "WebUI 引擎生成 2 条 angry 语音成功",
            gen_res["data"]["status"] == "done"
            and len(gen_res["data"]["generated"]) == 2,
            str(gen_res)[:160],
        )

        print("\n[5] 发送钩子端到端验证（按人设匹配音色 + 按关键词选情绪 + 追加语音）")
        ev = _MsgEvent("哈哈哈今天太开心了！", persona_id="菲比啾比-Phoebe")
        asyncio.run(plugin.chirp_hook(ev))
        chain = ev.get_result().chain
        check(
            "命中人设与开心关键词后，在消息链尾部追加了 happy 语音 Record",
            len(chain) == 2
            and isinstance(chain[0], _Plain)
            and isinstance(chain[1], _Record)
            and "voices/phoebe/happy/happy_001.wav" in chain[1].file.replace("\\", "/"),
            str([getattr(c, "text", getattr(c, "file", "")) for c in chain]),
        )

        test_frontend_consistency()

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        print(f"=== {len(failures)} 项测试失败 ===")
        for f in failures:
            print("  -", f)
        return 1
    print("=== 全部测试通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
