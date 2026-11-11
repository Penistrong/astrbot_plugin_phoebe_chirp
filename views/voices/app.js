/**
 * 菲比啾比 · 角色情绪语音包管理 WebUI
 *
 * 运行于 AstrBot 插件视图沙箱 iframe，所有后端交互经由 window.AstrBotPluginView bridge 完成。
 */

const bridge = window.AstrBotPluginView;

const state = {
  voices: [],
  selectedVoiceId: "",
  selectedMood: "__all__",
  clips: [],
  samples: [],
  personas: [],
  engines: {},
  settings: {},
  selectedClipIds: new Set(),
};

let editingVoiceId = null;
let editingMoodKey = null;
let confirmResolve = null;
let fileInput = null;

const els = {};

function byId(id) {
  return document.getElementById(id);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatSize(bytes) {
  const n = Number(bytes || 0);
  if (n <= 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function setBanner(msg, kind = "error") {
  if (!msg) {
    els.banner.hidden = true;
    els.banner.textContent = "";
    return;
  }
  els.banner.hidden = false;
  els.banner.textContent = msg;
  els.banner.dataset.kind = kind;
}

function askConfirm(message) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    els.confirmMessage.textContent = message;
    els.confirmDialog.showModal();
  });
}

function settleConfirm(ok) {
  if (!confirmResolve) return;
  const fn = confirmResolve;
  confirmResolve = null;
  if (els.confirmDialog.open) els.confirmDialog.close();
  fn(ok);
}

function pickFile(onPicked, accept = ".wav,.mp3,.ogg,.m4a,.flac,audio/*") {
  if (!fileInput) {
    fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.style.display = "none";
    document.body.appendChild(fileInput);
    fileInput.addEventListener("change", async () => {
      const f = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (f && typeof fileInput._onPicked === "function") {
        await fileInput._onPicked(f);
      }
    });
  }
  fileInput.accept = accept;
  fileInput._onPicked = onPicked;
  fileInput.click();
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读取本地音频文件失败"));
    reader.readAsDataURL(file);
  });
}

function getCurrentVoice() {
  return state.voices.find((v) => v.id === state.selectedVoiceId) || state.voices[0] || null;
}

function getFilteredClips() {
  if (!state.selectedMood || state.selectedMood === "__all__") {
    return state.clips;
  }
  return state.clips.filter((c) => c.mood === state.selectedMood);
}

/* ------------------------------------------------------------------ */
/* 数据应用与顶栏状态                                                  */
/* ------------------------------------------------------------------ */

function applyOverview(data) {
  if (!data || typeof data !== "object") return;
  if (Array.isArray(data.voices)) state.voices = data.voices;
  if (data.selected_voice_id) state.selectedVoiceId = data.selected_voice_id;
  if (!state.voices.some((v) => v.id === state.selectedVoiceId)) {
    state.selectedVoiceId = state.voices[0] ? state.voices[0].id : "";
  }
  if (Array.isArray(data.clips)) state.clips = data.clips;
  if (Array.isArray(data.samples)) state.samples = data.samples;
  if (Array.isArray(data.personas)) state.personas = data.personas;
  if (data.engines && typeof data.engines === "object") state.engines = data.engines;
  if (data.settings && typeof data.settings === "object") state.settings = data.settings;

  // 若当前选中的 mood 在新音色里不存在，则回退到 __all__
  const curVoice = getCurrentVoice();
  if (
    state.selectedMood !== "__all__" &&
    curVoice &&
    !(curVoice.moods && curVoice.moods[state.selectedMood])
  ) {
    state.selectedMood = "__all__";
  }

  // 清理已不存在的选中项
  const validIds = new Set(state.clips.map((c) => c.id));
  for (const cid of Array.from(state.selectedClipIds)) {
    if (!validIds.has(cid)) state.selectedClipIds.delete(cid);
  }

  renderStatusChips();
  renderVoiceSelector();
  renderVoiceProfileCard();
  renderMoodSidebar();
  renderMoodDetailBar();
  renderClipsGrid();
  renderSamplesList();
  renderSettingsForm();
}

function renderStatusChips() {
  const box = els.statusChips;
  box.innerHTML = "";

  const enabled = state.settings.enable !== false;
  const totalClips = state.voices.reduce((acc, v) => acc + (v.total_clips || 0), 0);
  const enabledClips = state.voices.reduce((acc, v) => acc + (v.enabled_clips || 0), 0);

  const c1 = document.createElement("span");
  c1.className = "chip " + (enabled ? "chip-ok" : "chip-warn");
  c1.textContent = enabled ? "● 插件已启用" : "○ 插件已关闭";
  box.appendChild(c1);

  const c2 = document.createElement("span");
  c2.className = "chip";
  c2.textContent = `音色包 ${state.voices.length} 个`;
  box.appendChild(c2);

  const c3 = document.createElement("span");
  c3.className = "chip";
  c3.textContent = `有效语音 ${enabledClips} / ${totalClips} 条`;
  box.appendChild(c3);

  const c4 = document.createElement("span");
  c4.className = "chip " + (state.settings.api_key_configured ? "chip-ok" : "chip-warn");
  c4.textContent = state.settings.api_key_configured ? "API Key 已配置" : "API Key 未配置";
  box.appendChild(c4);
}

/* ------------------------------------------------------------------ */
/* 音色选择器与音色概览卡                                              */
/* ------------------------------------------------------------------ */

function renderVoiceSelector() {
  const sel = els.voiceSelect;
  sel.innerHTML = "";
  state.voices.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.id;
    const defTag = v.is_default ? " [默认]" : "";
    const enTag = v.enabled === false ? " (已禁用)" : "";
    opt.textContent = `${v.name} (${v.id})${defTag}${enTag} — ${v.enabled_clips || 0}/${v.total_clips || 0} 条`;
    if (v.id === state.selectedVoiceId) opt.selected = true;
    sel.appendChild(opt);
  });

  const cur = getCurrentVoice();
  els.btnSetDefaultVoice.disabled = !cur || cur.is_default;
  els.btnDeleteVoice.disabled = !cur || state.voices.length <= 1;
}

function renderVoiceProfileCard() {
  const box = els.voiceProfileCard;
  box.innerHTML = "";
  const v = getCurrentVoice();
  if (!v) return;

  const left = document.createElement("div");
  left.className = "vp-meta-list";

  const addMeta = (label, valueHtml, badgeClass = "") => {
    const item = document.createElement("div");
    item.className = "vp-meta-item";
    const l = document.createElement("span");
    l.className = "vp-meta-label";
    l.textContent = `${label}：`;
    item.appendChild(l);
    const val = document.createElement("span");
    if (badgeClass) val.className = `badge ${badgeClass}`;
    val.textContent = valueHtml;
    item.appendChild(val);
    left.appendChild(item);
  };

  addMeta("状态", v.enabled !== false ? "启用中" : "已禁用", v.enabled !== false ? "badge-ok" : "badge-danger");
  addMeta("固定台词", `「${v.chirp_text || "菲比啾比"}」`);

  const engName = (state.engines[v.engine_type] && state.engines[v.engine_type].name) || v.engine_type || "aliyun";
  addMeta("合成引擎", engName);

  const ec = v.engine_config || {};
  const sourceDesc = ec.voice_id
    ? `复刻ID: ${ec.voice_id}`
    : ec.sample_name
      ? `克隆样本: ${ec.sample_name}`
      : ec.sample_url
        ? "公网音频URL克隆"
        : `系统音色: ${ec.default_voice || state.settings.default_voice || "longanhuan_v3.1"}`;
  addMeta("音色来源", sourceDesc);

  box.appendChild(left);

  const right = document.createElement("div");
  right.className = "vp-personas";
  const pLabel = document.createElement("span");
  pLabel.className = "vp-meta-label";
  pLabel.textContent = "绑定人设：";
  right.appendChild(pLabel);

  const personas = Array.isArray(v.persona_ids) ? v.persona_ids : [];
  if (personas.length === 0) {
    const b = document.createElement("span");
    b.className = "badge badge-muted";
    b.textContent = v.is_default ? "全部人设（默认兜底）" : "未绑定人设";
    right.appendChild(b);
  } else {
    personas.forEach((p) => {
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = p;
      right.appendChild(b);
    });
  }
  box.appendChild(right);
}

/* ------------------------------------------------------------------ */
/* 左侧情绪分类导航 + 右侧情绪详情栏                                   */
/* ------------------------------------------------------------------ */

function renderMoodSidebar() {
  const list = els.moodNavList;
  list.innerHTML = "";
  const v = getCurrentVoice();
  if (!v) return;

  // 「全部情绪」入口
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className =
    "mood-nav-item" + (state.selectedMood === "__all__" ? " mood-nav-item-active" : "");
  const allName = document.createElement("div");
  allName.className = "mood-nav-name";
  const allTitle = document.createElement("span");
  allTitle.className = "mood-nav-title";
  allTitle.textContent = "📂 全部情绪语音";
  const allSub = document.createElement("span");
  allSub.className = "mood-nav-sub";
  allSub.textContent = `共 ${Object.keys(v.moods || {}).length} 个情绪分类`;
  allName.appendChild(allTitle);
  allName.appendChild(allSub);
  const allCount = document.createElement("span");
  allCount.className = "mood-nav-count";
  allCount.textContent = `${v.enabled_clips || 0}/${v.total_clips || 0}`;
  allBtn.appendChild(allName);
  allBtn.appendChild(allCount);
  allBtn.addEventListener("click", () => {
    state.selectedMood = "__all__";
    state.selectedClipIds.clear();
    renderMoodSidebar();
    renderMoodDetailBar();
    renderClipsGrid();
  });
  list.appendChild(allBtn);

  const moodStats = v.mood_stats || v.moods || {};
  Object.entries(moodStats).forEach(([mk, m]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "mood-nav-item" +
      (state.selectedMood === mk ? " mood-nav-item-active" : "") +
      (m.enabled === false ? " mood-nav-item-disabled" : "");

    const nameWrap = document.createElement("div");
    nameWrap.className = "mood-nav-name";
    const title = document.createElement("span");
    title.className = "mood-nav-title";
    title.textContent = `${m.label || mk} (${mk})`;
    const sub = document.createElement("span");
    sub.className = "mood-nav-sub";
    const kwCount = Array.isArray(m.keywords) ? m.keywords.length : 0;
    sub.textContent = m.enabled === false ? "已禁用" : `关键词 ${kwCount} 个`;
    nameWrap.appendChild(title);
    nameWrap.appendChild(sub);

    const countBadge = document.createElement("span");
    countBadge.className = "mood-nav-count";
    countBadge.textContent = `${m.enabled_clips ?? 0}/${m.total_clips ?? 0}`;

    btn.appendChild(nameWrap);
    btn.appendChild(countBadge);
    btn.addEventListener("click", () => {
      state.selectedMood = mk;
      state.selectedClipIds.clear();
      renderMoodSidebar();
      renderMoodDetailBar();
      renderClipsGrid();
    });
    list.appendChild(btn);
  });
}

function renderMoodDetailBar() {
  const bar = els.moodDetailBar;
  bar.innerHTML = "";
  const v = getCurrentVoice();
  if (!v) return;

  const info = document.createElement("div");
  info.className = "mood-detail-info";

  if (state.selectedMood === "__all__") {
    const h3 = document.createElement("h3");
    h3.textContent = `全部情绪语音（共 ${state.clips.length} 条）`;
    const desc = document.createElement("p");
    desc.className = "mood-detail-desc";
    desc.textContent =
      "在左侧点击任意具体情绪分类，即可编辑该情绪的合成语气指令（Instruction）与触发关键词，或向该情绪目录定向上传/生成语音。同一情绪下有多条启用语音时，发送时将随机挑选一条。";
    info.appendChild(h3);
    info.appendChild(desc);
    bar.appendChild(info);
    return;
  }

  const m = (v.moods || {})[state.selectedMood];
  if (!m) return;

  const h3 = document.createElement("h3");
  h3.textContent = `${m.label || state.selectedMood} (${state.selectedMood})`;
  const statusBadge = document.createElement("span");
  statusBadge.className = "badge " + (m.enabled !== false ? "badge-ok" : "badge-danger");
  statusBadge.textContent = m.enabled !== false ? "情绪已启用" : "情绪已禁用";
  h3.appendChild(statusBadge);
  info.appendChild(h3);

  const instLine = document.createElement("p");
  instLine.className = "mood-detail-desc";
  instLine.textContent = `🎭 合成语气指令：${m.instruction || "（未设置）"}`;
  info.appendChild(instLine);

  const kwLine = document.createElement("p");
  kwLine.className = "mood-detail-desc";
  const kws = Array.isArray(m.keywords) && m.keywords.length ? m.keywords.join("、") : "（无关键词，仅随机或固定命中）";
  kwLine.textContent = `🔑 触发关键词：${kws}`;
  info.appendChild(kwLine);

  bar.appendChild(info);

  const actions = document.createElement("div");
  actions.className = "pack-actions";
  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "btn btn-sm";
  editBtn.textContent = "⚙️ 编辑该情绪指令与关键词";
  editBtn.addEventListener("click", () => openMoodDialog(state.selectedMood));
  actions.appendChild(editBtn);
  bar.appendChild(actions);
}

/* ------------------------------------------------------------------ */
/* 右侧语音卡片网格与批量操作                                          */
/* ------------------------------------------------------------------ */

function updateBatchButtons() {
  const count = state.selectedClipIds.size;
  els.btnBatchEnable.disabled = count === 0;
  els.btnBatchDisable.disabled = count === 0;
  els.btnBatchDelete.disabled = count === 0;
  const visible = getFilteredClips();
  els.chkSelectAll.checked =
    visible.length > 0 && visible.every((c) => state.selectedClipIds.has(c.id));
}

function renderClipsGrid() {
  const grid = els.clipsGrid;
  grid.innerHTML = "";
  const v = getCurrentVoice();
  const visible = getFilteredClips();
  updateBatchButtons();

  if (!visible.length) {
    els.clipsEmpty.hidden = false;
    els.clipsEmpty.textContent =
      state.selectedMood === "__all__"
        ? "当前音色还没有任何情绪语音，点击上方「⚡ 一键补齐各情绪语音」或「✨ 引擎生成新语音」即可自动合成！"
        : `当前「${state.selectedMood}」情绪目录下还没有语音文件，可点击「✨ 引擎生成新语音」或「📤 上传本地语音」添加。`;
    return;
  }
  els.clipsEmpty.hidden = true;

  const moodsMap = (v && v.moods) || {};

  visible.forEach((clip) => {
    const card = document.createElement("div");
    card.className =
      "clip-card" +
      (clip.enabled === false ? " clip-card-disabled" : "") +
      (state.selectedClipIds.has(clip.id) ? " clip-card-selected" : "");
    card.setAttribute("role", "listitem");

    // 顶部：复选框 + 文件名 + 状态徽章
    const top = document.createElement("div");
    top.className = "clip-card-top";

    const titleRow = document.createElement("label");
    titleRow.className = "clip-card-title-row";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = state.selectedClipIds.has(clip.id);
    chk.addEventListener("change", () => {
      if (chk.checked) state.selectedClipIds.add(clip.id);
      else state.selectedClipIds.delete(clip.id);
      card.classList.toggle("clip-card-selected", chk.checked);
      updateBatchButtons();
    });
    const fname = document.createElement("span");
    fname.className = "clip-filename";
    fname.title = clip.filename;
    fname.textContent = clip.filename;
    titleRow.appendChild(chk);
    titleRow.appendChild(fname);
    top.appendChild(titleRow);

    const badges = document.createElement("div");
    badges.className = "clip-badges";
    const moodBadge = document.createElement("span");
    moodBadge.className = "badge";
    moodBadge.textContent = clip.mood_label || clip.mood;
    badges.appendChild(moodBadge);

    const stateBadge = document.createElement("span");
    stateBadge.className = "badge " + (clip.enabled !== false ? "badge-ok" : "badge-muted");
    stateBadge.textContent = clip.enabled !== false ? "已启用" : "已禁用";
    badges.appendChild(stateBadge);
    top.appendChild(badges);
    card.appendChild(top);

    // 中部：台词、语气指令、来源与文件大小
    const body = document.createElement("div");
    body.className = "clip-body";

    const textLine = document.createElement("div");
    textLine.className = "clip-text-line";
    textLine.textContent = `台词：「${clip.text || "菲比啾比"}」`;
    body.appendChild(textLine);

    if (clip.instruction) {
      const instLine = document.createElement("div");
      instLine.className = "clip-inst-line";
      instLine.title = clip.instruction;
      instLine.textContent = `语气：${clip.instruction}`;
      body.appendChild(instLine);
    }

    const srcMap = { engine: "引擎生成", upload: "手动上传", migrated: "旧版迁移", local: "本地文件" };
    const metaLine = document.createElement("div");
    metaLine.className = "clip-meta-line";
    metaLine.textContent = `${srcMap[clip.source] || clip.source} · ${formatSize(clip.size)} · ${formatTime(clip.created_at)}`;
    body.appendChild(metaLine);
    card.appendChild(body);

    // 底部：试听、启停切换、跨情绪移动、下载、删除
    const actions = document.createElement("div");
    actions.className = "clip-actions";

    const leftActs = document.createElement("div");
    leftActs.className = "clip-actions-left";

    const playBtn = document.createElement("button");
    playBtn.type = "button";
    playBtn.className = "btn btn-sm btn-primary";
    playBtn.textContent = "▶ 试听";
    playBtn.addEventListener("click", () => playClip(clip, playBtn));
    leftActs.appendChild(playBtn);

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn btn-sm";
    toggleBtn.textContent = clip.enabled !== false ? "禁用" : "启用";
    toggleBtn.addEventListener("click", () => toggleClip(clip, clip.enabled === false));
    leftActs.appendChild(toggleBtn);

    actions.appendChild(leftActs);

    const rightActs = document.createElement("div");
    rightActs.className = "clip-actions-right";

    // 移动情绪下拉框
    const moveSel = document.createElement("select");
    moveSel.className = "clip-move-select";
    moveSel.title = "移动到其他情绪目录";
    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = "移动至...";
    moveSel.appendChild(defaultOpt);
    Object.entries(moodsMap).forEach(([mk, mInfo]) => {
      if (mk === clip.mood) return;
      const opt = document.createElement("option");
      opt.value = mk;
      opt.textContent = `${mInfo.label || mk} (${mk})`;
      moveSel.appendChild(opt);
    });
    moveSel.addEventListener("change", () => {
      if (moveSel.value) moveClip(clip, moveSel.value);
    });
    rightActs.appendChild(moveSel);

    const dlBtn = document.createElement("button");
    dlBtn.type = "button";
    dlBtn.className = "btn btn-sm";
    dlBtn.title = "下载音频文件";
    dlBtn.textContent = "⬇";
    dlBtn.addEventListener("click", () => downloadClip(clip));
    rightActs.appendChild(dlBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn btn-sm btn-danger";
    delBtn.title = "删除该条语音";
    delBtn.textContent = "删除";
    delBtn.addEventListener("click", () => deleteClip(clip));
    rightActs.appendChild(delBtn);

    actions.appendChild(rightActs);
    card.appendChild(actions);

    grid.appendChild(card);
  });
}

/* ------------------------------------------------------------------ */
/* 在线试听与语音操作交互                                              */
/* ------------------------------------------------------------------ */

async function playClip(clip, btn) {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "加载中…";
  setBanner("");
  try {
    const res = await bridge.apiGet("clips/audio", {
      voice_id: clip.voice_id,
      mood: clip.mood,
      filename: clip.filename,
    });
    if (!res || !res.audio) throw new Error("未获取到音频数据");
    els.playerTitle.textContent = `[${clip.mood_label || clip.mood}] ${clip.filename} — 「${clip.text || "菲比啾比"}」`;
    els.player.src = res.audio;
    els.playerBar.hidden = false;
    try {
      els.player.currentTime = 0;
      await els.player.play();
    } catch {
      /* 浏览器自动播放受限时用户可手动点击播放栏 */
    }
  } catch (err) {
    setBanner(err.message || String(err), "error");
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function downloadClip(clip) {
  try {
    await bridge.download(
      "clips/download",
      {
        voice_id: clip.voice_id,
        mood: clip.mood,
        filename: clip.filename,
      },
      clip.filename,
    );
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

async function toggleClip(clip, nextEnabled) {
  setBanner("");
  try {
    const data = await bridge.apiPost("clips/toggle", {
      voice_id: clip.voice_id,
      mood: clip.mood,
      filename: clip.filename,
      enabled: nextEnabled,
    });
    applyOverview(data);
    setBanner(`已${nextEnabled ? "启用" : "禁用"}语音 ${clip.filename}`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

async function moveClip(clip, toMood) {
  setBanner("");
  try {
    const data = await bridge.apiPost("clips/move", {
      voice_id: clip.voice_id,
      from_mood: clip.mood,
      to_mood: toMood,
      filename: clip.filename,
    });
    applyOverview(data);
    setBanner(`已将 ${clip.filename} 移动到「${toMood}」目录`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

async function deleteClip(clip) {
  const ok = await askConfirm(`确定要永久删除语音文件「${clip.filename}」吗？`);
  if (!ok) return;
  setBanner("");
  try {
    const data = await bridge.apiPost("clips/delete", {
      voice_id: clip.voice_id,
      mood: clip.mood,
      filename: clip.filename,
    });
    state.selectedClipIds.delete(clip.id);
    applyOverview(data);
    setBanner(`已删除语音 ${clip.filename}`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

async function batchToggleSelected(enabled) {
  const ids = Array.from(state.selectedClipIds);
  if (!ids.length) return;
  setBanner("");
  try {
    const data = await bridge.apiPost("clips/batch_toggle", {
      voice_id: state.selectedVoiceId,
      clip_ids: ids,
      enabled,
    });
    applyOverview(data);
    setBanner(`已批量${enabled ? "启用" : "禁用"} ${data.updated || ids.length} 条语音`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

async function batchDeleteSelected() {
  const ids = Array.from(state.selectedClipIds);
  if (!ids.length) return;
  const ok = await askConfirm(`确定要删除选中的 ${ids.length} 条语音文件吗？此操作不可撤销。`);
  if (!ok) return;
  setBanner("");
  try {
    const data = await bridge.apiPost("clips/batch_delete", {
      voice_id: state.selectedVoiceId,
      clip_ids: ids,
    });
    state.selectedClipIds.clear();
    applyOverview(data);
    setBanner(`已批量删除 ${data.deleted || ids.length} 条语音`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

async function uploadClipFile(file) {
  const v = getCurrentVoice();
  if (!v) return;
  const targetMood =
    state.selectedMood && state.selectedMood !== "__all__"
      ? state.selectedMood
      : Object.keys(v.moods || {})[0] || "happy";

  setBanner(`正在上传语音 ${file.name} 到「${targetMood}」目录…`, "info");
  try {
    const b64 = await fileToBase64(file);
    const res = await bridge.apiPost("clips/upload", {
      voice_id: v.id,
      mood: targetMood,
      filename: file.name,
      data_base64: b64,
    });
    if (res && res.overview) applyOverview(res.overview);
    setBanner(`已成功上传语音 ${(res.clip && res.clip.filename) || file.name}`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

/* ------------------------------------------------------------------ */
/* 引擎合成任务弹窗与轮询                                              */
/* ------------------------------------------------------------------ */

function openGenDialog() {
  const v = getCurrentVoice();
  if (!v) return;
  els.genDialogError.hidden = true;
  els.gdMood.innerHTML = "";

  const moods = v.moods || {};
  Object.entries(moods).forEach(([mk, m]) => {
    const opt = document.createElement("option");
    opt.value = mk;
    opt.textContent = `${m.label || mk} (${mk})`;
    els.gdMood.appendChild(opt);
  });

  const initialMood =
    state.selectedMood !== "__all__" && moods[state.selectedMood]
      ? state.selectedMood
      : Object.keys(moods)[0] || "happy";
  els.gdMood.value = initialMood;
  els.gdCount.value = "1";
  els.gdText.value = v.chirp_text || "菲比啾比";
  els.gdInstruction.value = (moods[initialMood] && moods[initialMood].instruction) || "";

  els.gdMood.onchange = () => {
    const m = moods[els.gdMood.value];
    if (m) els.gdInstruction.value = m.instruction || "";
  };

  els.genDialog.showModal();
}

async function pollGenerateJob(jobId) {
  for (let i = 0; i < 120; i += 1) {
    await sleep(i === 0 ? 400 : 1000);
    const res = await bridge.apiGet("clips/generate/result", { job_id: jobId });
    if (!res) continue;
    if (res.status === "done" || res.status === "error") {
      return res;
    }
  }
  throw new Error("引擎合成任务轮询超时，请稍后刷新查看");
}

async function submitGenDialog() {
  const v = getCurrentVoice();
  if (!v) return;
  const mood = els.gdMood.value;
  const count = Number(els.gdCount.value || 1);
  const text = els.gdText.value.trim();
  const instruction = els.gdInstruction.value.trim();

  els.btnGenDialogSubmit.disabled = true;
  const origLabel = els.btnGenDialogSubmit.textContent;
  els.btnGenDialogSubmit.textContent = "正在调用引擎合成…";
  els.genDialogError.hidden = true;

  try {
    const started = await bridge.apiPost("clips/generate", {
      voice_id: v.id,
      mood,
      count,
      text,
      instruction,
    });
    const result = await pollGenerateJob(started.job_id);
    if (result.overview) applyOverview(result.overview);
    if (result.status === "error") {
      throw new Error(result.error || "合成失败");
    }
    els.genDialog.close();
    const genCount = (result.generated && result.generated.length) || count;
    const warnMsg =
      result.errors && result.errors.length ? `（部分失败：${result.errors[0]}）` : "";
    setBanner(`✨ 成功合成并入库 ${genCount} 条「${mood}」情绪语音！${warnMsg}`, "ok");
  } catch (err) {
    els.genDialogError.hidden = false;
    els.genDialogError.textContent = err.message || String(err);
  } finally {
    els.btnGenDialogSubmit.disabled = false;
    els.btnGenDialogSubmit.textContent = origLabel;
  }
}

async function generateAllMoods() {
  const v = getCurrentVoice();
  if (!v) return;
  const ok = await askConfirm(
    `将为音色「${v.name}」下所有已启用的情绪分类各合成 1 条新语音，是否继续？`,
  );
  if (!ok) return;

  els.btnGenAllMoods.disabled = true;
  const orig = els.btnGenAllMoods.textContent;
  els.btnGenAllMoods.textContent = "⚡ 正在批量合成中…";
  setBanner("正在调用语音合成引擎为各情绪分类生成语音，请稍候…", "info");

  try {
    const started = await bridge.apiPost("clips/generate", {
      voice_id: v.id,
      mood: "__all__",
      count: 1,
    });
    const result = await pollGenerateJob(started.job_id);
    if (result.overview) applyOverview(result.overview);
    if (result.status === "error") {
      throw new Error(result.error || "批量合成失败");
    }
    const genCount = (result.generated && result.generated.length) || 0;
    setBanner(`⚡ 已成功为音色「${v.name}」合成 ${genCount} 条情绪语音！`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  } finally {
    els.btnGenAllMoods.disabled = false;
    els.btnGenAllMoods.textContent = orig;
  }
}

/* ------------------------------------------------------------------ */
/* 音色新建 / 编辑弹窗与多人设绑定                                     */
/* ------------------------------------------------------------------ */

function parsePersonasInput() {
  return els.vdPersonasInput.value
    .split(/[,，\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function renderPersonaChipsInDialog() {
  const box = els.vdPersonaChips;
  box.innerHTML = "";
  const selected = new Set(parsePersonasInput());
  const allCandidates = Array.from(new Set([...state.personas, ...selected]));

  if (!allCandidates.length) {
    const hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = "（当前未检测到系统预设人设，可直接在下方输入框手动填写人设名称）";
    box.appendChild(hint);
    return;
  }

  allCandidates.forEach((pName) => {
    const chip = document.createElement("button");
    chip.type = "button";
    const active = selected.has(pName);
    chip.className = "persona-chip" + (active ? " persona-chip-active" : "");
    chip.textContent = active ? `✓ ${pName}` : `+ ${pName}`;
    chip.addEventListener("click", () => {
      const cur = new Set(parsePersonasInput());
      if (cur.has(pName)) cur.delete(pName);
      else cur.add(pName);
      els.vdPersonasInput.value = Array.from(cur).join(", ");
      renderPersonaChipsInDialog();
    });
    box.appendChild(chip);
  });
}

function populateSampleDropdown(selectedName = "") {
  const sel = els.vdSampleName;
  sel.innerHTML = "";
  const emptyOpt = document.createElement("option");
  emptyOpt.value = "";
  emptyOpt.textContent = "（不使用本地克隆样本）";
  sel.appendChild(emptyOpt);

  state.samples.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = `${s.name} (${formatSize(s.size)})`;
    if (s.name === selectedName) opt.selected = true;
    sel.appendChild(opt);
  });
}

function openVoiceDialog(voice = null) {
  editingVoiceId = voice ? voice.id : null;
  els.voiceDialogError.hidden = true;
  els.voiceDialogTitle.textContent = voice ? `编辑音色：${voice.name}` : "新建基础音色";

  els.vdId.value = voice ? voice.id : "";
  els.vdId.disabled = Boolean(voice);
  els.vdName.value = voice ? voice.name : "";
  els.vdChirpText.value = voice ? voice.chirp_text || "菲比啾比" : "菲比啾比";
  els.vdEngineType.value = voice ? voice.engine_type || "aliyun" : "aliyun";
  els.vdEnabled.checked = voice ? voice.enabled !== false : true;
  els.vdIsDefault.checked = voice ? Boolean(voice.is_default) : false;

  const personas = voice && Array.isArray(voice.persona_ids) ? voice.persona_ids : [];
  els.vdPersonasInput.value = personas.join(", ");
  renderPersonaChipsInDialog();

  const ec = (voice && voice.engine_config) || {};
  populateSampleDropdown(ec.sample_name || "");
  els.vdSampleUrl.value = ec.sample_url || "";
  els.vdVoiceId.value = ec.voice_id || "";
  els.vdDefaultVoice.value = ec.default_voice || "";
  els.vdModel.value = ec.model || "";
  els.vdVoicePrefix.value = ec.voice_prefix || (voice ? voice.id.slice(0, 10) : "phoebe");

  els.voiceDialog.showModal();
}

async function saveVoiceDialog() {
  const name = els.vdName.value.trim();
  if (!name) {
    els.voiceDialogError.hidden = false;
    els.voiceDialogError.textContent = "请填写音色显示名称";
    return;
  }

  const payload = {
    id: editingVoiceId || els.vdId.value.trim(),
    name,
    chirp_text: els.vdChirpText.value.trim() || "菲比啾比",
    engine_type: els.vdEngineType.value,
    enabled: els.vdEnabled.checked,
    is_default: els.vdIsDefault.checked,
    persona_ids: parsePersonasInput(),
    engine_config: {
      sample_name: els.vdSampleName.value,
      sample_url: els.vdSampleUrl.value.trim(),
      voice_id: els.vdVoiceId.value.trim(),
      default_voice: els.vdDefaultVoice.value.trim(),
      model: els.vdModel.value.trim(),
      voice_prefix: els.vdVoicePrefix.value.trim() || "phoebe",
    },
  };

  els.btnVoiceDialogSave.disabled = true;
  try {
    const ep = editingVoiceId ? "voices/update" : "voices/create";
    const data = await bridge.apiPost(ep, payload);
    applyOverview(data);
    els.voiceDialog.close();
    setBanner(`已保存音色「${name}」`, "ok");
  } catch (err) {
    els.voiceDialogError.hidden = false;
    els.voiceDialogError.textContent = err.message || String(err);
  } finally {
    els.btnVoiceDialogSave.disabled = false;
  }
}

async function setDefaultVoice() {
  const v = getCurrentVoice();
  if (!v) return;
  try {
    const data = await bridge.apiPost("voices/update", {
      id: v.id,
      is_default: true,
    });
    applyOverview(data);
    setBanner(`已将「${v.name}」设为默认音色包`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

async function deleteCurrentVoice() {
  const v = getCurrentVoice();
  if (!v) return;
  const ok = await askConfirm(
    `确定要删除音色包「${v.name} (${v.id})」及其下属的所有情绪语音文件吗？此操作不可恢复。`,
  );
  if (!ok) return;
  try {
    const data = await bridge.apiPost("voices/delete", { id: v.id });
    applyOverview(data);
    setBanner(`已删除音色包「${v.name}」`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

/* ------------------------------------------------------------------ */
/* 情绪分类新建 / 编辑弹窗                                              */
/* ------------------------------------------------------------------ */

function openMoodDialog(moodKey = null) {
  const v = getCurrentVoice();
  if (!v) return;
  editingMoodKey = moodKey;
  els.moodDialogError.hidden = true;

  const existing = moodKey && v.moods ? v.moods[moodKey] : null;
  els.moodDialogTitle.textContent = existing
    ? `编辑情绪分类：${existing.label || moodKey}`
    : "新增情绪分类";

  els.mdKey.value = moodKey || "";
  els.mdKey.disabled = Boolean(existing);
  els.mdLabel.value = existing ? existing.label || moodKey : "";
  els.mdEnabled.checked = existing ? existing.enabled !== false : true;
  els.mdInstruction.value = existing ? existing.instruction || "" : "";
  els.mdKeywords.value =
    existing && Array.isArray(existing.keywords) ? existing.keywords.join(", ") : "";

  els.btnMoodDialogDelete.hidden = !existing || Object.keys(v.moods || {}).length <= 1;
  els.moodDialog.showModal();
}

async function saveMoodDialog() {
  const v = getCurrentVoice();
  if (!v) return;
  const key = (editingMoodKey || els.mdKey.value).trim();
  const label = els.mdLabel.value.trim();
  if (!key || !label) {
    els.moodDialogError.hidden = false;
    els.moodDialogError.textContent = "请填写情绪英文标识和中文名称";
    return;
  }

  els.btnMoodDialogSave.disabled = true;
  try {
    const data = await bridge.apiPost("moods/save", {
      voice_id: v.id,
      mood: key,
      label,
      enabled: els.mdEnabled.checked,
      instruction: els.mdInstruction.value.trim(),
      keywords: els.mdKeywords.value,
    });
    state.selectedMood = key;
    applyOverview(data);
    els.moodDialog.close();
    setBanner(`已保存情绪分类「${label} (${key})」`, "ok");
  } catch (err) {
    els.moodDialogError.hidden = false;
    els.moodDialogError.textContent = err.message || String(err);
  } finally {
    els.btnMoodDialogSave.disabled = false;
  }
}

async function deleteMoodFromDialog() {
  const v = getCurrentVoice();
  if (!v || !editingMoodKey) return;
  const ok = await askConfirm(
    `确定要删除情绪分类「${editingMoodKey}」以及该目录下的所有语音文件吗？`,
  );
  if (!ok) return;
  try {
    const data = await bridge.apiPost("moods/delete", {
      voice_id: v.id,
      mood: editingMoodKey,
    });
    state.selectedMood = "__all__";
    applyOverview(data);
    els.moodDialog.close();
    setBanner(`已删除情绪分类「${editingMoodKey}」`, "ok");
  } catch (err) {
    els.moodDialogError.hidden = false;
    els.moodDialogError.textContent = err.message || String(err);
  }
}

/* ------------------------------------------------------------------ */
/* 克隆参考音频样本管理                                                */
/* ------------------------------------------------------------------ */

function renderSamplesList() {
  const list = els.samplesList;
  list.innerHTML = "";
  if (!state.samples.length) {
    els.samplesEmpty.hidden = false;
    els.samplesEmpty.textContent =
      "尚未上传任何参考音频样本。点击右上角「📤 上传参考音频样本」上传一段 10~20 秒的清晰语音（wav/mp3），即可在音色配置中用于零样本音色克隆。";
    return;
  }
  els.samplesEmpty.hidden = true;

  state.samples.forEach((s) => {
    const row = document.createElement("div");
    row.className = "row";

    const main = document.createElement("div");
    main.className = "row-main";
    const name = document.createElement("div");
    name.className = "row-name";
    name.textContent = s.name;
    const meta = document.createElement("div");
    meta.className = "row-meta";
    const refs = Array.isArray(s.referenced_by) && s.referenced_by.length
      ? `被音色引用：${s.referenced_by.join("、")}`
      : "未被音色引用";
    meta.textContent = `${formatSize(s.size)} · ${formatTime(s.created_at)} · ${refs}`;
    main.appendChild(name);
    main.appendChild(meta);
    row.appendChild(main);

    const actions = document.createElement("div");
    actions.className = "row-actions";

    const playBtn = document.createElement("button");
    playBtn.type = "button";
    playBtn.className = "btn btn-sm";
    playBtn.textContent = "▶ 试听样本";
    playBtn.addEventListener("click", async () => {
      try {
        const res = await bridge.apiGet("samples/audio", { name: s.name });
        els.playerTitle.textContent = `[克隆参考样本] ${s.name}`;
        els.player.src = res.audio;
        els.playerBar.hidden = false;
        await els.player.play().catch(() => {});
      } catch (err) {
        setBanner(err.message || String(err), "error");
      }
    });
    actions.appendChild(playBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn btn-sm btn-danger";
    delBtn.textContent = "删除";
    delBtn.addEventListener("click", async () => {
      const ok = await askConfirm(`确定删除克隆参考样本「${s.name}」吗？`);
      if (!ok) return;
      try {
        const res = await bridge.apiPost("samples/delete", { name: s.name });
        state.samples = res.samples || [];
        renderSamplesList();
        setBanner(`已删除样本 ${s.name}`, "ok");
      } catch (err) {
        setBanner(err.message || String(err), "error");
      }
    });
    actions.appendChild(delBtn);

    row.appendChild(actions);
    list.appendChild(row);
  });
}

async function uploadSampleFile(file) {
  setBanner(`正在上传克隆参考音频样本 ${file.name}…`, "info");
  try {
    const b64 = await fileToBase64(file);
    const res = await bridge.apiPost("samples/upload", {
      filename: file.name,
      data_base64: b64,
    });
    state.samples = res.samples || [];
    renderSamplesList();
    setBanner(`已上传克隆样本 ${(res.sample && res.sample.name) || file.name}`, "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

/* ------------------------------------------------------------------ */
/* 全局设置与默认引擎表单                                              */
/* ------------------------------------------------------------------ */

function renderSettingsForm() {
  const s = state.settings || {};
  els.setEnable.checked = s.enable !== false;
  els.setAutoMood.checked = s.auto_mood !== false;
  els.setAutoGen.checked = s.auto_generate_when_empty !== false;
  els.setChirpText.value = s.chirp_text || "菲比啾比";
  els.setProbability.value = s.probability ?? 100;
  els.setCooldown.value = s.cooldown_seconds ?? 0;
  els.setSendMode.value = s.send_mode || "append";
  els.setFixedMood.value = s.fixed_mood || "random";
  els.setSkipPatterns.value = Array.isArray(s.skip_patterns) ? s.skip_patterns.join("\n") : "";

  els.setEngineType.value = s.engine_type || "aliyun";
  els.setApiKey.value = "";
  els.setApiKey.placeholder = s.api_key_configured
    ? "已配置密钥（留空保持不变，输入新值则覆盖）"
    : "请输入 DashScope 或对应引擎的 API Key";
  els.setApiBase.value = s.api_base || "https://dashscope.aliyuncs.com/api/v1";
  els.setModel.value = s.model || "qwen-audio-3.1-tts-flash";
  els.setDefaultVoice.value = s.default_voice || "longanhuan_v3.1";
  els.setVoiceId.value = s.voice_id || "";
  els.setFormat.value = s.format || "wav";
  els.setSampleRate.value = s.sample_rate ?? 24000;
  els.setVolume.value = s.volume ?? 50;
  els.setSpeechRate.value = s.speech_rate ?? 1.0;
  els.setPitch.value = s.pitch ?? 1.0;
}

async function saveGlobalSettings() {
  els.btnSaveSettings.disabled = true;
  const orig = els.btnSaveSettings.textContent;
  els.btnSaveSettings.textContent = "保存中…";
  setBanner("");
  try {
    const payload = {
      enable: els.setEnable.checked,
      auto_mood: els.setAutoMood.checked,
      auto_generate_when_empty: els.setAutoGen.checked,
      chirp_text: els.setChirpText.value.trim() || "菲比啾比",
      probability: Number(els.setProbability.value || 100),
      cooldown_seconds: Number(els.setCooldown.value || 0),
      send_mode: els.setSendMode.value,
      fixed_mood: els.setFixedMood.value,
      skip_patterns: els.setSkipPatterns.value,
      engine_type: els.setEngineType.value,
      api_key: els.setApiKey.value.trim(),
      api_base: els.setApiBase.value.trim(),
      model: els.setModel.value.trim(),
      default_voice: els.setDefaultVoice.value.trim(),
      voice_id: els.setVoiceId.value.trim(),
      format: els.setFormat.value,
      sample_rate: Number(els.setSampleRate.value || 24000),
      volume: Number(els.setVolume.value || 50),
      speech_rate: Number(els.setSpeechRate.value || 1.0),
      pitch: Number(els.setPitch.value || 1.0),
    };
    const data = await bridge.apiPost("settings/save", { settings: payload });
    applyOverview(data);
    setBanner("💾 全局设置与引擎参数已保存并立即生效！", "ok");
  } catch (err) {
    setBanner(err.message || String(err), "error");
  } finally {
    els.btnSaveSettings.disabled = false;
    els.btnSaveSettings.textContent = orig;
  }
}

/* ------------------------------------------------------------------ */
/* 标签页切换与初始化绑定                                              */
/* ------------------------------------------------------------------ */

const TABS = ["voices", "samples", "settings"];

function switchTab(tabName) {
  if (!TABS.includes(tabName)) return;
  const pairs = {
    voices: [els.tabVoicesBtn, els.tabVoices],
    samples: [els.tabSamplesBtn, els.tabSamples],
    settings: [els.tabSettingsBtn, els.tabSettings],
  };
  for (const [k, [btn, panel]] of Object.entries(pairs)) {
    const active = k === tabName;
    btn.classList.toggle("tab-active", active);
    panel.hidden = !active;
  }
  setBanner("");
}

async function reloadVoice(voiceId) {
  setBanner("");
  try {
    const data = await bridge.apiGet("overview", { voice_id: voiceId || "" });
    applyOverview(data);
  } catch (err) {
    setBanner(err.message || String(err), "error");
  }
}

function bindElementsAndEvents() {
  Object.assign(els, {
    banner: byId("banner"),
    statusChips: byId("status-chips"),
    playerBar: byId("player-bar"),
    playerBadge: byId("player-badge"),
    playerTitle: byId("player-title"),
    player: byId("player"),
    btnClosePlayer: byId("btn-close-player"),

    tabVoicesBtn: byId("tab-voices-btn"),
    tabSamplesBtn: byId("tab-samples-btn"),
    tabSettingsBtn: byId("tab-settings-btn"),
    tabVoices: byId("tab-voices"),
    tabSamples: byId("tab-samples"),
    tabSettings: byId("tab-settings"),

    voiceSelect: byId("voice-select"),
    btnNewVoice: byId("btn-new-voice"),
    btnEditVoice: byId("btn-edit-voice"),
    btnSetDefaultVoice: byId("btn-set-default-voice"),
    btnDeleteVoice: byId("btn-delete-voice"),
    voiceProfileCard: byId("voice-profile-card"),

    btnNewMood: byId("btn-new-mood"),
    moodNavList: byId("mood-nav-list"),
    moodDetailBar: byId("mood-detail-bar"),

    btnGenClip: byId("btn-gen-clip"),
    btnGenAllMoods: byId("btn-gen-all-moods"),
    btnUploadClip: byId("btn-upload-clip"),
    chkSelectAll: byId("chk-select-all"),
    btnBatchEnable: byId("btn-batch-enable"),
    btnBatchDisable: byId("btn-batch-disable"),
    btnBatchDelete: byId("btn-batch-delete"),
    clipsGrid: byId("clips-grid"),
    clipsEmpty: byId("clips-empty"),

    btnUploadSample: byId("btn-upload-sample"),
    samplesList: byId("samples-list"),
    samplesEmpty: byId("samples-empty"),

    btnSaveSettings: byId("btn-save-settings"),
    setEnable: byId("set-enable"),
    setAutoMood: byId("set-auto-mood"),
    setAutoGen: byId("set-auto-gen"),
    setChirpText: byId("set-chirp-text"),
    setProbability: byId("set-probability"),
    setCooldown: byId("set-cooldown"),
    setSendMode: byId("set-send-mode"),
    setFixedMood: byId("set-fixed-mood"),
    setSkipPatterns: byId("set-skip-patterns"),
    setEngineType: byId("set-engine-type"),
    setApiKey: byId("set-api-key"),
    setApiBase: byId("set-api-base"),
    setModel: byId("set-model"),
    setDefaultVoice: byId("set-default-voice"),
    setVoiceId: byId("set-voice-id"),
    setFormat: byId("set-format"),
    setSampleRate: byId("set-sample-rate"),
    setVolume: byId("set-volume"),
    setSpeechRate: byId("set-speech-rate"),
    setPitch: byId("set-pitch"),

    voiceDialog: byId("voice-dialog"),
    voiceDialogTitle: byId("voice-dialog-title"),
    voiceDialogError: byId("voice-dialog-error"),
    btnVoiceDialogClose: byId("btn-voice-dialog-close"),
    btnVoiceDialogCancel: byId("btn-voice-dialog-cancel"),
    btnVoiceDialogSave: byId("btn-voice-dialog-save"),
    vdId: byId("vd-id"),
    vdName: byId("vd-name"),
    vdChirpText: byId("vd-chirp-text"),
    vdEngineType: byId("vd-engine-type"),
    vdEnabled: byId("vd-enabled"),
    vdIsDefault: byId("vd-is-default"),
    vdPersonaChips: byId("vd-persona-chips"),
    vdPersonasInput: byId("vd-personas-input"),
    vdSampleName: byId("vd-sample-name"),
    vdSampleUrl: byId("vd-sample-url"),
    vdVoiceId: byId("vd-voice-id"),
    vdDefaultVoice: byId("vd-default-voice"),
    vdModel: byId("vd-model"),
    vdVoicePrefix: byId("vd-voice-prefix"),

    moodDialog: byId("mood-dialog"),
    moodDialogTitle: byId("mood-dialog-title"),
    moodDialogError: byId("mood-dialog-error"),
    btnMoodDialogClose: byId("btn-mood-dialog-close"),
    btnMoodDialogCancel: byId("btn-mood-dialog-cancel"),
    btnMoodDialogSave: byId("btn-mood-dialog-save"),
    btnMoodDialogDelete: byId("btn-mood-dialog-delete"),
    mdKey: byId("md-key"),
    mdLabel: byId("md-label"),
    mdEnabled: byId("md-enabled"),
    mdInstruction: byId("md-instruction"),
    mdKeywords: byId("md-keywords"),

    genDialog: byId("gen-dialog"),
    genDialogError: byId("gen-dialog-error"),
    btnGenDialogClose: byId("btn-gen-dialog-close"),
    btnGenDialogCancel: byId("btn-gen-dialog-cancel"),
    btnGenDialogSubmit: byId("btn-gen-dialog-submit"),
    gdMood: byId("gd-mood"),
    gdCount: byId("gd-count"),
    gdText: byId("gd-text"),
    gdInstruction: byId("gd-instruction"),

    confirmDialog: byId("confirm-dialog"),
    confirmMessage: byId("confirm-message"),
    btnConfirmCancel: byId("btn-confirm-cancel"),
    btnConfirmOk: byId("btn-confirm-ok"),
  });

  els.tabVoicesBtn.addEventListener("click", () => switchTab("voices"));
  els.tabSamplesBtn.addEventListener("click", () => switchTab("samples"));
  els.tabSettingsBtn.addEventListener("click", () => switchTab("settings"));

  els.btnClosePlayer.addEventListener("click", () => {
    els.player.pause();
    els.playerBar.hidden = true;
  });

  els.voiceSelect.addEventListener("change", () => {
    state.selectedVoiceId = els.voiceSelect.value;
    state.selectedMood = "__all__";
    state.selectedClipIds.clear();
    reloadVoice(state.selectedVoiceId);
  });

  els.btnNewVoice.addEventListener("click", () => openVoiceDialog(null));
  els.btnEditVoice.addEventListener("click", () => openVoiceDialog(getCurrentVoice()));
  els.btnSetDefaultVoice.addEventListener("click", setDefaultVoice);
  els.btnDeleteVoice.addEventListener("click", deleteCurrentVoice);

  els.btnNewMood.addEventListener("click", () => openMoodDialog(null));

  els.btnGenClip.addEventListener("click", openGenDialog);
  els.btnGenAllMoods.addEventListener("click", generateAllMoods);
  els.btnUploadClip.addEventListener("click", () =>
    pickFile((f) => uploadClipFile(f)),
  );

  els.chkSelectAll.addEventListener("change", () => {
    const visible = getFilteredClips();
    if (els.chkSelectAll.checked) {
      visible.forEach((c) => state.selectedClipIds.add(c.id));
    } else {
      visible.forEach((c) => state.selectedClipIds.delete(c.id));
    }
    renderClipsGrid();
  });

  els.btnBatchEnable.addEventListener("click", () => batchToggleSelected(true));
  els.btnBatchDisable.addEventListener("click", () => batchToggleSelected(false));
  els.btnBatchDelete.addEventListener("click", batchDeleteSelected);

  els.btnUploadSample.addEventListener("click", () =>
    pickFile((f) => uploadSampleFile(f)),
  );
  els.btnSaveSettings.addEventListener("click", saveGlobalSettings);

  // 音色弹窗
  els.btnVoiceDialogClose.addEventListener("click", () => els.voiceDialog.close());
  els.btnVoiceDialogCancel.addEventListener("click", () => els.voiceDialog.close());
  els.btnVoiceDialogSave.addEventListener("click", saveVoiceDialog);
  els.vdPersonasInput.addEventListener("input", renderPersonaChipsInDialog);

  // 情绪弹窗
  els.btnMoodDialogClose.addEventListener("click", () => els.moodDialog.close());
  els.btnMoodDialogCancel.addEventListener("click", () => els.moodDialog.close());
  els.btnMoodDialogSave.addEventListener("click", saveMoodDialog);
  els.btnMoodDialogDelete.addEventListener("click", deleteMoodFromDialog);

  // 引擎生成弹窗
  els.btnGenDialogClose.addEventListener("click", () => els.genDialog.close());
  els.btnGenDialogCancel.addEventListener("click", () => els.genDialog.close());
  els.btnGenDialogSubmit.addEventListener("click", submitGenDialog);

  // 确认弹窗
  els.btnConfirmCancel.addEventListener("click", () => settleConfirm(false));
  els.btnConfirmOk.addEventListener("click", () => settleConfirm(true));
  els.confirmDialog.addEventListener("cancel", (e) => {
    e.preventDefault();
    settleConfirm(false);
  });
}

async function main() {
  if (!bridge) {
    document.body.innerHTML =
      '<p style="padding:24px;font-family:sans-serif">此页面需要在 AstrBot WebUI 中打开。</p>';
    return;
  }

  bindElementsAndEvents();
  await bridge.ready();
  await reloadVoice("");
}

main();
