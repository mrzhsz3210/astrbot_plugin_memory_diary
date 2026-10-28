/* 记忆日记 · 活页日记本视图
 * 通过 window.AstrBotPluginView bridge 与插件后端通信（iframe 无 Dashboard cookie 权限）。
 */
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

const MODE_TEXT = {
  fresh: "签名钥匙已就位",
  same_key: "签名钥匙与本机一致",
  resign: "已用本机钥匙重签共存",
};

let users = [];
let currentKey = "";
let days = [];
let dayIdx = -1;
let loading = false;

/* ---------- 界面偏好：字号 / 字体（存后端，按面板账号分开） ---------- */
const FONT_FAMILIES = [
  { key: "kai", label: "楷体", css: '"STKaiti","KaiTi","楷体","Noto Serif SC",serif' },
  { key: "song", label: "宋体", css: '"SimSun","宋体","Noto Serif SC",serif' },
  { key: "sans", label: "黑体", css: '"Microsoft YaHei","微软雅黑","PingFang SC",sans-serif' },
];
const FONT_SIZE_MIN = 13;
const FONT_SIZE_MAX = 22;
let prefs = { font_size: 16, font_family: "kai", paper: "default", backdrop: "default" };

/* 查看密码只在本页面会话内缓存（不写磁盘、不进 localStorage） */
const sessionPw = {};
/* 自定义纸张 / 背景板 data URL 缓存 */
let paperDataUrl = "";
let backdropDataUrl = "";

function applyPrefs() {
  const root = document.documentElement.style;
  root.setProperty("--d-font-size", prefs.font_size + "px");
  const fam =
    FONT_FAMILIES.find((f) => f.key === prefs.font_family) || FONT_FAMILIES[0];
  root.setProperty("--d-font-family", fam.css);
  $("btnFontFamily").textContent = fam.label;
  $("btnFontFamily").title = "切换字体（当前：" + fam.label + "）";
}

let saveTimer = null;
function savePrefs() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      await bridge.apiPost("prefs/save", { prefs });
    } catch (e) {
      console.warn("偏好保存失败", e);
    }
  }, 400);
}

function changeFontSize(delta) {
  const next = Math.min(
    FONT_SIZE_MAX,
    Math.max(FONT_SIZE_MIN, prefs.font_size + delta)
  );
  if (next === prefs.font_size) return;
  prefs.font_size = next;
  applyPrefs();
  savePrefs();
}

function cycleFontFamily() {
  const i = FONT_FAMILIES.findIndex((f) => f.key === prefs.font_family);
  prefs.font_family = FONT_FAMILIES[(i + 1) % FONT_FAMILIES.length].key;
  applyPrefs();
  savePrefs();
}

async function loadPrefs() {
  try {
    const r = await bridge.apiGet("prefs");
    if (r.prefs && typeof r.prefs === "object") {
      if (Number.isInteger(r.prefs.font_size)) {
        prefs.font_size = Math.min(
          FONT_SIZE_MAX,
          Math.max(FONT_SIZE_MIN, r.prefs.font_size)
        );
      }
      if (FONT_FAMILIES.some((f) => f.key === r.prefs.font_family)) {
        prefs.font_family = r.prefs.font_family;
      }
      if (r.prefs.paper === "default" || r.prefs.paper === "custom") {
        prefs.paper = r.prefs.paper;
      }
      if (r.prefs.backdrop === "default" || r.prefs.backdrop === "custom") {
        prefs.backdrop = r.prefs.backdrop;
      }
    }
    if (typeof r.brand === "string" && r.brand) {
      $("brand").textContent = "📖 " + r.brand + "的日记本";
      document.title = "记忆日记 · " + r.brand + "的日记本";
    }
  } catch (e) {
    console.warn("偏好读取失败，用默认值", e);
  }
  applyPrefs();
}

/* ---------- 自定义纸张（书页纸纹）与背景板（书本背后的底衬），互不影响 ---------- */
function applyPaper() {
  const book = $("book");
  const root = document.documentElement.style;
  if (prefs.paper === "custom" && paperDataUrl) {
    root.setProperty("--d-paper-image", `url("${paperDataUrl}")`);
    book.classList.add("custom-paper");
  } else {
    root.removeProperty("--d-paper-image");
    book.classList.remove("custom-paper");
  }
  markMenuActive("paper", prefs.paper === "custom" && paperDataUrl);
}

function applyBackdrop() {
  const root = document.documentElement.style;
  if (prefs.backdrop === "custom" && backdropDataUrl) {
    root.setProperty("--d-backdrop-image", `url("${backdropDataUrl}")`);
    document.body.classList.add("custom-backdrop");
  } else {
    root.removeProperty("--d-backdrop-image");
    document.body.classList.remove("custom-backdrop");
  }
  markMenuActive("backdrop", prefs.backdrop === "custom" && backdropDataUrl);
}

/* 菜单里标出当前生效的那一项 */
function markMenuActive(kind, usingCustom) {
  const active = usingCustom ? "custom" : "default";
  for (const item of document.querySelectorAll(`.paper-item[data-kind="${kind}"]`)) {
    item.classList.toggle("active", item.dataset.paper === active);
  }
}

/* 读取后端已存的自定义图片；有图则解锁「使用自定义图片」菜单项 */
async function loadImageData(kind) {
  const useBtn = $(kind === "paper" ? "paperUseCustom" : "backdropUseCustom");
  try {
    const r = await bridge.apiGet(kind);
    if (r.found && r.data) {
      const url = "data:" + (r.mime || "image/png") + ";base64," + r.data;
      if (kind === "paper") paperDataUrl = url; else backdropDataUrl = url;
      useBtn.disabled = false;
    } else {
      if (kind === "paper") paperDataUrl = ""; else backdropDataUrl = "";
      useBtn.disabled = true;
      if (prefs[kind] === "custom") prefs[kind] = "default";
    }
  } catch (e) {
    console.warn(kind + " 图片读取失败", e);
    if (kind === "paper") paperDataUrl = ""; else backdropDataUrl = "";
  }
  if (kind === "paper") applyPaper(); else applyBackdrop();
}

function closeMenus() {
  $("paperMenu").hidden = true;
  $("backdropMenu").hidden = true;
  $("exportMenu").hidden = true;
}

function onImageMenuClick(kind, choice) {
  closeMenus();
  const apply = kind === "paper" ? applyPaper : applyBackdrop;
  const hasImage = kind === "paper" ? !!paperDataUrl : !!backdropDataUrl;
  if (choice === "default") {
    prefs[kind] = "default";
    apply();
    savePrefs();
  } else if (choice === "upload") {
    $(kind === "paper" ? "paperInput" : "backdropInput").click();
  } else if (choice === "custom") {
    if (!hasImage) return;
    prefs[kind] = "custom";
    apply();
    savePrefs();
  }
}

async function onImageFileChosen(kind, file) {
  if (!file) return;
  const label = kind === "paper" ? "纸张" : "背景板";
  const apply = kind === "paper" ? applyPaper : applyBackdrop;
  try {
    toast(`正在上传${label}图片…`);
    await bridge.upload(kind + "/upload", file);
    await loadImageData(kind);
    prefs[kind] = "custom";
    apply();
    savePrefs();
    toast(`${label}已更换`);
  } catch (e) {
    toast(e.message || `${label}上传失败`, 4000);
  }
}

function toast(msg, ms = 3000) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => (t.hidden = true), ms);
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* 日记正文渲染：分隔条（—— …… ——）染成淡色小字，其余保持原样 */
function renderContent(text) {
  return escapeHtml(text)
    .split("\n")
    .map((line) =>
      /^——.+——$/.test(line.trim())
        ? `<span class="seg-header">${line}</span>`
        : line
    )
    .join("\n");
}

async function loadUsers() {
  const r = await bridge.apiGet("users");
  users = r.users || [];
  const sel = $("userSelect");
  sel.innerHTML = "";
  for (const u of users) {
    const opt = document.createElement("option");
    opt.value = u.file_key;
    opt.textContent =
      `${u.protected ? "🔒 " : ""}${u.nickname}（${u.day_count} 天）`;
    sel.appendChild(opt);
  }
  const has = users.length > 0;
  $("book").hidden = !has;
  $("empty").hidden = has;
  $("emptyText").textContent = has
    ? ""
    : "还没有人写过日记——也可以点右上角「导入日记」装入迁移包";
  $("btnExport").disabled = !has;
  if (!has) {
    currentKey = "";
    return;
  }
  if (!users.some((u) => u.file_key === currentKey)) {
    currentKey = users[0].file_key;
  }
  sel.value = currentKey;
}

async function loadDays() {
  const u = users.find((x) => x.file_key === currentKey);
  $("ownerHint").hidden = !u || u.owner_known;
  $("ownerHint").textContent = u && !u.owner_known
    ? "这份日记还没绑定主人 QQ，翻开时会请你输入一次（验签通过才接受）"
    : "";
  const r = await bridge.apiGet(`days/${currentKey}`);
  days = r.days || [];
  renderDayList();
  if (days.length === 0) {
    $("diaryHeader").innerHTML = "";
    $("diaryBody").textContent = "（这个文件里还没有日记）";
    $("pageInfo").textContent = "0 / 0";
    dayIdx = -1;
    return;
  }
  await showDay(days.length - 1, false);
}

function renderDayList() {
  const ul = $("dayList");
  ul.innerHTML = "";
  // 目录按时间倒序展示，最新的在最上面
  days.forEach((d, i) => {
    const li = document.createElement("li");
    li.dataset.i = i;
    li.innerHTML = `<span>${escapeHtml(d.label)}</span><span class="idx">第 ${i + 1} 页</span>`;
    if (i === dayIdx) li.classList.add("active");
    li.addEventListener("click", () => showDay(i, true));
    ul.appendChild(li);
  });
  ul.scrollTop = ul.scrollHeight;
}

/* 依次处理两道闸门：先绑定主人 QQ（旧 zip），再过查看密码。返回响应或 null。 */
async function fetchDayWithGates(dayStr) {
  const params = {};
  if (sessionPw[currentKey]) params.pw = sessionPw[currentKey];
  let r = await bridge.apiGet(`day/${currentKey}/${dayStr}`, params);

  if (r.need_owner) {
    const qq = prompt(
      "这份旧日记的说明里没有主人 QQ（文件名是加密的、反推不出）。\n" +
        "请输入日记主人的 QQ 号：会用它全量验签，通过才会显示。"
    );
    if (!qq) {
      toast("已取消");
      return null;
    }
    params.qq = qq.trim();
    try {
      r = await bridge.apiGet(`day/${currentKey}/${dayStr}`, params);
    } catch (e) {
      toast(e.message || "读取失败");
      return null;
    }
    const u = users.find((x) => x.file_key === currentKey);
    if (u) u.owner_known = true;
    $("ownerHint").hidden = true;
    toast("QQ 验签通过，已记住这份日记的主人");
    delete params.qq;
  }

  if (r.need_password) {
    const pw = prompt("这份日记设了查看密码，请输入：");
    if (!pw) {
      toast("已取消");
      return null;
    }
    params.pw = pw;
    try {
      r = await bridge.apiGet(`day/${currentKey}/${dayStr}`, params);
      sessionPw[currentKey] = pw;
    } catch (e) {
      if ((e.message || "").includes("PASSWORD_WRONG")) {
        toast("密码不对");
      } else {
        toast(e.message || "读取失败");
      }
      return null;
    }
  }
  return r;
}

async function showDay(i, animate = true) {
  if (loading || i < 0 || i >= days.length) return;
  loading = true;
  const page = $("pageRight");
  try {
    if (animate) {
      page.classList.remove("turn");
      void page.offsetWidth; // 重启动画
      page.classList.add("turn");
    }
    const r = await fetchDayWithGates(days[i].day);
    if (!r) return; // 用户取消或校验失败，提示已在函数内给出
    dayIdx = i;
    $("diaryHeader").innerHTML =
      `<span>${escapeHtml(days[i].label)}</span>` +
      `<span class="stamp">第 ${i + 1} / ${days.length} 页</span>`;
    if (r.tampered) {
      $("diaryBody").innerHTML =
        '<span class="tampered">⚠ 这一天的日记验签失败，疑似在插件之外被修改过，已拒绝展示。</span>';
    } else {
      $("diaryBody").innerHTML = renderContent(r.content || "（空白）");
    }
    $("pageInfo").textContent = `${i + 1} / ${days.length}`;
    renderDayListActive();
  } catch (e) {
    toast(e.message || "读取失败");
  } finally {
    loading = false;
  }
}

function renderDayListActive() {
  for (const li of $("dayList").children) {
    li.classList.toggle("active", Number(li.dataset.i) === dayIdx);
  }
}

/* ---------- 导出 / 导入 ---------- */
/* mode: "browser" 浏览器下载 ｜ "local" 存到本机下载文件夹并打开位置（面板须开在本机） */
async function doExport(mode) {
  const u = users.find((x) => x.file_key === currentKey);
  if (!u) return;
  let params = {};
  if (!u.owner_known) {
    const qq = prompt(
      "导出迁移包需要日记主人的 QQ 号（写入清单并用于验签）。\n请输入："
    );
    if (!qq) {
      toast("已取消导出");
      return;
    }
    params.qq = qq.trim();
  }
  if (sessionPw[currentKey]) params.pw = sessionPw[currentKey];

  const run = async () => {
    if (mode === "local") {
      const r = await bridge.apiGet(`export_local/${currentKey}`, params);
      return { filename: r.filename, local: true };
    }
    const r = await bridge.download(
      `export/${currentKey}`,
      params,
      "memory_diary_export.zip"
    );
    return { filename: r.filename, local: false };
  };
  const okToast = (r) =>
    toast(
      r.local
        ? `已存到「下载」文件夹并打开位置：${r.filename}`
        : `迁移包已下载：${r.filename}（含签名钥匙，请勿转发他人）`,
      4500
    );

  try {
    const r = await run();
    okToast(r);
    if (!u.owner_known) u.owner_known = true;
  } catch (e) {
    const msg = e.message || "";
    if (msg.includes("PASSWORD_REQUIRED")) {
      const pw = prompt("这份日记设了查看密码，导出前请输入：");
      if (!pw) {
        toast("已取消导出");
        return;
      }
      params.pw = pw;
      try {
        const r2 = await run();
        sessionPw[currentKey] = pw;
        okToast(r2);
      } catch (e2) {
        toast((e2.message || "").includes("PASSWORD_WRONG") ? "密码不对" : e2.message || "导出失败");
      }
    } else if (msg.includes("PASSWORD_WRONG")) {
      delete sessionPw[currentKey];
      toast("密码不对");
    } else if (msg.includes("OWNER_UNKNOWN")) {
      toast("QQ 号与日记签名对不上，导出被拒绝");
    } else if (msg.includes("REMOTE_PANEL")) {
      toast("管理面板不在服务器本机，请改用「下载到浏览器」", 4500);
    } else {
      toast(msg || "导出失败");
    }
  }
}

async function doImport(file) {
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    toast("请选择迁移包 zip 文件");
    return;
  }
  try {
    toast("正在验签并导入…");
    const r = await bridge.upload("import", file);
    toast(
      `导入完成：${r.imported} 天日记（${MODE_TEXT[r.mode] || r.mode}）`,
      4500
    );
    await loadUsers();
    if (currentKey) await loadDays();
  } catch (e) {
    toast(e.message || "导入失败", 5000);
  }
}

/* ---------- 启动 ---------- */
await bridge.ready();
$("btnFontSmaller").addEventListener("click", () => changeFontSize(-1));
$("btnFontLarger").addEventListener("click", () => changeFontSize(1));
$("btnFontFamily").addEventListener("click", cycleFontFamily);
await loadPrefs();
await loadImageData("paper");
await loadImageData("backdrop");
$("btnPaper").addEventListener("click", (e) => {
  e.stopPropagation();
  const menu = $("paperMenu");
  const willOpen = menu.hidden;
  closeMenus();
  menu.hidden = !willOpen;
});
$("btnBackdrop").addEventListener("click", (e) => {
  e.stopPropagation();
  const menu = $("backdropMenu");
  const willOpen = menu.hidden;
  closeMenus();
  menu.hidden = !willOpen;
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".paper-ctl")) closeMenus();
});
for (const item of document.querySelectorAll(".paper-item")) {
  item.addEventListener("click", () =>
    onImageMenuClick(item.dataset.kind, item.dataset.paper)
  );
}
$("paperInput").addEventListener("change", async (e) => {
  await onImageFileChosen("paper", e.target.files[0]);
  e.target.value = "";
});
$("backdropInput").addEventListener("change", async (e) => {
  await onImageFileChosen("backdrop", e.target.files[0]);
  e.target.value = "";
});
$("userSelect").addEventListener("change", async (e) => {
  currentKey = e.target.value;
  await loadDays();
});
$("btnRefresh").addEventListener("click", async () => {
  await loadUsers();
  if (currentKey) await loadDays();
  toast("已刷新");
});
$("btnExport").addEventListener("click", (e) => {
  e.stopPropagation();
  if ($("btnExport").disabled) return;
  const menu = $("exportMenu");
  const willOpen = menu.hidden;
  closeMenus();
  menu.hidden = !willOpen;
});
for (const item of document.querySelectorAll("#exportMenu .paper-item")) {
  item.addEventListener("click", () => {
    closeMenus();
    doExport(item.dataset.export);
  });
}
$("btnImport").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", async (e) => {
  await doImport(e.target.files[0]);
  e.target.value = "";
});
$("btnPrev").addEventListener("click", () => showDay(dayIdx - 1, true));
$("btnNext").addEventListener("click", () => showDay(dayIdx + 1, true));
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") showDay(dayIdx - 1, true);
  if (e.key === "ArrowRight") showDay(dayIdx + 1, true);
});

await loadUsers();
if (currentKey) await loadDays();
