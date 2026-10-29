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
/* 自定义纸张 / 背景板图片缓存。这里存的是 blob: URL，不是 base64 data URL：
 * Chromium/Edge 会把 CSS 自定义属性值截断到 1,000,000 字符，而一张 2MB 图片的
 * base64 约 3MB，直接塞进 --d-paper-image 会被截断成非法 URL，背景画不出来，
 * 页面看起来就只是“纸的边框消失了”。blob: URL 很短，不受这个限制。 */
let paperDataUrl = "";
let backdropDataUrl = "";

function dataUrlToObjectUrl(dataUrl) {
  try {
    if (typeof URL.createObjectURL !== "function") return dataUrl;
    const comma = dataUrl.indexOf(",");
    if (comma < 0) return "";
    const meta = dataUrl.slice(0, comma);
    const binary = atob(dataUrl.slice(comma + 1));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const mime = (meta.match(/^data:([^;]+)/) || [])[1] || "image/png";
    return URL.createObjectURL(new Blob([bytes], { type: mime }));
  } catch (e) {
    console.warn("自定义图片转换成 Blob 失败", e);
    return "";
  }
}

function setImageUrl(kind, url) {
  const old = kind === "paper" ? paperDataUrl : backdropDataUrl;
  if (old && old.startsWith("blob:")) {
    try {
      URL.revokeObjectURL(old);
    } catch (e) {
      /* 忽略释放失败 */
    }
  }
  if (kind === "paper") paperDataUrl = url; else backdropDataUrl = url;
}

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
    // 后端字段必须叫 image、不能叫 data：bridge 会把响应顶层的 data 当信封解包
    // （PluginViewPage.vue: response.data?.data ?? response.data），那样这里只拿到
    // base64 字符串，r.found / r.mime 全没了，上传成功也不换图。
    if (r.found && r.image) {
      const dataUrl = "data:" + (r.mime || "image/png") + ";base64," + r.image;
      const url = dataUrlToObjectUrl(dataUrl);
      setImageUrl(kind, url);
      useBtn.disabled = !url;
      if (!url && prefs[kind] === "custom") prefs[kind] = "default";
    } else {
      setImageUrl(kind, "");
      useBtn.disabled = true;
      if (prefs[kind] === "custom") prefs[kind] = "default";
    }
  } catch (e) {
    console.warn(kind + " 图片读取失败", e);
    setImageUrl(kind, "");
    useBtn.disabled = true;
    if (prefs[kind] === "custom") prefs[kind] = "default";
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

/* ---------- 页面内输入对话框 ----------
 * 插件视图 iframe 的 sandbox 是 "allow-scripts allow-forms allow-downloads"，
 * 没有 allow-modals：原生 window.prompt / confirm / alert 会被浏览器直接忽略
 * （返回 null，控制台报 Ignored call to prompt），所以查看密码和
 * 主人 QQ 都必须用页面里的输入框，不能依赖原生弹窗。
 */
let dialogResolve = null;

function setDialogError(msg) {
  const el = $("dialogError");
  el.textContent = msg || "";
  el.hidden = !msg;
}

function closeDialog(value) {
  $("dialogMask").hidden = true;
  const resolve = dialogResolve;
  dialogResolve = null;
  if (resolve) resolve(value);
}

function confirmDialog() {
  const input = $("dialogInput");
  const value = input.value.trim();
  if (!value) {
    setDialogError("请输入内容后再点确定");
    input.focus();
    return;
  }
  closeDialog(value);
}

function askDialog({
  title,
  message = "",
  password = false,
  placeholder = "",
  okText = "确定",
  error = "",
  inputMode = "",
}) {
  // 同时只允许一个对话框；新请求先把旧的按“取消”结束
  if (dialogResolve) closeDialog(null);
  return new Promise((resolve) => {
    dialogResolve = resolve;
    $("dialogTitle").textContent = title;
    $("dialogMsg").textContent = message;
    $("dialogMsg").hidden = !message;
    const input = $("dialogInput");
    input.type = password ? "password" : "text";
    input.inputMode = inputMode;
    input.autocomplete = password ? "current-password" : "off";
    input.maxLength = password ? 32 : 20;
    input.placeholder = placeholder;
    input.value = "";
    $("dialogOk").textContent = okText;
    setDialogError(error);
    $("dialogMask").hidden = false;
    setTimeout(() => input.focus(), 30);
  });
}

$("dialogOk").addEventListener("click", confirmDialog);
$("dialogCancel").addEventListener("click", () => closeDialog(null));
$("dialogMask").addEventListener("mousedown", (e) => {
  if (e.target === $("dialogMask")) closeDialog(null);
});
$("dialogInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    confirmDialog();
  }
});
$("dialogInput").addEventListener("input", () => setDialogError(""));

/* ---------- 查看密码：顶栏的锁按钮 ---------- */
function updateLockUI() {
  const btn = $("btnLock");
  const u = users.find((x) => x.file_key === currentKey);
  if (!u || !u.protected) {
    btn.hidden = true;
    return;
  }
  const unlocked = !!sessionPw[currentKey];
  btn.hidden = false;
  btn.textContent = unlocked ? "🔓 已解锁" : "🔒 密码";
  btn.title = unlocked
    ? "这份日记已解锁（点击可重新上锁）"
    : "这份日记设了查看密码，点击输入";
  btn.classList.toggle("unlocked", unlocked);
}

async function onLockClick() {
  const u = users.find((x) => x.file_key === currentKey);
  if (!u || !u.protected) return;
  if (sessionPw[currentKey]) {
    delete sessionPw[currentKey];
    updateLockUI();
    toast("已重新上锁，下次翻阅需要密码");
    return;
  }
  let error = "";
  for (;;) {
    const pw = await askDialog({
      title: "输入查看密码",
      message:
        "这份日记设了查看密码（主人在 QQ 里用 /日记密码 设置）。\n" +
        "输入正确后，就能在面板里翻阅和导出这份日记。",
      error,
      password: true,
      placeholder: "4~32 位、不含空格",
      okText: "解锁",
    });
    if (pw === null) return;
    if (days.length === 0) {
      // 还没有日记可验证；先记住密码，导出时后端还会再校验一次
      sessionPw[currentKey] = pw;
      updateLockUI();
      toast("已记住密码");
      return;
    }
    try {
      await bridge.apiGet(`day/${currentKey}/${days[days.length - 1].day}`, {
        pw,
      });
    } catch (e) {
      const msg = e.message || "";
      if (msg.includes("PASSWORD_WRONG")) {
        error = "密码不对，请重新输入。";
        continue;
      }
      toast(msg || "密码校验失败");
      return;
    }
    sessionPw[currentKey] = pw;
    updateLockUI();
    toast("已解锁");
    if (dayIdx >= 0) await showDay(dayIdx, false);
    return;
  }
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
    updateLockUI();
    return;
  }
  if (!users.some((u) => u.file_key === currentKey)) {
    currentKey = users[0].file_key;
  }
  sel.value = currentKey;
  updateLockUI();
}

async function loadDays() {
  updateLockUI();
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

  let r;
  try {
    r = await bridge.apiGet(`day/${currentKey}/${dayStr}`, params);
  } catch (e) {
    const msg = e.message || "";
    if (!msg.includes("PASSWORD_WRONG")) {
      toast(msg || "读取失败");
      return null;
    }
    // 主人改过密码：缓存作废，下面重新询问
    delete sessionPw[currentKey];
    delete params.pw;
    updateLockUI();
    r = { need_password: true };
  }

  if (r.need_owner) {
    let error = "";
    for (;;) {
      const qq = await askDialog({
        title: "确认日记主人",
        message:
          "这份旧日记的说明里没有主人 QQ（文件名是加密的、反推不出）。\n" +
          "请输入日记主人的 QQ 号：会用它全量验签，通过才会显示。",
        error,
        inputMode: "numeric",
        placeholder: "日记主人的 QQ 号",
        okText: "验签并打开",
      });
      if (qq === null) {
        toast("已取消");
        return null;
      }
      params.qq = qq;
      try {
        r = await bridge.apiGet(`day/${currentKey}/${dayStr}`, params);
      } catch (e) {
        delete params.qq;
        error =
          (e.message || "").replace(/^.*无法确认日记主人：/, "验签未通过：") ||
          "验签未通过，请重新输入 QQ 号";
        continue;
      }
      delete params.qq;
      const u = users.find((x) => x.file_key === currentKey);
      if (u) u.owner_known = true;
      $("ownerHint").hidden = true;
      toast("QQ 验签通过，已记住这份日记的主人");
      break;
    }
  }

  if (r.need_password) {
    let error = "";
    for (;;) {
      const pw = await askDialog({
        title: "这份日记已上锁",
        message: "这份日记设了查看密码，请输入后翻阅。",
        error,
        password: true,
        placeholder: "4~32 位、不含空格",
        okText: "解锁",
      });
      if (pw === null) {
        toast("已取消");
        return null;
      }
      try {
        r = await bridge.apiGet(`day/${currentKey}/${dayStr}`, {
          ...params,
          pw,
        });
      } catch (e) {
        const msg = e.message || "";
        if (msg.includes("PASSWORD_WRONG")) {
          error = "密码不对，请重新输入。";
          continue;
        }
        toast(msg || "读取失败");
        return null;
      }
      sessionPw[currentKey] = pw;
      updateLockUI();
      break;
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

  const params = {};
  if (sessionPw[currentKey]) params.pw = sessionPw[currentKey];

  async function askOwner(message, error = "") {
    return askDialog({
      title: "确认日记主人",
      message,
      error,
      inputMode: "numeric",
      placeholder: "日记主人的 QQ 号",
      okText: "继续",
    });
  }

  if (!u.owner_known) {
    const qq = await askOwner(
      "导出迁移包需要日记主人的 QQ 号（写入清单并用于验签）。\n请输入："
    );
    if (qq === null) {
      toast("已取消导出");
      return;
    }
    params.qq = qq;
  }

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

  for (;;) {
    try {
      const r = await run();
      if (params.pw) sessionPw[currentKey] = params.pw;
      if (params.qq) u.owner_known = true;
      updateLockUI();
      okToast(r);
      return;
    } catch (e) {
      const msg = e.message || "";
      if (msg.includes("PASSWORD_REQUIRED") || msg.includes("PASSWORD_WRONG")) {
        delete sessionPw[currentKey];
        delete params.pw;
        updateLockUI();
        const pw = await askDialog({
          title: "这份日记已上锁",
          message: "导出迁移包需要查看密码，请输入。",
          error: msg.includes("PASSWORD_WRONG") ? "密码不对，请重新输入。" : "",
          password: true,
          placeholder: "4~32 位、不含空格",
          okText: "解锁并导出",
        });
        if (pw === null) {
          toast("已取消导出");
          return;
        }
        params.pw = pw;
        continue;
      }
      if (msg.includes("OWNER_UNKNOWN") || msg.includes("QQ 号与日记签名")) {
        const qq = await askOwner(
          "导出迁移包需要日记主人的 QQ 号（写入清单并用于验签）。",
          "QQ 号与日记签名对不上，请重新输入。"
        );
        if (qq === null) {
          toast("已取消导出");
          return;
        }
        params.qq = qq;
        continue;
      }
      if (msg.includes("REMOTE_PANEL")) {
        toast("管理面板不在服务器本机，请改用「下载到浏览器」", 4500);
        return;
      }
      toast(msg || "导出失败");
      return;
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
  if (!document.getElementById("dialogMask").hidden) {
    if (e.key === "Escape") {
      e.preventDefault();
      closeDialog(null);
    }
    return; // 对话框打开时不响应翻页快捷键
  }
  if (e.key === "ArrowLeft") showDay(dayIdx - 1, true);
  if (e.key === "ArrowRight") showDay(dayIdx + 1, true);
});
$("btnLock").addEventListener("click", onLockClick);

await loadUsers();
if (currentKey) await loadDays();
updateLockUI();
