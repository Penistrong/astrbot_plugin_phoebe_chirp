# 更新日志

本文件记录 `astrbot_plugin_phoebe_chirp`（菲比啾比情绪语音包）各版本的迭代内容，最新版本排在最前。

## v2.0.0

模块化重构与全面升级：分离多后端语音合成引擎，支持「基础音色 → 情绪类别 → 多条语音」分级存储与多人设绑定，并新增仿表情包管理器的可视化 WebUI 管理界面。

### 新增

- **模块化语音合成引擎架构（`engines/`）**
  - 抽象统一的 `ChirpTTSEngine` 基类（`engines/base.py`），提供 `create_engine` 工厂与 `register_engine` 动态扩展接口，便于后续接入任意第三方 TTS 服务
  - **阿里云百炼引擎（`aliyun`）**：支持 `qwen-audio-3.1-tts-flash`、`qwen-audio-3.1-tts-plus`、`cosyvoice-v3-flash`、`cosyvoice-v3.5-flash` 等模型的 `SpeechSynthesizer` 接口（支持 `instruction` 自然语言语气控制与富语言标签），以及 `qwen3-tts-flash` 多模态生成接口
  - **零样本声音复刻与持久化缓存**：内置阿里云百炼 `voice-enrollment`（`/services/audio/tts/customization`）自动复刻与 `query_voice` 状态轮询流程，支持上传本地参考音频（`wav` / `mp3`）或填写公网音频 URL 自动创建专属音色，并将复刻得到的 `voice_id` 按指纹持久化缓存于 `aliyun_cloned_voices.json`，避免重复消耗百炼账号的 1000 个音色配额
  - **多引擎内置支持**：新增 OpenAI 兼容接口引擎（`openai_compat`，支持 `/v1/audio/speech` 及 `instructions` 透传）、小米 MiMo 语音合成与克隆引擎（`mimo`）、自定义 HTTP 模板引擎（`custom_http`，支持 `${TEXT}`、`${INSTRUCTION}`、`${VOICE}`、`${VOICE_SAMPLE_BASE64}` 等占位符）
- **「音色 - 情绪分类 - 多语音」分级存储与随机发送（`storage.py`）**
  - 按 `voices/<voice_id>/<mood_key>/*.wav` 目录结构分门别类管理多套基础音色与各自的情绪目录
  - **单音色绑定多个人设**：每个基础音色支持独立配置 `persona_ids` 列表、专属口癖台词（如「菲比啾比」）及专属合成引擎参数；会话人设命中时自动路由至对应音色包
  - **同情绪多语音随机选择**：每个情绪分类目录下可存储多条语音文件（支持引擎生成或用户手动上传），触发时从该情绪下处于「启用」状态的语音中随机挑选一条发送
  - **禁用守卫与空目录按需合成**：新增 `auto_generate_when_empty` 配置项；若某情绪目录下已有语音但被用户全部设为「禁用」，将严格尊重禁用状态跳过发送，不会违背用户意愿自动重新生成
  - **发送位置扩展**：`send_mode` 新增 `prepend`（置于文字开头），与原有的 `append`（追加在文字尾部）、`separate`（文字发出后单独补发语音）并存
- **可视化 WebUI 管理界面（`views/voices/`）**
  - 参考 `astrbot_plugin_meme_manager`（表情包管理器）设计「顶部音色包切换 + 左侧情绪分类导航 + 右侧语音卡片网格」布局，注册 22 条插件 Web API 路由
  - **情绪语音库管理页**：支持切换/新建/编辑/删除基础音色、可视化点选绑定多个人设、新增/编辑/删除自定义情绪分类（含合成语气 `Instruction` 与触发关键词）
  - **语音卡片操作**：支持在线置顶播放器试听、单条或批量启用/禁用/删除、跨情绪分类一键移动、下载语音文件、手动上传本地音频文件
  - **异步引擎合成任务**：支持在弹窗中指定目标情绪、一次生成条数（1~5 条随机变体）、临时微调台词（含 `[excited]`、`[laughing]` 等富语言标签）或语气指令，以及「一键补齐各情绪语音」（采用后台任务 + 短轮询机制，避免网关超时）
  - **克隆样本管理页 & 全局设置页**：支持上传、在线试听、删除用于声音复刻的参考音频样本（带音色引用保护检查），以及可视化配置全局引擎参数与触发规则（API Key 自动脱敏回显）
- **指令增强与自动化测试**
  - 新增 `/啾比 voices` 查看全部音色包、绑定人设与有效语音统计
  - 新增 `/啾比 gen [音色ID] [情绪]` 定向调用引擎合成补齐情绪语音
  - 新增 `tests/test_plugin.py` 自动化测试套件，覆盖旧版迁移、多引擎合成、声音复刻状态机与缓存、多人设路由、多语音随机选择、Web API 与前端沙箱静态一致性检查

### 兼容性与迁移说明

- **无损自动迁移**：首次启动 v2.0.0 时，会自动将 v1.0.0 旧版 `data/plugin_data/astrbot_plugin_phoebe_chirp/voice/<mood>.wav` 迁移至 `voices/phoebe/<mood>/<mood>_001.wav`，并保留原有配置项中的台词与人设绑定
- 最低 AstrBot 版本要求：`>= 4.25.0`（依赖插件 Web API `register_web_api` 与视图桥接 SDK；在更低版本中加载时会自动降级跳过 WebUI 注册，不影响聊天钩子功能）

---

## v1.0.0

初始版本发布。

### 新增

- **发送前钩子附带角色语音**：基于 `on_decorating_result` 钩子，在不改动大模型正文回复的前提下，在消息链尾部追加或单独发送一句固定台词（默认「菲比啾比」）的角色语音
- **人设绑定过滤**：自动解析当前会话生效的人设（会话规则 > 会话配置 > 默认人设），支持通过 `persona_ids` 限制仅在特定角色人设下触发语音
- **6 种内置情绪语调自动匹配**：内置 `happy`（开心）、`angry`（生气）、`sleepy`（困倦）、`sneer`（嘲讽）、`shy`（害羞）、`surprise`（惊讶）6 类情绪关键词与语气指令，按回复内容自动挑语调或使用固定情绪
- **阿里云百炼 TTS 对接与本地缓存**：支持调用阿里云百炼 `qwen-audio-3.1-tts-flash` 模型（`/services/audio/tts/SpeechSynthesizer`）按情绪指令合成语音并缓存至本地 `voice/` 目录
- **基础控制指令与防刷屏**：支持触发概率（`probability`）、同会话冷却时间（`cooldown_seconds`）、正则跳过规则（`skip_patterns`），以及 `/啾比 status`、`/啾比 on`、`/啾比 off`、`/啾比 regen` 指令
