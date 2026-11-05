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
/* 插图回填的“代次”：每次真正替换正文前自增。图片请求是异步的，
 * 返回时 token 或当前页不匹配说明用户已经翻页/换人，这张图必须丢弃。 */
let viewToken = 0;

/* 批量删除（v0.6.1）：目录里勾选的日期，用 d.day（YYYYMMDD）作键。
 * 勾选只对当前用户/当前目录有意义，切换用户或刷新目录时必须清空。 */
const selectedDays = new Set();
/* 批量删除提交中：防重复点击，同时锁住顶栏按钮与目录复选框 */
let deleting = false;

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

/* ---------- 页面内对话框（输入 / 确认） ----------
 * 插件视图 iframe 的 sandbox 是 "allow-scripts allow-forms allow-downloads"，
 * 没有 allow-modals：原生 window.prompt / confirm / alert 会被浏览器直接忽略
 * （返回 null，控制台报 Ignored call to prompt），所以查看密码、主人 QQ 和
 * 批量删除确认都必须用页面里的对话框，不能依赖原生弹窗。
 */
let dialogResolve = null;
/* 确认型对话框（askConfirm）没有输入框，点「确定」不校验内容；
 * 每次 askDialog 打开都显式重设，密码 / QQ 输入框不会被上一次确认框影响。 */
let dialogNeedsInput = true;

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
  // 确认型：没有输入框可校验，点「确定」直接算确认
  if (!dialogNeedsInput) {
    closeDialog(true);
    return;
  }
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
  warning = "",
  confirm = false,
  danger = false,
}) {
  // 同时只允许一个对话框；新请求先把旧的按“取消”结束
  if (dialogResolve) closeDialog(null);
  return new Promise((resolve) => {
    dialogResolve = resolve;
    dialogNeedsInput = !confirm;
    $("dialogTitle").textContent = title;
    $("dialogMsg").textContent = message;
    $("dialogMsg").hidden = !message;
    $("dialogWarning").textContent = warning;
    $("dialogWarning").hidden = !warning;
    const input = $("dialogInput");
    // 确认型对话框必须隐藏输入框（否则用户以为要填内容）；恢复时同样显式
    // 显示回来，否则随后打开的密码 / QQ 对话框会缺输入框。
    input.hidden = confirm;
    input.type = password ? "password" : "text";
    input.inputMode = inputMode;
    input.autocomplete = password ? "current-password" : "off";
    input.maxLength = password ? 32 : 20;
    input.placeholder = placeholder;
    input.value = "";
    const okBtn = $("dialogOk");
    okBtn.textContent = okText;
    okBtn.classList.toggle("danger", danger);
    setDialogError(error);
    $("dialogMask").hidden = false;
    // 确认型没有输入框可聚焦：把焦点交给「确定」，回车 / 空格即可确认
    setTimeout(() => (confirm ? okBtn.focus() : input.focus()), 30);
  });
}

/* 确认型对话框：true = 用户确认，false = 取消（取消按钮 / Esc / 点遮罩）。
 * 必须走页面内对话框：插件视图 iframe 的 sandbox 没有 allow-modals，
 * 原生 confirm() 会被浏览器忽略并直接返回 false。 */
function askConfirm({ title, message = "", warning = "", okText = "确定", danger = false }) {
  return askDialog({ title, message, warning, okText, danger, confirm: true }).then(
    (value) => value === true
  );
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

/* alt / data-entry 要放进属性值里，除 HTML 三件套外还要处理引号 */
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* ---------- 日记正文渲染 ----------
 * 轻量自渲染器：分隔条（—— …… ——）染成淡色小字；识别标准 markdown 图片语法
 * `![alt](images/name)`（只认 images/ 开头，与契约第 2 节一致）渲染成 <figure> 占位。
 * 图片本身不能直接 <img src>：插件视图在 sandbox iframe 里，浏览器请求不带
 * Dashboard 鉴权，必须由 JS 走 bridge 取回 JSON base64 再换成 data URL。
 */
const IMAGE_MARKDOWN_RE = /!\[([^\]]*)\]\((images\/[^)\s]+)\)/g;

function imageFigureHtml(alt, entry) {
  const name = entry.slice("images/".length);
  // 图还没生成好：只显示“还在画”占位，不发请求；
  // 后端 replace_pending_images() 成功后会把这行换成最终文件名。
  const generating = /^PENDING-/i.test(name);
  const cls = generating
    ? "diary-img pending img-generating"
    : "diary-img pending";
  return (
    `<figure class="${cls}" data-entry="${escapeAttr(entry)}">` +
    `<img alt="${escapeAttr(alt)}"></figure>`
  );
}

/* 单行正文：把图片语法换成 <figure>，其余片段照常转义 */
function renderLineWithImages(line) {
  if (!line.includes("![")) return escapeHtml(line);
  IMAGE_MARKDOWN_RE.lastIndex = 0;
  let out = "";
  let last = 0;
  let m;
  while ((m = IMAGE_MARKDOWN_RE.exec(line)) !== null) {
    out += escapeHtml(line.slice(last, m.index));
    out += imageFigureHtml(m[1], m[2]);
    last = m.index + m[0].length;
  }
  out += escapeHtml(line.slice(last));
  return out;
}

function renderContent(text) {
  return text
    .split("\n")
    .map((line) =>
      /^——.+——$/.test(line.trim())
        ? `<span class="seg-header">${escapeHtml(line)}</span>`
        : renderLineWithImages(line)
    )
    .join("\n");
}

/* ---------- 页边批注（v0.6） ----------
 * 后端 GET day/... 只在开启且有批注时返回 annotations，验签失败时返回
 * annotations_tampered。这里按 anchor（原文摘录 ≤30 字）在正文纯文本里定位：
 * 命中处插入上标角标 + 悬停/点按卡片；找不到的不丢，收进当天日记底部的
 * .ann-margin 页边区域（日记被面板外改过时锚点会失配）。
 * 全部文本一律 escape（批注/摘要/标题都来自 LLM，绝不直接拼进 HTML）。
 */
const ANN_SKIP_SELECTOR =
  "figure.diary-img, sup, .seg-header, .ann-mark, .ann-card, .ann-margin, .ann-tampered";
/* 批注卡片是 position: fixed 的悬浮层（见 style.css），会浮在活页本之上、
 * 不被书页裁切；下面三个常量用于打开时把它钉在角标旁并夹进视口。 */
const ANN_CARD_OVERLAP = 2; /* 卡片与角标叠住 2px：鼠标从角标移进卡片时不断 :hover */
const ANN_VIEW_MARGIN = 10; /* 卡片离视口边缘的最小距离（px） */
const ANN_CARD_MIN_SPACE = 120; /* 可用高度太小时的下限，卡片内部滚动 */

function annText(v) {
  if (typeof v === "string") return v;
  if (v === null || v === undefined) return "";
  return String(v);
}

/* 角标序号：1 → 1，12 → 12。用普通数字而不是 ⁰¹² 上标字符：上标字符本身
 * 只有半个字高，再叠一层 <sup> 会小到看不清（放大角标时改的）。 */
function annSupNumber(n) {
  const digits = String(n).replace(/\D/g, "");
  return digits || "1";
}

/* 卡片两区：批注原文（不加"批注内容"字样标题）+ 回复区（reply 存在时才有） */
function annotationBodyHtml(ann) {
  let html = `<span class="ann-note">${escapeHtml(annText(ann && ann.note))}</span>`;
  const reply = ann && ann.reply;
  if (reply && typeof reply === "object") {
    let inner = "";
    if (reply.reply_title) {
      inner +=
        `<span class="ann-reply-title">` +
        `${escapeHtml(annText(reply.reply_title))}</span>`;
    }
    if (reply.summary) {
      inner += `<span class="ann-summary">${escapeHtml(annText(reply.summary))}</span>`;
    }
    if (reply.speculation_marked) {
      inner +=
        `<span class="ann-spec">推测：` +
        `${escapeHtml(annText(reply.speculation_marked))}</span>`;
    }
    if (inner) html += `<span class="ann-reply">${inner}</span>`;
  }
  return html;
}

function annotationCardHtml(ann) {
  return (
    `<span class="ann-card" role="note" data-ann-id="` +
    `${escapeAttr(annText(ann && ann.id))}">` +
    annotationBodyHtml(ann) +
    `</span>`
  );
}

/* 在正文纯文本里找 anchor 第一次出现的位置；跳过图片/角标/批注卡片自身等节点 */
function findAnnAnchor(anchor) {
  const body = $("diaryBody");
  if (!body) return null;
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || node.nodeValue.indexOf(anchor) < 0) {
        return NodeFilter.FILTER_REJECT;
      }
      let p = node.parentNode;
      while (p && p !== body) {
        if (
          p.nodeType === 1 &&
          p.matches &&
          p.matches(ANN_SKIP_SELECTOR)
        ) {
          return NodeFilter.FILTER_REJECT;
        }
        p = p.parentNode;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const index = node.nodeValue.indexOf(anchor);
    if (index >= 0) return { node, index };
  }
  return null;
}

function insertAnnMark(hit, anchor, ann, ord) {
  const node = hit.node;
  const parent = node.parentNode;
  if (!parent) return false;
  const after = node.splitText(hit.index + anchor.length);
  const mid = node.splitText(hit.index);
  const mark = document.createElement("span");
  mark.className = "ann-mark";
  mark.setAttribute("data-ann-id", annText(ann && ann.id));
  mark.appendChild(mid);
  const badge =
    `<sup class="ann-badge" data-ann-id="${escapeAttr(annText(ann && ann.id))}"` +
    ` role="button" tabindex="0" title="页边批注">${annSupNumber(ord)}</sup>`;
  mark.insertAdjacentHTML("beforeend", badge + annotationCardHtml(ann));
  // 卡片是 fixed 悬浮层：鼠标移上来（悬停展开）或键盘聚焦角标时，按角标
  // 当前位置把卡片钉好；触屏点按在 onAnnBadgeClick 里另有一处。
  mark.addEventListener("mouseenter", () => trackAnnMark(mark));
  mark.addEventListener("focusin", () => trackAnnMark(mark));
  parent.insertBefore(mark, after);
  return true;
}

/* 锚点失配兜底：收进当天日记底部的页边区域，条间留白、同样两区内容。
 * 按设计方案 8.3 一期不加"批注内容"字样标题（页面无 AI 标题可对上），
 * 只保留一条细分割线与正文区分。 */
function appendAnnMargin(items) {
  const body = $("diaryBody");
  if (!body || !items.length) return;
  let html = "";
  for (const item of items) {
    const anchor = annText(item.ann && item.ann.anchor).trim();
    html +=
      `<div class="ann-margin-card" data-ann-id="${escapeAttr(annText(item.ann && item.ann.id))}">` +
      (anchor
        ? `<div class="ann-margin-anchor">锚于「${escapeHtml(anchor)}」</div>`
        : "") +
      annotationBodyHtml(item.ann) +
      `</div>`;
  }
  const box = document.createElement("div");
  box.className = "ann-margin";
  box.innerHTML = html;
  body.appendChild(box);
}

/* 批注卡片是固定定位的悬浮层：打开时量一下角标位置，把卡片钉在角标下方
 * （下方放不下且上方更宽松时翻到上方），再夹进视口 —— 卡片因此能盖在活页本
 * （书页 / 书脊 / 书页圆角）之上，长批注也不会被书页边缘裁掉；内容比可用
 * 空间还高时由 CSS 的 overflow-y 在卡片内部滚动。纯展示用，不影响内容。 */
function annCardSize(card) {
  // 卡片平时 display: none，量不到尺寸：先临时显示成不可见、脱离文档流再量
  const saved = card.getAttribute("style") || "";
  card.style.cssText =
    saved +
    ";display:block;visibility:hidden;position:fixed;left:-99999px;top:0;max-height:none;";
  const width = card.offsetWidth;
  const height = card.offsetHeight;
  card.style.cssText = saved;
  return { width, height };
}

function positionAnnCard(mark) {
  const badge = mark && mark.querySelector ? mark.querySelector(".ann-badge") : null;
  const card = mark && mark.querySelector ? mark.querySelector(".ann-card") : null;
  if (!badge || !card) return;
  const rect = badge.getBoundingClientRect();
  if (!rect.width && !rect.height) return;
  const vw = window.innerWidth || document.documentElement.clientWidth;
  const vh = window.innerHeight || document.documentElement.clientHeight;
  const size = annCardSize(card);
  const width = Math.min(size.width || 280, vw - ANN_VIEW_MARGIN * 2);
  const spaceBelow =
    vh - ANN_VIEW_MARGIN - (rect.bottom - ANN_CARD_OVERLAP);
  const spaceAbove = rect.top + ANN_CARD_OVERLAP - ANN_VIEW_MARGIN;
  // 下方放得下就朝下；放不下且上方更宽松就翻到角标上方（与旧版方向一致）
  const up = size.height > spaceBelow && spaceAbove > spaceBelow;
  const space = Math.max(ANN_CARD_MIN_SPACE, up ? spaceAbove : spaceBelow);
  const height = Math.min(size.height || 0, space);
  // 水平方向：默认左缘对齐角标左缘，右/左越界时夹回视口内
  let left = rect.left;
  if (left + width > vw - ANN_VIEW_MARGIN) left = vw - ANN_VIEW_MARGIN - width;
  if (left < ANN_VIEW_MARGIN) left = ANN_VIEW_MARGIN;
  // 卡片与角标叠住 2px：鼠标从角标移进卡片时 :hover 不断，长批注才滚得动
  const top = up
    ? rect.top + ANN_CARD_OVERLAP - height
    : rect.bottom - ANN_CARD_OVERLAP;
  card.style.maxHeight = Math.round(space) + "px";
  card.style.left = Math.round(left) + "px";
  card.style.top = Math.round(Math.max(ANN_VIEW_MARGIN, top)) + "px";
}

/* 当前展开的角标：滚动阅读栏 / 改窗口大小时按它重新钉住卡片 */
let activeAnnMark = null;

function trackAnnMark(mark) {
  if (!mark) return;
  activeAnnMark = mark;
  positionAnnCard(mark);
}

function repositionActiveAnnCard() {
  if (activeAnnMark && activeAnnMark.isConnected) positionAnnCard(activeAnnMark);
}

function closeAnnCards() {
  activeAnnMark = null;
  for (const mark of document.querySelectorAll(".ann-mark.open")) {
    mark.classList.remove("open");
  }
}

/* 触屏没有 hover：点角标切换卡片；点页面空白处收起 */
function onAnnBadgeClick(e) {
  const target = e.target;
  const badge =
    target && target.closest ? target.closest(".ann-badge") : null;
  if (!badge) {
    if (target && target.closest && target.closest(".ann-card")) return;
    closeAnnCards();
    return;
  }
  const mark = badge.closest(".ann-mark");
  if (!mark) return;
  const willOpen = !mark.classList.contains("open");
  closeAnnCards();
  if (willOpen) {
    mark.classList.add("open");
    trackAnnMark(mark);
  }
  e.preventDefault();
  e.stopPropagation();
}

function onAnnBadgeKeydown(e) {
  if (e.key !== "Enter" && e.key !== " ") return;
  const target = e.target;
  const badge =
    target && target.closest ? target.closest(".ann-badge") : null;
  if (!badge) return;
  const mark = badge.closest(".ann-mark");
  if (!mark) return;
  const willOpen = !mark.classList.contains("open");
  closeAnnCards();
  if (willOpen) {
    mark.classList.add("open");
    trackAnnMark(mark);
  }
  e.preventDefault();
}

function applyAnnotations(annotations, tampered) {
  const body = $("diaryBody");
  if (!body) return;
  // 验签失败：顶部警告条（契约 7.2；日记本身篡改时后端不会带批注字段）
  if (tampered) {
    const warn = document.createElement("div");
    warn.className = "ann-tampered";
    warn.textContent =
      "⚠ 页边批注验签失败，疑似在插件之外被改过，已拒绝显示。";
    body.insertBefore(warn, body.firstChild);
  }
  if (!Array.isArray(annotations) || annotations.length === 0) return;
  const missed = [];
  const usedAnchors = {};
  let ord = 0;
  for (const ann of annotations) {
    if (!ann || typeof ann !== "object") continue;
    ord += 1;
    const anchor = annText(ann.anchor).trim();
    // 同一锚点只标正文里第一处：重复锚点不再二次命中
    if (anchor && !usedAnchors[anchor]) {
      const hit = findAnnAnchor(anchor);
      if (hit) {
        usedAnchors[anchor] = true;
        if (insertAnnMark(hit, anchor, ann, ord)) continue;
      }
    }
    missed.push({ ann });
  }
  if (missed.length) appendAnnMargin(missed);
  // 卡片是 fixed 悬浮层，打开时才按角标位置落位（见 positionAnnCard），
  // 这里不需要预先量版。零批注时上面已提前返回，DOM 不变。
}

/* ---------- 日记插图：注入 DOM 后逐张异步回填 ----------
 * 回填前用 token + 当前页快照双重校验：与 showDay 里 `if (!days.includes(day)) return;`
 * 同一思路，防止上一页/上一个用户的图在请求返回时落到新一页。
 */
function isCurrentDiaryView(day, token) {
  return token === viewToken && dayIdx >= 0 && days[dayIdx] === day;
}

function waitImageLoad(img) {
  return new Promise((resolve) => {
    if (img.complete && img.naturalWidth > 0) {
      resolve(true);
      return;
    }
    const finish = (ok) => {
      img.removeEventListener("load", onLoad);
      img.removeEventListener("error", onError);
      resolve(ok);
    };
    const onLoad = () => finish(true);
    const onError = () => finish(false);
    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);
  });
}

/* missing：优雅降级成文本占位（正文照常可读）；tampered：按入侵给警告 */
function markImageMissing(figure, alt, status) {
  figure.classList.remove("pending", "img-generating");
  if (status === "tampered") {
    figure.classList.add("img-tampered");
    figure.innerHTML =
      '<span class="img-warning">⚠ 这幅插图验签失败，疑似在插件之外被换过</span>';
    return;
  }
  figure.classList.add("img-missing");
  figure.innerHTML = `<span class="img-missing-text">[插图：${escapeHtml(alt)}]</span>`;
}

async function applyDiaryImage(figure, alt, res) {
  const img = figure.querySelector("img");
  if (!img) return;
  img.src = "data:" + (res.mime || "image/png") + ";base64," + res.image;
  const ok = await waitImageLoad(img);
  if (!ok) {
    markImageMissing(figure, alt, "");
    return;
  }
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  // 横图通栏、竖图 70% 居中、方图居中（具体尺寸见 style.css）
  const orientation = w > h ? "landscape" : h > w ? "portrait" : "square";
  figure.classList.remove("pending", "img-generating");
  figure.classList.add("img-loaded", orientation);
}

async function loadDiaryImages(day, token) {
  const body = $("diaryBody");
  if (!body) return;
  const figures = Array.from(
    body.querySelectorAll("figure.diary-img[data-entry]")
  );
  for (const figure of figures) {
    if (!isCurrentDiaryView(day, token) || !figure.isConnected) return;
    if (figure.classList.contains("img-generating")) continue; // PENDING：图还在画，不请求
    if (figure.classList.contains("img-loaded")) continue;
    const entry = figure.getAttribute("data-entry") || "";
    if (!entry.startsWith("images/")) continue;
    // 路由 /image/<file_key>/<name> 的 <name> 是单段路径：只传 zip 条目的 basename，
    // data-entry 仍按契约保留完整的 images/<basename>。
    const name = entry.slice("images/".length);
    const img = figure.querySelector("img");
    const alt = (img && img.alt) || "";
    const params = {};
    if (sessionPw[currentKey]) params.pw = sessionPw[currentKey];
    let res = null;
    try {
      res = await bridge.apiGet(`image/${currentKey}/${name}`, params);
    } catch (e) {
      console.warn("插图读取失败", name, e);
      if (!isCurrentDiaryView(day, token) || !figure.isConnected) return;
      markImageMissing(figure, alt, "");
      continue;
    }
    // 等待期间翻页/切用户了：这张图不能落到当前页
    if (!isCurrentDiaryView(day, token) || !figure.isConnected) return;
    if (res && res.found && res.image) {
      await applyDiaryImage(figure, alt, res);
    } else {
      markImageMissing(figure, alt, res && res.status);
    }
  }
}

/* 昵称里的不可见字符（零宽 / 方向控制 / 软连字符 / 谚文与盲文填充）：
 * 有人用纯隐形字符当 QQ 昵称，只显示昵称时下拉框就是一片空白、分不清是谁。
 * 字符集与插件侧 common.py 的 INVISIBLE_CHARS_RE 对齐，另补几个常见隐形填充。 */
const INVISIBLE_DISPLAY_RE =
  /[\u00AD\u115F\u1160\u17B4\u17B5\u200B-\u200F\u202A-\u202E\u2060\u2800\u3164\uFEFF\uFFA0]/g;

function visibleText(s) {
  return String(s || "").replace(INVISIBLE_DISPLAY_RE, "").trim();
}

/* 用户下拉框的一行标签：昵称可见时「🔒 昵称（QQ xxx · N 天）」；
 * 昵称不可见时降级「🔒 QQ xxx（N 天）」；连主人 QQ 都未知时保持旧文案。 */
function userLabel(u) {
  const lock = u.protected ? "🔒 " : "";
  const nick = visibleText(u.nickname);
  const qq = String(u.owner || "").trim();
  const days = `${u.day_count} 天`;
  if (nick && qq) return `${lock}${nick}（QQ ${qq} · ${days}）`;
  if (qq) return `${lock}QQ ${qq}（${days}）`;
  return `${lock}${u.nickname}（${days}）`;
}

async function loadUsers() {
  // 用户列表刷新可能换人 / 清空：旧勾选不能留到新用户的目录上
  clearSelectedDays();
  const r = await bridge.apiGet("users");
  users = r.users || [];
  const sel = $("userSelect");
  sel.innerHTML = "";
  for (const u of users) {
    const opt = document.createElement("option");
    opt.value = u.file_key;
    opt.textContent = userLabel(u);
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
    viewToken += 1; // 没有任何用户：丢弃仍在路上的插图回填
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
  // 目录整体刷新（切用户 / 刷新 / 删除成功）：勾选只对旧目录有意义，先清空
  clearSelectedDays();
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
    viewToken += 1; // 清空正文：让还在路上的旧插图请求失效
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
    const checked = selectedDays.has(d.day);
    // 复选框用 title/aria-label 带上日期：图标本身没有文字，读屏只能靠它
    li.innerHTML =
      `<input type="checkbox" class="day-check"` +
      ` title="选择 ${escapeAttr(d.label)}" aria-label="选择 ${escapeAttr(d.label)}"` +
      `${checked ? " checked" : ""}>` +
      `<span>${escapeHtml(d.label)}</span><span class="idx">第 ${i + 1} 页</span>`;
    if (i === dayIdx) li.classList.add("active");
    if (checked) li.classList.add("selected");
    li.addEventListener("click", () => {
      showDay(i, true);
      closeToc(); // 窄屏抽屉：点日期跳过去就合上（点复选框被 stopPropagation 拦掉，不会触发）
    });
    ul.appendChild(li);
  });
  // 没日记时抽屉里没有可勾的日期，开关一并禁用
  $("btnToc").disabled = days.length === 0;
  // 复选框只负责勾选，不能顺带翻页：click 事件拦住冒泡，点 li 其它区域照旧翻页
  for (const cb of ul.querySelectorAll(".day-check")) {
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", onDayCheckChange);
  }
  ul.scrollTop = ul.scrollHeight;
  updateBatchDeleteUI();
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
  const day = days[i];
  loading = true;
  // 翻页/切用户前先收起上一天还开着的批注卡片：卡片是 fixed 悬浮层，不随
  // innerHTML 一起消失；翻页动画的 transform 也会临时把 fixed 卡片夹回页面里。
  closeAnnCards();
  const page = $("pageRight");
  try {
    if (animate) {
      page.classList.remove("turn");
      void page.offsetWidth; // 重启动画
      page.classList.add("turn");
    }
    const r = await fetchDayWithGates(day.day);
    if (!r) return; // 用户取消或校验失败，提示已在函数内给出
    // 等待（含密码/QQ 对话框）期间用户可能已切换用户或刷新目录：
    // days 已整体更换时放弃本次展示，避免旧内容配上新目录的标题
    if (!days.includes(day)) return;
    // 真正替换正文前递增 token：上一页还在路上的插图请求从此刻起一律作废
    const token = (viewToken += 1);
    dayIdx = i;
    $("diaryHeader").innerHTML =
      `<span>${escapeHtml(day.label)}</span>` +
      `<span class="stamp">第 ${i + 1} / ${days.length} 页</span>`;
    if (r.tampered) {
      $("diaryBody").innerHTML =
        '<span class="tampered">⚠ 这一天的日记验签失败，疑似在插件之外被修改过，已拒绝展示。</span>';
    } else {
      $("diaryBody").innerHTML = renderContent(r.content || "（空白）");
      // 页边批注：翻页/切用户时随 innerHTML 整体替换，不需要额外清理；
      // 零批注且未验签失败时本函数不改动 DOM（契约 7.2）。
      applyAnnotations(r.annotations || [], r.annotations_tampered);
      // 不 await：图片走异步回填，不阻塞翻页（大图 base64 可能很慢）
      void loadDiaryImages(day, token);
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

/* ---------- 窄屏目录抽屉（v0.6.1-hotfix1） ----------
 * 宽屏目录固定在左页，这两个是空操作；窄屏时左页变成抽屉，靠它们开合。 */
function closeToc() {
  const toc = $("pageLeft");
  if (!toc.classList.contains("toc-open")) return;
  toc.classList.remove("toc-open");
  $("btnToc").setAttribute("aria-expanded", "false");
}
function toggleToc() {
  const toc = $("pageLeft");
  const open = toc.classList.toggle("toc-open");
  $("btnToc").setAttribute("aria-expanded", String(open));
}

/* ---------- 批量删除（v0.6.1） ----------
 * 目录左页勾选多个日期，顶栏一次删掉（不可恢复）。原生 confirm 在插件视图
 * iframe 里会被忽略（sandbox 没有 allow-modals），确认框必须走页面内的
 * askConfirm；删除接口的两道闸门（主人 QQ / 查看密码）与翻阅、导出同一口径。
 */
function updateBatchDeleteUI() {
  const btn = $("btnBatchDelete");
  const n = selectedDays.size;
  btn.disabled = deleting || n === 0;
  btn.textContent = deleting
    ? "🗑 正在删除…"
    : n > 0
      ? `🗑 批量删除（${n}）`
      : "🗑 批量删除";
  // 提交期间锁住复选框：否则用户会以为还能改所选日期
  for (const cb of $("dayList").querySelectorAll(".day-check")) {
    cb.disabled = deleting;
  }
}

function clearSelectedDays() {
  selectedDays.clear();
  for (const cb of $("dayList").querySelectorAll(".day-check")) {
    cb.checked = false;
    const li = cb.closest("li");
    if (li) li.classList.remove("selected");
  }
  updateBatchDeleteUI();
}

/* 复选框 change：只改选中集合与视觉，不翻页（click 冒泡已在 renderDayList 拦住） */
function onDayCheckChange(e) {
  const cb = e.target;
  const li = cb.closest("li");
  const i = li ? Number(li.dataset.i) : -1;
  const d = Number.isInteger(i) ? days[i] : null;
  if (!d) return;
  if (cb.checked) selectedDays.add(d.day);
  else selectedDays.delete(d.day);
  li.classList.toggle("selected", cb.checked);
  updateBatchDeleteUI();
}

/* 发删除请求并处理两道闸门：need_owner（旧 zip 无主人信息）先要主人 QQ，
 * need_password / PASSWORD_WRONG 再要查看密码。成功返回响应；用户取消或失败
 * 返回 null（提示已在本函数内给出，页面数据保持不变）。 */
async function deleteDaysRequest(daysToDelete) {
  const body = { file_key: currentKey, days: daysToDelete };
  if (sessionPw[currentKey]) body.pw = sessionPw[currentKey];

  async function askOwner(error) {
    return askDialog({
      title: "确认日记主人",
      message:
        "这份旧日记的说明里没有主人 QQ（文件名是加密的、反推不出）。\n" +
        "请输入日记主人的 QQ 号：会用它全量验签，通过才会删除。",
      error,
      inputMode: "numeric",
      placeholder: "日记主人的 QQ 号",
      okText: "验证并删除",
    });
  }

  async function askPassword(message, error = "") {
    return askDialog({
      title: "这份日记已上锁",
      message,
      error,
      password: true,
      placeholder: "4~32 位、不含空格",
      okText: "解锁并删除",
    });
  }

  for (;;) {
    let r;
    try {
      r = await bridge.apiPost("delete_days", body);
    } catch (e) {
      const msg = e.message || "";
      if (msg.includes("PASSWORD_WRONG") || msg.includes("PASSWORD_REQUIRED")) {
        // 主人改过密码：缓存作废，重新询问（与 doExport 同一口径）
        delete sessionPw[currentKey];
        delete body.pw;
        updateLockUI();
        const pw = await askPassword(
          "删除日记需要查看密码，请输入。",
          msg.includes("PASSWORD_WRONG") ? "密码不对，请重新输入。" : ""
        );
        if (pw === null) {
          toast("已取消删除");
          return null;
        }
        body.pw = pw;
        continue;
      }
      if (
        msg.includes("OWNER_UNKNOWN") ||
        msg.includes("无法确认日记主人") ||
        msg.includes("QQ 号与日记签名")
      ) {
        const qq = await askOwner(
          msg.includes("OWNER_UNKNOWN")
            ? ""
            : "QQ 号与日记签名对不上，请重新输入。"
        );
        if (qq === null) {
          toast("已取消删除");
          return null;
        }
        body.qq = qq;
        continue;
      }
      toast(msg || "删除失败", 4500);
      return null;
    }
    // 闸门响应是 HTTP 200（need_owner / need_password），不是错误：问完再重试
    if (r.need_owner) {
      const qq = await askOwner("");
      if (qq === null) {
        toast("已取消删除");
        return null;
      }
      body.qq = qq;
      continue;
    }
    if (r.need_password) {
      const pw = await askPassword("删除日记需要查看密码，请输入。");
      if (pw === null) {
        toast("已取消删除");
        return null;
      }
      body.pw = pw;
      continue;
    }
    // 成功：记住密码 / 主人信息，后续翻阅、导出不再重复输入
    if (body.pw) sessionPw[currentKey] = body.pw;
    if (body.qq) {
      const u = users.find((x) => x.file_key === currentKey);
      if (u) u.owner_known = true;
      $("ownerHint").hidden = true;
    }
    updateLockUI();
    return r;
  }
}

async function onBatchDelete() {
  if (deleting || !currentKey) return;
  // 只取当前目录里仍勾选的日期；顺序沿用 days（升序）
  const picked = days.filter((d) => selectedDays.has(d.day));
  if (picked.length === 0) return;
  const latest = picked[picked.length - 1]; // days 升序，最后一项即最大日期
  const ok = await askConfirm({
    title: "确认批量删除",
    message:
      `将删除 ${picked.length} 天日记（最新一天：${latest.label}）。\n` +
      "删除后这些日期会从这本日记里彻底消失。",
    warning: "⚠ 删除不可恢复，建议先「⬇ 导出日记」备份。",
    okText: "确认删除",
    danger: true,
  });
  if (!ok) {
    toast("已取消删除");
    return;
  }
  const daysToDelete = picked.map((d) => d.day);
  deleting = true;
  updateBatchDeleteUI();
  try {
    toast(`正在删除 ${daysToDelete.length} 天日记…`);
    const r = await deleteDaysRequest(daysToDelete);
    if (!r) return; // 取消或失败：提示已给出，页面数据不动
    // 先清空选中再刷新：loadUsers / loadDays 会按 Set 回填勾选，避免删完又勾上
    clearSelectedDays();
    const deleted = Number(r.deleted) || 0;
    const remaining = Number(r.remaining) || 0;
    if (deleted === 0) {
      toast("所选日期没有日记，未做任何改动");
    } else {
      toast(`已删除 ${deleted} 天日记（剩余 ${remaining} 天）`, 4500);
    }
    // 与「⟳ 刷新」同一顺序：先刷新用户列表（可能换人 / 用户整体消失），
    // 再按刷新后的 currentKey 拉目录；全部删光时 loadUsers 已切到空状态。
    try {
      await loadUsers();
      if (currentKey) await loadDays();
    } catch (e) {
      toast(e.message || "删除成功，但刷新列表失败，请点「⟳ 刷新」重试", 5000);
    }
  } finally {
    deleting = false;
    updateBatchDeleteUI();
  }
}

/* ---------- 导出 / 导入 ---------- */
/* 一键备份全部用户日记（管理员）：只含日记 zip，不含签名钥匙 */
async function doBackupAll() {
  try {
    const r = await bridge.download(
      "export_all",
      {},
      "memory_diary_backup.zip"
    );
    toast(
      `备份包已下载：${r.filename}（不含签名钥匙；整机迁移请连同主密钥 .diary_key 一起备份）`,
      6000
    );
  } catch (e) {
    toast(e.message || "备份失败", 4500);
  }
}

/* 一键备份全部用户日记 → 存到本机「下载」文件夹并打开位置（面板须开在本机） */
async function doBackupAllLocal() {
  try {
    const r = await bridge.apiGet("export_all_local");
    toast(
      `已存到「下载」文件夹并打开位置：${r.filename}` +
        "（不含签名钥匙；整机迁移请连同主密钥 .diary_key 一起备份）",
      6000
    );
  } catch (e) {
    const msg = e.message || "";
    toast(
      msg.includes("REMOTE_PANEL")
        ? "管理面板不在服务器本机，无法自动打开文件夹；请改用「备份全部日记（管理员）」下载到浏览器"
        : msg || "备份失败",
      6000
    );
  }
}

/* mode: "browser" 浏览器下载 ｜ "local" 存到本机下载文件夹并打开位置（面板须开在本机） */
async function doExport(mode) {
  if (mode === "backup_all") {
    await doBackupAll();
    return;
  }
  if (mode === "backup_all_local") {
    await doBackupAllLocal();
    return;
  }
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
/* 抽屉点外部收起：抽屉自身和「☰ 目录」按钮的点击不算外部 */
$("btnToc").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleToc();
});
document.addEventListener("click", (e) => {
  const toc = $("pageLeft");
  if (!toc.classList.contains("toc-open")) return;
  if (e.target.closest(".page-left") || e.target.closest("#btnToc")) return;
  closeToc();
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
$("btnBatchDelete").addEventListener("click", onBatchDelete);
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
    } else if (
      e.key === "Enter" &&
      !dialogNeedsInput &&
      document.activeElement !== $("dialogCancel")
    ) {
      // 确认型没有输入框：焦点不在「取消」上时，回车等价于点「确定」
      e.preventDefault();
      confirmDialog();
    }
    return; // 对话框打开时不响应翻页快捷键
  }
  if (e.key === "ArrowLeft") showDay(dayIdx - 1, true);
  if (e.key === "ArrowRight") showDay(dayIdx + 1, true);
  // 抽屉开着时优先用 Escape 关抽屉，不触发上面的对话框分支（对话框开着已提前 return）
  if (e.key === "Escape") closeToc();
});
$("btnLock").addEventListener("click", onLockClick);

/* 页边批注：点按切换卡片（触屏没有 hover）、键盘可操作；卡片是 fixed
 * 悬浮层，滚动阅读栏 / 改窗口大小后按角标新位置重新钉一次。 */
$("diaryBody").addEventListener("click", onAnnBadgeClick);
$("diaryBody").addEventListener("keydown", onAnnBadgeKeydown);
const annPageInner = $("pageInner");
if (annPageInner) {
  annPageInner.addEventListener("scroll", repositionActiveAnnCard, {
    passive: true,
  });
}
let annResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(annResizeTimer);
  annResizeTimer = setTimeout(repositionActiveAnnCard, 120);
});

await loadUsers();
if (currentKey) await loadDays();
updateLockUI();
