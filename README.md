# 菲比啾比语音

文字照常发，额外附带一句固定语音「菲比啾比」；语调按回复情绪自动切换，音频本地缓存，缺了才调 Qwen-Audio-TTS 现合成。

## 工作原理

1. 挂在 `on_decorating_result`（发送前）钩子，不改动任何文字组件。
2. 判断当前会话**实际生效的人格**：会话规则 > 会话配置 > 默认人格，再和 `persona_ids` 比对。
3. 按回复文本里的关键词挑一个情绪（开心 / 生气 / 困 / 嘲讽 / 害羞 / 惊讶），从 `data/plugin_data/astrbot_plugin_phoebe_chirp/voice/<mood>.wav` 取音频。
4. `send_mode=append` 时把 `Record` 追加到消息链尾部，文字和语音同一条发出；`separate` 时在文字发出后补一条语音。

## 配置要点

| 项 | 说明 |
| --- | --- |
| `enable` | 总开关 |
| `persona_ids` | 绑人格，填人格 ID（如 `菲比啾比-Phoebe`），留空表示全部人格 |
| `probability` / `cooldown_seconds` | 触发概率、同会话冷却，防止刷屏 |
| `send_mode` | 适配器不支持图文混排时改成 `separate` |
| `auto_mood` / `fixed_mood` | 情绪自动判定，或固定某一种 |
| `api_key` / `voice_id` / `model` | DashScope 凭据与音色 |

## 指令

- `/啾比` 查看状态（当前人格、概率、缓存条数）
- `/啾比 on` / `/啾比 off` 开关
- `/啾比 regen` 清缓存，下次回复重新合成

## 换音色 / 换念的内容

改 `chirp_text` 就念别的；换 `voice_id` 后执行 `/啾比 regen` 让缓存重建。

## 目录

```
data/plugin_data/astrbot_plugin_phoebe_chirp/voice/*.wav   # 情绪音频缓存
```
