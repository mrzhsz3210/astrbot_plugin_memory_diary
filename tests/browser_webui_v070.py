"""真实 Edge/CDP 回归：工具折叠、四组主题、刷新持久化与保存失败。

运行（需要 Windows Edge 与 AstrBot 环境中的 aiohttp）：
    <带 AstrBot 依赖的 python> tests/browser_webui_v070.py
本脚本由 tests/run.py 之外的显式入口运行，不依赖 pytest。
"""

from __future__ import annotations

import asyncio
import functools
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiohttp


ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / "views" / "diary-book"
EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)

MOCK = r"""
document.documentElement.dataset.theme = localStorage.getItem("diaryTestMode") || "light";
window.__saveBodies = [];
window.__materialLoads = 0;
window.__searchParams = null;
window.__failSave = false;
window.AstrBotPluginView = {
  ready: async () => {},
  apiGet: async (endpoint, params = {}) => {
    if (endpoint === "prefs") {
      const fallback = {font_size: 16, font_family: "kai", paper: "default", backdrop: "default", paper_theme: "warm"};
      return {prefs: JSON.parse(localStorage.getItem("diaryTestPrefs") || JSON.stringify(fallback)), brand: "测试"};
    }
    if (endpoint === "users") return {users: [{file_key: "0123456789abcdef", nickname: "阿罗", owner: "10001", owner_known: true, protected: false, day_count: 1, latest: "2026-11-07", size: 100}]};
    if (endpoint.startsWith("days/")) return {days: [{day: "20261107", label: "2026-11-07"}]};
    if (endpoint.startsWith("day/")) return {tampered: false, label: "2026-11-07", content: "今天讨论发布"};
    if (endpoint.startsWith("materials/")) { window.__materialLoads += 1; return {items: [], status: "missing"}; }
    if (endpoint.startsWith("search/")) {
      window.__searchParams = JSON.parse(JSON.stringify(params));
      return {results: [{day: "20261107", source: "私聊", scope: "private", entry: "diaries/20261107.md", mode: "full", evidence_type: "mixed", evidence_label: "混合段落", has_explicit: true, source_status: "ok", snippet: "用户相关部分〔她的私人随笔〕"}], total: 1, next_offset: null, tampered: false};
    }
    return {found: false};
  },
  apiPost: async (endpoint, body) => {
    if (endpoint === "prefs/save") {
      window.__saveBodies.push(JSON.parse(JSON.stringify(body)));
      if (window.__failSave) throw new Error("模拟保存失败");
      localStorage.setItem("diaryTestPrefs", JSON.stringify(body.prefs));
      return {saved: true, prefs: body.prefs};
    }
    return {ok: true};
  },
  upload: async () => ({saved: true}),
  download: async () => ({filename: "test.zip"}),
};
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def call(ws, counter, method, params=None):
    message_id = counter[0] = counter[0] + 1
    await ws.send_json({"id": message_id, "method": method, "params": params or {}})
    while True:
        message = await ws.receive_json()
        if message.get("id") == message_id:
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result", {})


async def evaluate(ws, counter, expression):
    result = await call(
        ws,
        counter,
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    if result.get("exceptionDetails"):
        raise AssertionError(result["exceptionDetails"])
    return result.get("result", {}).get("value")


async def wait_ready(ws, counter):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ready = await evaluate(
            ws,
            counter,
            "Boolean(document.querySelector('#book') && document.documentElement.dataset.paperTheme)",
        )
        if ready:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("页面初始化超时")


async def select_theme(ws, counter, theme):
    return await evaluate(
        ws,
        counter,
        f"""(function(){{
          document.getElementById('btnPaper').click();
          document.querySelector('.theme-item[data-paper-theme="{theme}"]').click();
          const root = document.documentElement;
          const style = getComputedStyle(root);
          function lum(hex) {{
            const h = hex.trim().replace('#', '');
            const channels = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255)
              .map(v => v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
          }}
          function contrast(color) {{
            const values = [lum(style.getPropertyValue('--paper')), lum(style.getPropertyValue(color))].sort((a, b) => b - a);
            return (values[0] + 0.05) / (values[1] + 0.05);
          }}
          return {{
            theme: root.dataset.paperTheme,
            paper: style.getPropertyValue('--paper').trim(),
            body: getComputedStyle(document.body).backgroundColor,
            contrasts: ['--ink', '--ink-soft', '--accent'].map(contrast),
            active: document.querySelector('.theme-item[data-paper-theme="{theme}"]').classList.contains('active'),
            pressed: document.querySelector('.theme-item[data-paper-theme="{theme}"]').getAttribute('aria-pressed'),
            otherPressed: document.querySelector('.theme-item[data-paper-theme="{("warm" if theme == "classic" else "classic")}"]').getAttribute('aria-pressed')
          }};
        }})()""",
    )


async def run_browser_test():
    edge = next((path for path in EDGE_CANDIDATES if path.is_file()), None)
    if edge is None:
        raise RuntimeError("未找到 Microsoft Edge，无法运行真实浏览器回归")
    tmp = Path(tempfile.mkdtemp(prefix="memory_diary_webui_browser_"))
    webroot = tmp / "www"
    shutil.copytree(VIEW, webroot)
    index = webroot / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            '<script type="module"', f"<script>{MOCK}</script>\n<script type=\"module\""
        ),
        encoding="utf-8",
    )
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(webroot))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = free_port()
    profile = tmp / "edge-profile"
    proc = subprocess.Popen(
        [
            str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with aiohttp.ClientSession() as session:
            targets = None
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                try:
                    async with session.get(f"http://127.0.0.1:{port}/json/list") as response:
                        targets = await response.json()
                    if targets:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            if not targets:
                raise RuntimeError("Edge CDP 启动超时")
            page = next(target for target in targets if target.get("type") == "page")
            async with session.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                counter = [0]
                await call(ws, counter, "Page.enable")
                await call(ws, counter, "Runtime.enable")
                await call(
                    ws, counter, "Emulation.setDeviceMetricsOverride",
                    {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False},
                )
                url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
                await call(ws, counter, "Page.navigate", {"url": url})
                await wait_ready(ws, counter)

                state = await evaluate(ws, counter, """(function(){
                  document.getElementById('btnSearch').click();
                  return {panel: !toolsPanel.hidden, search: !searchTools.hidden,
                    materials: !materialTools.hidden,
                    aria: btnSearch.getAttribute('aria-expanded'), focus: document.activeElement.id};
                })()""")
                assert state == {"panel": True, "search": True, "materials": False, "aria": "true", "focus": "searchQuery"}
                evidence = await evaluate(ws, counter, """(async function(){
                  searchQuery.value = '翻出来'; searchEvidenceType.value = 'all'; btnRunSearch.click();
                  await new Promise(resolve => setTimeout(resolve, 100));
                  const badge = document.querySelector('.evidence-badge');
                  return {params: __searchParams, badge: badge && badge.textContent,
                    badgeClass: badge && badge.className, explicit: document.querySelector('.evidence-explicit')?.textContent,
                    snippet: document.querySelector('.evidence-card p')?.textContent,
                    badgeColor: badge && getComputedStyle(badge).color,
                    accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()};
                })()""")
                assert evidence["params"]["evidence_type"] == "all" and evidence["params"]["q"] == "翻出来"
                assert evidence["badge"] == "混合段落" and "evidence-mixed" in evidence["badgeClass"]
                assert evidence["explicit"] == "显式交代" and "〔她的私人随笔〕" in evidence["snippet"]
                assert evidence["badgeColor"] and evidence["accent"]
                state = await evaluate(ws, counter, """(function(){
                  btnSearch.click(); return {panel: toolsPanel.hidden, search: searchTools.hidden,
                    materials: materialTools.hidden, aria: btnSearch.getAttribute('aria-expanded'),
                    focus: document.activeElement.id, height: toolsPanel.getBoundingClientRect().height};
                })()""")
                assert state == {"panel": True, "search": True, "materials": True, "aria": "false", "focus": "btnSearch", "height": 0}

                state = await evaluate(ws, counter, """(async function(){
                  btnSearch.click(); btnMaterials.click();
                  await new Promise(resolve => setTimeout(resolve, 100));
                  return {search: !searchTools.hidden, materials: !materialTools.hidden,
                    searchAria: btnSearch.getAttribute('aria-expanded'),
                    materialsAria: btnMaterials.getAttribute('aria-expanded'), loads: __materialLoads};
                })()""")
                assert state == {"search": False, "materials": True, "searchAria": "false", "materialsAria": "true", "loads": 1}
                state = await evaluate(ws, counter, """(async function(){
                  btnMaterials.click(); btnMaterials.click();
                  await new Promise(resolve => setTimeout(resolve, 100));
                  return {panel: toolsPanel.hidden, loads: __materialLoads};
                })()""")
                assert state == {"panel": False, "loads": 1}, state

                state = await evaluate(ws, counter, """(function(){
                  btnSearch.click();
                  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                  return {panel: toolsPanel.hidden, focus: document.activeElement.id};
                })()""")
                assert state == {"panel": True, "focus": "btnSearch"}
                state = await evaluate(ws, counter, """(function(){
                  btnSearch.click(); dialogMask.hidden = false;
                  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                  const afterModal = {modal: dialogMask.hidden, panel: toolsPanel.hidden};
                  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                  return {afterModal: afterModal, panel: toolsPanel.hidden};
                })()""")
                assert state == {"afterModal": {"modal": True, "panel": False}, "panel": True}

                all_styles = {}
                for mode in ("light", "dark"):
                    await evaluate(
                        ws, counter,
                        f"document.documentElement.dataset.theme='{mode}'; localStorage.setItem('diaryTestMode','{mode}'); true",
                    )
                    warm = await select_theme(ws, counter, "warm")
                    classic = await select_theme(ws, counter, "classic")
                    assert warm["theme"] == "warm" and classic["theme"] == "classic"
                    assert warm["paper"] != classic["paper"] or warm["body"] != classic["body"]
                    assert classic["active"] and classic["pressed"] == "true" and classic["otherPressed"] == "false"
                    assert min(warm["contrasts"] + classic["contrasts"]) >= 4.5
                    all_styles[mode] = {"warm": warm, "classic": classic}
                    await asyncio.sleep(0.65)
                    persisted = await evaluate(
                        ws, counter,
                        "JSON.parse(localStorage.getItem('diaryTestPrefs')).paper_theme",
                    )
                    assert persisted == "classic"
                    bodies = await evaluate(ws, counter, "__saveBodies")
                    assert bodies and bodies[-1]["prefs"]["paper_theme"] == "classic"
                    await call(ws, counter, "Page.reload", {"ignoreCache": True})
                    await wait_ready(ws, counter)
                    restored = await evaluate(
                        ws, counter,
                        "({theme: document.documentElement.dataset.paperTheme, paper: getComputedStyle(document.documentElement).getPropertyValue('--paper').trim()})",
                    )
                    assert restored == {"theme": "classic", "paper": classic["paper"]}

                failure = await evaluate(ws, counter, """(async function(){
                  window.__failSave = true;
                  btnPaper.click(); document.querySelector('.theme-item[data-paper-theme="warm"]').click();
                  await new Promise(resolve => setTimeout(resolve, 700));
                  return {text: toast.textContent, visible: !toast.hidden,
                    theme: document.documentElement.dataset.paperTheme,
                    saved: JSON.parse(localStorage.getItem('diaryTestPrefs')).paper_theme};
                })()""")
                assert failure["visible"] and "保存失败" in failure["text"]
                assert failure["theme"] == "warm" and failure["saved"] == "classic"

                custom = await evaluate(ws, counter, """(function(){
                  const root = document.documentElement;
                  root.style.setProperty('--d-paper-image', 'url("data:image/png;base64,AA==")');
                  root.style.setProperty('--d-backdrop-image', 'url("data:image/png;base64,AA==")');
                  book.classList.add('custom-paper'); document.body.classList.add('custom-backdrop');
                  return {paper: getComputedStyle(pageRight).backgroundImage,
                    backdrop: getComputedStyle(document.body).backgroundImage,
                    wash: getComputedStyle(root).getPropertyValue('--paper-wash')};
                })()""")
                assert "linear-gradient" in custom["paper"] and "data:image" in custom["paper"]
                assert "data:image" in custom["backdrop"] and custom["wash"].strip()

                await call(
                    ws, counter, "Emulation.setDeviceMetricsOverride",
                    {"width": 375, "height": 800, "deviceScaleFactor": 1, "mobile": False},
                )
                narrow = await evaluate(ws, counter, """(async function(){
                  if (!toolsPanel.hidden) {
                    if (!searchTools.hidden) btnSearch.click(); else btnMaterials.click();
                  }
                  const collapsed = {scrollWidth: document.documentElement.scrollWidth,
                    panelDisplay: getComputedStyle(toolsPanel).display,
                    panelHeight: toolsPanel.getBoundingClientRect().height};
                  btnSearch.click(); btnRunSearch.click();
                  await new Promise(resolve => setTimeout(resolve, 100));
                  const card = document.querySelector('.evidence-card');
                  return {width: innerWidth, collapsed: collapsed,
                    openScrollWidth: document.documentElement.scrollWidth,
                    panelHeight: toolsPanel.getBoundingClientRect().height,
                    filterWidth: searchEvidenceType.getBoundingClientRect().width,
                    cardWidth: card && card.getBoundingClientRect().width};
                })()""")
                assert narrow["collapsed"]["scrollWidth"] <= narrow["width"]
                assert narrow["collapsed"]["panelDisplay"] == "none" and narrow["collapsed"]["panelHeight"] == 0
                assert narrow["openScrollWidth"] <= narrow["width"] and narrow["panelHeight"] > 0
                assert narrow["filterWidth"] > 0 and narrow["cardWidth"] > 0
                print(json.dumps({
                    "fold": "passed",
                    "themes": all_styles,
                    "save_failure": "passed",
                    "custom_images": "passed",
                    "narrow_375": narrow,
                }, ensure_ascii=False, indent=2))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(run_browser_test())
