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
/* 切用户 / 刷新目录的“代次”（与 viewToken 分开）：文件夹目录请求 await 回来时
 * 若代次已变，说明用户又切了人 / 点了刷新，这次结果必须放弃，否则旧用户的
 * 目录和正文会落到新用户身上。 */
let navToken = 0;
/* 翻页请求还在路上时又请求了另一页：记下最后一次，等当前请求结束再翻，
 * 不再静默丢按键（旧实现 loading 期间 return，按键像没反应）。 */
let pendingDay = null;
/* 正在请求的那一页下标：连按方向键时以它为基准连续翻页（而不是以屏幕上
 * 还没更新的 dayIdx 为基准，否则连按两次只会翻一页）。 */
let inflightDay = null;

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
let prefs = { font_size: 16, font_family: "kai", paper: "default", backdrop: "default", paper_theme: "warm" };

/* 查看密码的短时凭证（v0.6.2 契约 3）：解锁 POST /unlock/<file_key> 换取 token，
 * 之后所有读接口用 query 参数 `tk` 携带（宿主 bridge 的 api:get/api:post 不支持
 * 自定义 header）。密码本身只在 unlock 的 POST body 里出现，不再放进 query，
 * 避免进访问日志 / 浏览器历史。凭证只存内存：刷新页面后需要重新输入密码。 */
const sessionToken = {};
const sessionTokenExp = {};
/* 本地比后端提前 5s 作废，留出请求在途的时间余量；后端仍是权威。 */
const TOKEN_SKEW_MS = 5000;
const TOKEN_TTL_FALLBACK_S = 1800;
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
  document.documentElement.dataset.paperTheme = prefs.paper_theme;
  for (const item of document.querySelectorAll(".theme-item[data-paper-theme]")) {
    const active = item.dataset.paperTheme === prefs.paper_theme;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  }
  root.setProperty("--d-font-size", prefs.font_size + "px");
  const fam =
    FONT_FAMILIES.find((f) => f.key === prefs.font_family) || FONT_FAMILIES[0];
  root.setProperty("--d-font-family", fam.css);
  $("btnFontFamily").textContent = fam.label;
  $("btnFontFamily").title = "切换字体（当前：" + fam.label + "）";
}

let saveTimer = null;
let cancelPendingSave = null;
function savePrefs() {
  if (cancelPendingSave) cancelPendingSave(false);
  clearTimeout(saveTimer);
  return new Promise((resolve) => {
    cancelPendingSave = resolve;
    saveTimer = setTimeout(async () => {
      cancelPendingSave = null;
      const requestedTheme = prefs.paper_theme;
      try {
        const r = await bridge.apiPost("prefs/save", { prefs });
        if (!r || r.saved !== true) throw new Error("偏好保存响应异常");
        if (
          r.prefs &&
          ["warm", "classic"].includes(r.prefs.paper_theme) &&
          prefs.paper_theme === requestedTheme
        ) {
          prefs.paper_theme = r.prefs.paper_theme;
          applyPrefs();
        }
        resolve(true);
      } catch (e) {
        console.warn("偏好保存失败", e);
        toast("偏好已临时生效，但保存失败，刷新后可能恢复", 5000);
        resolve(false);
      }
    }, 400);
  });
}

function setPaperTheme(theme) {
  if (!["warm", "classic"].includes(theme)) return false;
  prefs.paper_theme = theme;
  applyPrefs();
  const saving = savePrefs();
  closeMenus();
  return saving;
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
      if (["warm", "classic"].includes(r.prefs.paper_theme)) {
        prefs.paper_theme = r.prefs.paper_theme;
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

/* ---------- 查看密码凭证（v0.6.2 契约 3） ---------- */
function tokenAlive(key = currentKey) {
  const token = sessionToken[key];
  if (!token) return false;
  const exp = sessionTokenExp[key] || 0;
  if (exp && Date.now() >= exp - TOKEN_SKEW_MS) {
    delete sessionToken[key];
    delete sessionTokenExp[key];
    return false;
  }
  return true;
}

function setSessionToken(key, token, expiresIn) {
  if (!key || !token) return;
  sessionToken[key] = String(token);
  const ttl = Number(expiresIn) > 0 ? Number(expiresIn) : TOKEN_TTL_FALLBACK_S;
  sessionTokenExp[key] = Date.now() + ttl * 1000;
}

function clearSessionToken(key = currentKey) {
  if (!key) return;
  delete sessionToken[key];
  delete sessionTokenExp[key];
}

/* 读接口的凭证参数：token 走 query `tk`（契约 3）。没有可用 token 时返回空对象，
 * 由后端返回 need_password / 403 PASSWORD_REQUIRED，再走 acquireToken 的闸门。 */
function authParams() {
  return tokenAlive(currentKey) ? { tk: sessionToken[currentKey] } : {};
}

/* 主人 QQ 对话框（旧 zip 没有主人信息时用；unlock 接口用它验签/解析 owner） */
async function askOwnerQq({ message, error = "", okText = "验签并解锁" }) {
  return askDialog({
    title: "确认日记主人",
    message,
    error,
    inputMode: "numeric",
    placeholder: "日记主人的 QQ 号",
    okText,
  });
}

/* owner 解析成功后本地更新用户信息（P3-20）：owner/protected 以便顶栏 🔒/🔓
 * 与下拉框标签立刻正确。不重新拉 users，避免把人正在勾选的批量删除选择清掉。
 * passwordVerified=true 表示刚才的密码确实验证通过（acquireToken 成功），
 * 此时 protected 必定是 true；只解出 owner 时不能乱动 protected。 */
function markOwnerResolved(qq, { passwordVerified = false } = {}) {
  const u = users.find((x) => x.file_key === currentKey);
  if (!u) return;
  u.owner_known = true;
  if (!u.owner && qq) u.owner = String(qq);
  if (passwordVerified) u.protected = true;
  $("ownerHint").hidden = true;
  syncUserSelectLabels();
  updateLockUI();
}

/* 下拉框标签依赖 protected / owner：本地更新后要重写选项文字（保持当前选中） */
function syncUserSelectLabels() {
  const sel = $("userSelect");
  for (let i = 0; i < sel.options.length && i < users.length; i += 1) {
    sel.options[i].textContent = userLabel(users[i]);
  }
}

/* 走「主人 QQ → 查看密码」两道闸门，用 unlock 接口换短时 token。
 * 返回 true 表示 token 已就绪；false 表示用户取消或失败（提示已给出）。
 * needOwner=true 时先问主人 QQ（旧 zip 的说明里没有用户ID）。
 */
async function acquireToken({ reason = "", needOwner = false, qq = "", intent = "打开" } = {}) {
  const key = currentKey;
  if (!key) return false;
  let ownerQq = qq ? String(qq) : "";
  let error = "";
  let silentTried = false;
  const askQq = (err = "") =>
    askOwnerQq({
      message:
        "这份旧日记的说明里没有主人 QQ（文件名是加密的、反推不出）。\n" +
        `请输入日记主人的 QQ 号：会用它全量验签，通过后才会${intent}。`,
      error: err,
    });
  if (needOwner && !ownerQq) {
    const asked = await askQq();
    if (asked === null) {
      toast("已取消");
      return false;
    }
    ownerQq = asked;
  }
  for (;;) {
    // 有主人 QQ 时先不带密码试换一次令牌：旧 zip 可能根本没设查看密码，
    // 后端对「没设密码的日记」直接发令牌，不该让用户白输一次密码。
    if (ownerQq && !silentTried) {
      silentTried = true;
      let silent = null;
      try {
        silent = await bridge.apiPost(`unlock/${key}`, { qq: ownerQq });
      } catch (e) {
        const msg = e.message || "";
        if (!msg.includes("PASSWORD_REQUIRED") && !msg.includes("PASSWORD_WRONG")) {
          // QQ 验签不过 / 文件不存在等：重新问主人 QQ
          const asked = await askQq("QQ 号与日记签名对不上，请重新输入。");
          if (asked === null) {
            toast("已取消");
            return false;
          }
          ownerQq = asked;
          silentTried = false;
          error = "";
          continue;
        }
      }
      if (silent && silent.token) {
        setSessionToken(key, silent.token, silent.expires_in);
        // 没输密码就拿到令牌说明这份日记没有查看密码：只确认 owner，不动 protected
        markOwnerResolved(ownerQq);
        updateLockUI();
        return true;
      }
      if (silent && silent.need_owner) {
        const asked = await askQq();
        if (asked === null) {
          toast("已取消");
          return false;
        }
        ownerQq = asked;
        silentTried = false;
        continue;
      }
      // PASSWORD_REQUIRED：确实有密码，继续下面弹密码框
    }
    const pw = await askDialog({
      title: "这份日记已上锁",
      message: reason || "这份日记设了查看密码，请输入后继续。",
      error,
      password: true,
      placeholder: "4~32 位、不含空格",
      okText: "解锁",
    });
    if (pw === null) {
      toast("已取消");
      return false;
    }
    let r;
    try {
      const body = ownerQq ? { pw, qq: ownerQq } : { pw };
      r = await bridge.apiPost(`unlock/${key}`, body);
    } catch (e) {
      const msg = e.message || "";
      if (msg.includes("PASSWORD_WRONG")) {
        error = "密码不对，请重新输入。";
        continue;
      }
      if (
        msg.includes("OWNER_UNKNOWN") ||
        msg.includes("无法确认日记主人") ||
        msg.includes("QQ 号与日记签名")
      ) {
        const asked = await askQq("QQ 号与日记签名对不上，请重新输入。");
        if (asked === null) {
          toast("已取消");
          return false;
        }
        ownerQq = asked;
        silentTried = false;
        error = "";
        continue;
      }
      toast(msg || "解锁失败");
      return false;
    }
    if (r && r.need_owner) {
      const asked = await askQq("QQ 号与日记签名对不上，请重新输入。");
      if (asked === null) {
        toast("已取消");
        return false;
      }
      ownerQq = asked;
      silentTried = false;
      error = "";
      continue;
    }
    if (r && r.token) {
      setSessionToken(key, r.token, r.expires_in);
      updateLockUI();
      if (ownerQq) markOwnerResolved(ownerQq, { passwordVerified: true });
      return true;
    }
    toast("解锁失败：后端没有返回凭证，请刷新后重试");
    return false;
  }
}

/* 带凭证的 GET（days / day / image 等读接口）：
 * 1) 带上 query `tk`；2) 遇到 200 的 need_owner / need_password 时走页面内闸门
 *    换取 token 后重试；3) 旧口径的 403 PASSWORD_REQUIRED/PASSWORD_WRONG 同样
 *    清缓存凭证再询问。用户取消或无法通过时返回 null（toast 已给出）。 */
async function gatedGet(endpoint, params = {}, { reason = "" } = {}) {
  const request = () => bridge.apiGet(endpoint, { ...params, ...authParams() });
  let r;
  try {
    r = await request();
  } catch (e) {
    const msg = e.message || "";
    if (
      !msg.includes("PASSWORD_REQUIRED") &&
      !msg.includes("PASSWORD_WRONG")
    ) {
      throw e;
    }
    clearSessionToken(currentKey);
    updateLockUI();
    if (!(await acquireToken({ reason }))) return null;
    return request();
  }
  for (let guard = 0; guard < 4; guard += 1) {
    if (r && r.need_owner) {
      clearSessionToken(currentKey);
      updateLockUI();
      if (!(await acquireToken({ needOwner: true, reason }))) return null;
      r = await request();
      continue;
    }
    if (r && r.need_password) {
      clearSessionToken(currentKey);
      updateLockUI();
      if (!(await acquireToken({ reason }))) return null;
      r = await request();
      continue;
    }
    return r;
  }
  toast("读取失败：身份校验没有通过，请点「⟳ 刷新」重试", 5000);
  return null;
}

/* POST 与 gatedGet 共用同一套短期令牌和解锁流程；令牌放在请求体 tk 字段。 */
async function gatedPost(endpoint, body = {}, { reason = "" } = {}) {
  const request = () => bridge.apiPost(endpoint, { ...body, ...authParams() });
  for (let guard = 0; guard < 5; guard += 1) {
    let r;
    try {
      r = await request();
    } catch (e) {
      const msg = e.message || "";
      if (!msg.includes("PASSWORD_REQUIRED") && !msg.includes("PASSWORD_WRONG")) {
        throw e;
      }
      clearSessionToken(currentKey);
      updateLockUI();
      if (!(await acquireToken({ reason }))) return null;
      continue;
    }
    if (r && (r.need_password || r.need_owner)) {
      const needOwner = Boolean(r.need_owner);
      clearSessionToken(currentKey);
      updateLockUI();
      if (!(await acquireToken({ reason, needOwner }))) return null;
      continue;
    }
    return r;
  }
  toast("操作失败：身份校验没有通过，请刷新后重试", 5000);
  return null;
}

/* ---------- 查看密码：顶栏的锁按钮 ---------- */
function updateLockUI() {
  const btn = $("btnLock");
  const u = users.find((x) => x.file_key === currentKey);
  if (!u || !u.protected) {
    btn.hidden = true;
    return;
  }
  const unlocked = tokenAlive(currentKey);
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
  if (tokenAlive(currentKey)) {
    // 重新上锁：只清本地凭证，已显示的内容保持（刷新/翻页后会重新要密码）
    clearSessionToken(currentKey);
    updateLockUI();
    toast("已重新上锁，下次翻阅需要密码");
    return;
  }
  const ok = await acquireToken({
    reason:
      "这份日记设了查看密码（主人在 QQ 里用 /日记密码 设置）。\n" +
      "输入正确后，就能在面板里翻阅和导出这份日记。",
  });
  if (!ok) return;
  toast("已解锁");
  if (dayIdx >= 0) await showDay(dayIdx, false);
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
    // P3-21：读图接口对 need_password / need_owner 返回 200 JSON（不是图片）。
    // 走 gatedGet 的闸门流程：弹密码/QQ 框换取 token 后重试这张图；不能当成
    // 「图片缺失」静默降级成 [插图：…]。
    let res = null;
    try {
      res = await gatedGet(
        `image/${currentKey}/${name}`,
        {},
        { reason: "查看日记插图需要查看密码，请输入。" }
      );
    } catch (e) {
      console.warn("插图读取失败", name, e);
      if (!isCurrentDiaryView(day, token) || !figure.isConnected) return;
      markImageMissing(figure, alt, "");
      continue;
    }
    // 等待（可能含密码/QQ 对话框）期间翻页/切用户了：这张图不能落到当前页
    if (!isCurrentDiaryView(day, token) || !figure.isConnected) return;
    if (!res) {
      // 用户取消了解锁：图其实还在，只是没权限读，不能标成「缺失」
      figure.classList.remove("pending");
      figure.classList.add("img-gated");
      figure.title = "插图需要先解锁查看";
      return;
    }
    if (res.found && res.image) {
      await applyDiaryImage(figure, alt, res);
    } else {
      markImageMissing(figure, alt, res.status);
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
  const warn = u.alert === "tampered" ? "⚠️ " : "";
  const nick = visibleText(u.nickname);
  const qq = String(u.owner || "").trim();
  const days = `${u.day_count} 天`;
  if (nick && qq) return `${warn}${lock}${nick}（QQ ${qq} · ${days}）`;
  if (qq) return `${warn}${lock}QQ ${qq}（${days}）`;
  return `${warn}${lock}${u.nickname}（${days}）`;
}

/* 目录/正文读取失败（P2-18）：toast 后端 message，并把右页置成明确的
 * 「读取失败，请刷新」，同时清掉旧目录/旧正文——失败后继续显示上一个用户的
 * 日记会让人以为看到的就是当前用户。 */
function showDiaryReadFailure(e) {
  const msg = (e && e.message) || "";
  toast(msg || "读取失败，请点「⟳ 刷新」重试", 5000);
  navToken += 1;
  pendingDay = null;
  viewToken += 1; // 作废仍在路上的插图回填
  closeAnnCards();
  days = [];
  dayIdx = -1;
  $("dayList").innerHTML = "";
  $("btnToc").disabled = true;
  $("diaryHeader").innerHTML = "";
  $("diaryBody").textContent = "读取失败，请点「⟳ 刷新」重试。";
  $("pageInfo").textContent = "0 / 0";
  updateBatchDeleteUI();
}

/* 用户列表读取失败：首屏失败时不能只留一片空白，空舞台上给出可读的错误。 */
function showUsersReadFailure(e) {
  const msg = (e && e.message) || "";
  toast(msg || "读取用户列表失败，请点「⟳ 刷新」重试", 5000);
  $("book").hidden = true;
  $("empty").hidden = false;
  $("emptyText").textContent =
    "读取失败，请点「⟳ 刷新」重试" + (msg ? `（${msg}）` : "");
  $("btnExport").disabled = true;
  days = [];
  dayIdx = -1;
  $("dayList").innerHTML = "";
  $("pageInfo").textContent = "0 / 0";
}

async function loadUsers() {
  // 用户列表刷新可能换人 / 清空：旧勾选不能留到新用户的目录上
  clearSelectedDays();
  const r = await bridge.apiGet("users");
  users = r.users || [];
  // v0.7.7：外部删除/篡改告警横幅（登记包凭空消失、全量验签失败）
  const banner = $("alertBanner");
  const alerts = Array.isArray(r.alerts) ? r.alerts.filter(Boolean) : [];
  if (alerts.length) {
    banner.hidden = false;
    banner.textContent = `⚠️ 疑似外部改动：${alerts.join("；")}`;
  } else {
    banner.hidden = true;
    banner.textContent = "";
  }
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
    // 删光日记/空目录：目录与右页一起清空，否则 hidden 掉的书上还残留
    // 刚被删掉的正文、页眉与页码（看起来像删除失败）。
    days = [];
    dayIdx = -1;
    $("dayList").innerHTML = "";
    $("btnToc").disabled = true;
    $("diaryHeader").innerHTML = "";
    $("diaryBody").textContent = "";
    $("pageInfo").textContent = "0 / 0";
    updateLockUI();
    return;
  }
  if (!users.some((u) => u.file_key === currentKey)) {
    currentKey = users[0].file_key;
  }
  sel.value = currentKey;
  updateLockUI();
}

/* 拉取当前用户的日期目录。返回 true 表示成功（含「文件里还没有日记」）。
 * 任何失败都 toast + 右页明确「读取失败，请刷新」，绝不保留上一个用户的目录/
 * 正文（P2-18）；切用户/刷新时也先清空旧内容，避免新旧用户同屏（P2-17）。 */
async function loadDays() {
  // 目录整体刷新（切用户 / 刷新 / 删除成功）：勾选只对旧目录有意义，先清空
  navToken += 1;
  const nav = navToken;
  pendingDay = null;
  clearSelectedDays();
  updateLockUI();
  const u = users.find((x) => x.file_key === currentKey);
  $("ownerHint").hidden = !u || u.owner_known;
  $("ownerHint").textContent = u && !u.owner_known
    ? "这份日记还没绑定主人 QQ，翻开时会请你输入一次（验签通过才接受）"
    : "";
  // 请求前先清右页与目录：请求慢或失败时不会出现「下拉框已是新用户、
  // 正文还是旧用户」的错位页面。
  viewToken += 1;
  closeAnnCards();
  days = [];
  dayIdx = -1;
  $("dayList").innerHTML = "";
  $("btnToc").disabled = true;
  $("diaryHeader").innerHTML = "";
  $("diaryBody").textContent = "正在读取目录…";
  $("pageInfo").textContent = "0 / 0";
  updateBatchDeleteUI();

  let r;
  try {
    r = await gatedGet(`days/${currentKey}`, {}, {
      reason: "翻阅日记目录需要查看密码，请输入。",
    });
  } catch (e) {
    if (nav === navToken) showDiaryReadFailure(e);
    return false;
  }
  if (nav !== navToken) return false; // 期间又切了用户/点了刷新：这次结果作废
  if (!r) {
    // 用户在密码/QQ 对话框点了取消：不当作「没有日记」
    if (nav === navToken) {
      $("diaryBody").textContent = "已取消解锁，请点「⟳ 刷新」重试。";
    }
    return false;
  }
  days = r.days || [];
  renderDayList();
  if (days.length === 0) {
    viewToken += 1; // 清空正文：让还在路上的旧插图请求失效
    $("diaryHeader").innerHTML = "";
    $("diaryBody").textContent = "（这个文件里还没有日记）";
    $("pageInfo").textContent = "0 / 0";
    dayIdx = -1;
    return true;
  }
  await showDay(days.length - 1, false);
  return true;
}

/* 刷新用户列表 + 当前用户目录（顶栏「⟳ 刷新」、导入成功后用）。
 * 失败提示都在内部给出；announce=true 且全部成功才 toast「已刷新」
 * （旧实现在 loadDays 之前就提示，失败时也会报「已刷新」）。 */
async function refreshAll({ announce = false } = {}) {
  try {
    await loadUsers();
  } catch (e) {
    showUsersReadFailure(e);
    return false;
  }
  if (!currentKey) {
    if (announce) toast("已刷新");
    return true;
  }
  const ok = await loadDays();
  if (ok && announce) toast("已刷新");
  return ok;
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

/* 读一天正文：带 token，遇到 need_owner（旧 zip）/need_password 时走页面内
 * 对话框换取 token 后重试（P3-7 契约 3）。返回响应或 null。 */
async function fetchDayWithGates(dayStr) {
  return gatedGet(`day/${currentKey}/${dayStr}`, {}, {
    reason: "这份日记设了查看密码，请输入后翻阅。",
  });
}

async function showDay(i, animate = true) {
  if (i < 0 || i >= days.length) return;
  // 上一次翻页还在路上：记下最后一次，等它结束后再翻（旧实现直接 return，
  // 连按方向键时后几次按键会被静默丢掉）。
  if (loading) {
    pendingDay = { i, animate, nav: navToken };
    return;
  }
  const day = days[i];
  const nav = navToken;
  loading = true;
  inflightDay = i;
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
    // 等待（含密码/QQ 对话框）期间用户可能已切换用户或刷新目录：
    // 代次变了就放弃本次展示，避免旧内容配上新目录的标题/页码。
    if (nav !== navToken) return;
    if (!r) return; // 用户取消或校验失败，提示已在函数内给出
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
    if (nav === navToken) toast(e.message || "读取失败");
  } finally {
    loading = false;
    inflightDay = null;
    // 请求期间积压的翻页：属于当前代次才执行（切用户/刷新会换代次）
    if (pendingDay && pendingDay.nav === navToken) {
      const next = pendingDay;
      pendingDay = null;
      await showDay(next.i, next.animate);
    } else {
      pendingDay = null;
    }
  }
}

/* 翻页入口（按钮 / 方向键共用）：以「正在请求的那一页」为基准，连按两次
 * ←/→ 会连续翻两页，而不是都从还没更新的 dayIdx 算、结果只翻一页。 */
function navigateDay(delta) {
  const base = pendingDay
    ? pendingDay.i
    : inflightDay !== null
      ? inflightDay
      : dayIdx;
  showDay(base + delta, true);
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

/* 发删除请求并处理两道闸门（契约 3）：DELETE 是 POST，宿主 bridge 不能带
 * query，所以 token 放 body 的 `tk` 字段；need_owner（旧 zip 无主人信息）/
 * need_password / PASSWORD_WRONG 都走页面内对话框换 token 后重试。
 * 成功返回响应；用户取消或失败返回 null（提示已在本函数内给出）。 */
async function deleteDaysRequest(daysToDelete) {
  const body = { file_key: currentKey, days: daysToDelete, ...authParams() };
  const passwordReason = "删除日记需要查看密码，请输入。";

  for (let guard = 0; guard < 6; guard += 1) {
    let r;
    try {
      r = await bridge.apiPost("delete_days", body);
    } catch (e) {
      const msg = e.message || "";
      if (msg.includes("PASSWORD_WRONG") || msg.includes("PASSWORD_REQUIRED")) {
        // 主人改过密码 / token 过期：本地凭证作废，重新问密码换 token
        clearSessionToken(currentKey);
        delete body.tk;
        updateLockUI();
        if (!(await acquireToken({ reason: passwordReason, intent: "删除" }))) {
          toast("已取消删除");
          return null;
        }
        Object.assign(body, authParams());
        continue;
      }
      if (
        msg.includes("OWNER_UNKNOWN") ||
        msg.includes("无法确认日记主人") ||
        msg.includes("QQ 号与日记签名")
      ) {
        if (!(await acquireToken({
          needOwner: true,
          reason: passwordReason,
          intent: "删除",
        }))) {
          toast("已取消删除");
          return null;
        }
        Object.assign(body, authParams());
        continue;
      }
      toast(msg || "删除失败", 4500);
      return null;
    }
    // 闸门响应是 HTTP 200（need_owner / need_password），不是错误：换 token 再试
    if (r.need_owner) {
      clearSessionToken(currentKey);
      delete body.tk;
      if (!(await acquireToken({
        needOwner: true,
        reason: passwordReason,
        intent: "删除",
      }))) {
        toast("已取消删除");
        return null;
      }
      Object.assign(body, authParams());
      continue;
    }
    if (r.need_password) {
      clearSessionToken(currentKey);
      delete body.tk;
      updateLockUI();
      if (!(await acquireToken({ reason: passwordReason, intent: "删除" }))) {
        toast("已取消删除");
        return null;
      }
      Object.assign(body, authParams());
      continue;
    }
    // 成功：token / 主人信息已在 acquireToken 内记好（含 P3-20 的锁按钮刷新）
    updateLockUI();
    return r;
  }
  toast("删除失败：身份校验没有通过，请点「⟳ 刷新」后重试", 5000);
  return null;
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
    // missing：后端专门回报「勾选了但 zip 里本来就不存在」的天数。不提示的话，
    // 用户在别处删过某天后会以为这次也一并删干净了（其实那几天本来就没有）。
    const missing = Number(r.missing) || 0;
    const missingNote = missing > 0 ? `，其中 ${missing} 天本来没有日记` : "";
    if (deleted === 0) {
      toast(
        missing > 0
          ? `所选日期没有日记，未做任何改动（其中 ${missing} 天本来没有日记）`
          : "所选日期没有日记，未做任何改动"
      );
    } else {
      toast(
        `已删除 ${deleted} 天日记${missingNote}（剩余 ${remaining} 天）`,
        4500
      );
    }
    // 与「⟳ 刷新」同一顺序：先刷新用户列表（可能换人 / 用户整体消失），
    // 再按刷新后的 currentKey 拉目录；全部删光时 loadUsers 已切到空状态。
    // 失败提示在 refreshAll 内部给出，不会把删除成功的文案覆盖成「已刷新」。
    await refreshAll({ announce: false });
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

  /* 额外的非凭证参数（目前只有旧 zip 的主人 QQ）；token 到发请求时现取。 */
  const extraParams = {};
  const passwordReason = "导出迁移包需要查看密码，请输入。";

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
    extraParams.qq = qq;
  }

  // P1-6：下载（files:download）失败时宿主拿不到后端的 JSON 错误码——response.data
  // 是 Blob，getBridgeErrorMessage 只能取到 axios 的「Request failed with status
  // code 403」，PASSWORD_REQUIRED / PASSWORD_WRONG 在下载路径上永远不会出现。
  // 所以已知这份日记有查看密码、本地又没有 token 时，先主动弹密码框解锁再下载。
  if (u.protected && !tokenAlive(currentKey)) {
    if (!(await acquireToken({ reason: passwordReason }))) {
      toast("已取消导出");
      return;
    }
  }

  const run = async () => {
    const params = { ...extraParams, ...authParams() };
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

  for (let guard = 0; guard < 6; guard += 1) {
    try {
      const r = await run();
      if (extraParams.qq) {
        // 主人 QQ 通过验签：本地更新用户信息，让 🔒 按钮/下拉标签立刻正确
        markOwnerResolved(extraParams.qq);
        toast("QQ 验签通过，已记住这份日记的主人");
      }
      updateLockUI();
      okToast(r);
      return;
    } catch (e) {
      const msg = e.message || "";
      const generic403 = /status code 403\b/i.test(msg);
      const generic400 = /status code 400\b/i.test(msg);
      // 下载路径只有通用 403，按「需要查看密码」回退询问（P1-6）；
      // 「存到本机」走 apiGet，仍可能拿到明确的 PASSWORD_* 错误码。
      if (
        msg.includes("PASSWORD_REQUIRED") ||
        msg.includes("PASSWORD_WRONG") ||
        generic403
      ) {
        clearSessionToken(currentKey);
        updateLockUI();
        if (!(await acquireToken({ reason: passwordReason }))) {
          toast("已取消导出");
          return;
        }
        continue;
      }
      // 旧 zip（说明里没有用户ID）：下载失败只剩通用 400，主人 QQ 未知时重问
      if (
        msg.includes("OWNER_UNKNOWN") ||
        msg.includes("QQ 号与日记签名") ||
        (generic400 && u.owner_known === false)
      ) {
        const qq = await askOwner(
          "导出迁移包需要日记主人的 QQ 号（写入清单并用于验签）。",
          "QQ 号与日记签名对不上，请重新输入。"
        );
        if (qq === null) {
          toast("已取消导出");
          return;
        }
        extraParams.qq = qq;
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
  toast("导出失败：身份校验没有通过，请点「⟳ 刷新」后重试", 5000);
}

/* 迁移包上传上限：与后端 ``upload.save(tmp, max_bytes=32MB)`` 对齐。
 * 超过上限后端必然拒绝；前端先按 file.size 拦下来，免得把几十上百 MB 的
 * 整包读进页面内存再传一遍（标签页会卡顿甚至 OOM）。 */
const IMPORT_MAX_BYTES = 32 * 1024 * 1024;

async function doImport(file) {
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    toast("请选择迁移包 zip 文件");
    return;
  }
  if (file.size > IMPORT_MAX_BYTES) {
    const mb = (file.size / (1024 * 1024)).toFixed(1);
    toast(
      `迁移包 ${mb}MB 超过 32MB 上限：请改用 QQ 指令 /导入日记，` +
        "或按用户拆分后再从页面导入",
      6500
    );
    return;
  }
  try {
    toast("正在验签并导入…");
    const r = await bridge.upload("import", file);
    toast(
      `导入完成：${r.imported} 天日记（${MODE_TEXT[r.mode] || r.mode}）`,
      4500
    );
    try {
      await loadUsers();
      if (currentKey) await loadDays();
    } catch (e2) {
      toast(e2.message || "导入成功，但刷新列表失败，请点「⟳ 刷新」重试", 5000);
    }
  } catch (e) {
    const msg = e.message || "";
    // 宿主 files:upload 固定 60s 超时，但服务端可能已经继续完成导入并落盘；
    // 直接报 timeout 会让用户反复重试，而重复导入会被后端拒绝。
    if (/timeout/i.test(msg)) {
      toast(
        "导入请求超时：后台可能已经导入完成，请先点「⟳ 刷新」确认，确认没有再重试",
        7000
      );
      return;
    }
    toast(msg || "导入失败", 5000);
  }
}

/* ---------- 启动 ---------- */
await bridge.ready();
$("btnFontSmaller").addEventListener("click", () => changeFontSize(-1));
$("btnFontLarger").addEventListener("click", () => changeFontSize(1));
$("btnFontFamily").addEventListener("click", cycleFontFamily);
/* ---------- 本地时间轴 / 待吸收素材 ---------- */
let searchOffset = 0;
let searchTotal = 0;
let activeTool = null;
let materialsLoaded = false;

function syncToolButtons() {
  $("btnSearch").setAttribute("aria-expanded", String(activeTool === "search"));
  $("btnMaterials").setAttribute("aria-expanded", String(activeTool === "materials"));
}

function closeTools(restoreFocus = true) {
  const previous = activeTool;
  $("toolsPanel").hidden = true;
  $("searchTools").hidden = true;
  $("materialTools").hidden = true;
  activeTool = null;
  syncToolButtons();
  if (restoreFocus && previous) {
    $(previous === "search" ? "btnSearch" : "btnMaterials").focus();
  }
}

function openTools(kind) {
  if (!["search", "materials"].includes(kind)) return;
  if (activeTool === kind) {
    closeTools();
    return;
  }
  activeTool = kind;
  $("toolsPanel").hidden = false;
  $("searchTools").hidden = kind !== "search";
  $("materialTools").hidden = kind !== "materials";
  syncToolButtons();
  if (kind === "materials" && !materialsLoaded) {
    materialsLoaded = true;
    void loadMaterials();
  }
  $(kind === "search" ? "searchQuery" : "materialStatus").focus();
}

async function runSearch(reset = true) {
  if (!currentKey) return;
  if (reset) searchOffset = 0;
  const params = {
    q: $("searchQuery").value.trim(), day: $("searchDay").value,
    evidence_type: $("searchEvidenceType").value,
    mode: $("searchMode").value, origin: $("searchOrigin").value,
    has_annotations: $("searchAnnotations").value, has_images: $("searchImages").value,
    offset: searchOffset, ...authParams(),
  };
  try {
    const r = await gatedGet(`search/${currentKey}`, params, {
      reason: "搜索日记需要查看密码，请输入。",
    });
    if (r === null) return;
    searchTotal = r.total || 0;
    if (reset) $("searchResults").innerHTML = "";
    const html = (r.results || []).map((item) => {
      const evidenceType = escapeAttr(item.evidence_type || "unknown");
      const sourceNote = item.source_status === "partial" ? " · 来源未完整匹配" :
        (item.source_status === "tampered" ? " · 来源侧车验签失败" : "");
      return `<article class="evidence-card"><button class="evidence-day" data-day="${escapeAttr(item.day)}">${escapeHtml(item.day)} · ${escapeHtml(item.source)} · ${escapeHtml(item.scope)}</button>` +
        `<div class="evidence-mode"><span class="evidence-badge evidence-${evidenceType}">${escapeHtml(item.evidence_label || "未分级（旧记录）")}</span>${item.has_explicit ? '<span class="evidence-explicit">显式交代</span>' : ""} · ${escapeHtml(item.mode)}${item.has_annotations ? " · 有批注" : ""}${item.has_images ? " · 有插图" : ""}${sourceNote}</div>` +
        `<p>${escapeHtml(item.snippet)}</p><small>${escapeHtml(item.entry)}</small></article>`;
    }).join("");
    $("searchResults").insertAdjacentHTML("beforeend", html);
    $("searchStatus").textContent = `共 ${searchTotal} 条证据${r.tampered ? "；部分篡改条目已跳过，来源异常标为未分级" : ""}。无命中不代表没有相关记忆。`;
    searchOffset = r.next_offset === null || r.next_offset === undefined ? searchTotal : r.next_offset;
    $("btnSearchMore").hidden = searchOffset >= searchTotal;
  } catch (e) {
    $("searchStatus").textContent = `搜索失败：${e.message || e}`;
  }
}

async function loadMaterials() {
  if (!currentKey) return;
  try {
    const r = await gatedGet(`materials/${currentKey}`, {
      status: $("materialStatus").value, ...authParams(),
    }, { reason: "查看待吸收素材需要查看密码，请输入。" });
    if (r === null) return;
    $("materialResults").innerHTML = (r.items || []).map((item) =>
      `<article class="evidence-card"><div>${escapeHtml(item.source)} · ${escapeHtml(item.scope)} · ${escapeHtml(item.status)}</div>` +
      `<small>${escapeHtml(item.submitted_at)} · ${escapeHtml(item.id)}</small><p>${escapeHtml(item.text || "（正文已过期清除）")}</p>` +
      `<small>${escapeHtml(item.reason || "")}${item.absorbed_entry ? " · " + escapeHtml(item.absorbed_entry) : ""}</small>` +
      (["pending", "deferred"].includes(item.status) ? ` <button class="ctl material-action" data-id="${escapeAttr(item.id)}" data-action="discard">明确丢弃</button>` : "") +
      (item.status === "deferred" ? ` <button class="ctl material-action" data-id="${escapeAttr(item.id)}" data-action="retry">重试</button>` : "") + `</article>`
    ).join("") || "<p>当前筛选下没有素材。</p>";
  } catch (e) {
    $("materialResults").textContent = `素材读取失败：${e.message || e}`;
  }
}

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
for (const item of document.querySelectorAll(".paper-item[data-kind]")) {
  item.addEventListener("click", () =>
    onImageMenuClick(item.dataset.kind, item.dataset.paper)
  );
}
for (const item of document.querySelectorAll(".theme-item[data-paper-theme]")) {
  item.addEventListener("click", () => {
    void setPaperTheme(item.dataset.paperTheme);
  });
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
  materialsLoaded = false;
  closeTools(false);
  $("searchResults").innerHTML = "";
  $("searchStatus").textContent = "";
  searchOffset = 0;
  searchTotal = 0;
  // 切用户：loadDays 内部会先换代次（navToken）并清空旧目录/正文，在途的
  // 翻页结果会被丢弃，不会出现「目录是新用户、正文是旧用户」的错位页面。
  await loadDays();
});
$("btnRefresh").addEventListener("click", async () => {
  // 全部成功后才 toast「已刷新」；失败提示由 refreshAll 内部按后端 message 给出
  await refreshAll({ announce: true });
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
$("btnPrev").addEventListener("click", () => navigateDay(-1));
$("btnNext").addEventListener("click", () => navigateDay(1));
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
  if (e.key === "Escape" && activeTool) {
    e.preventDefault();
    closeTools();
    return;
  }
  // 焦点在表单控件里时，方向键属于控件本身：在「选择用户」下拉框上按 ←/→
  // 会切换用户（原生行为），若再冒泡到这里还会同时翻页 → 左右两页来自不同用户。
  const target = e.target;
  if (
    target &&
    target.closest &&
    target.closest("select, input, textarea, [contenteditable]")
  ) {
    return;
  }
  if (e.key === "ArrowLeft") navigateDay(-1);
  if (e.key === "ArrowRight") navigateDay(1);
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
$("btnSearch").addEventListener("click", () => openTools("search"));
$("btnMaterials").addEventListener("click", () => openTools("materials"));
$("btnRunSearch").addEventListener("click", () => runSearch(true));
$("btnSearchMore").addEventListener("click", () => runSearch(false));
$("searchQuery").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runSearch(true);
});
$("searchResults").addEventListener("click", async (event) => {
  const button = event.target.closest(".evidence-day");
  if (!button) return;
  const index = days.findIndex((item) => item.day === button.dataset.day);
  if (index >= 0) await showDay(index);
});
$("btnLoadMaterials").addEventListener("click", loadMaterials);
$("materialStatus").addEventListener("change", loadMaterials);
$("materialResults").addEventListener("click", async (event) => {
  const button = event.target.closest(".material-action");
  if (!button) return;
  try {
    const r = await gatedPost(`materials/${currentKey}/action`, {
      id: button.dataset.id, action: button.dataset.action,
    }, { reason: "处理待吸收素材需要查看密码，请输入。" });
    if (!r || r.ok !== true) {
      if (r) toast("操作失败：素材状态没有改变");
      return;
    }
    await loadMaterials();
  } catch (e) {
    toast(`操作失败：${e.message || e}`);
  }
});

try {
  await loadUsers();
} catch (e) {
  // 首屏失败不能只留一片空白：空舞台上显示可读的错误与「刷新」指引
  showUsersReadFailure(e);
}
if (currentKey) await loadDays();
updateLockUI();
