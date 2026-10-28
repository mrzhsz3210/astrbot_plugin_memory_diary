# memory_diary 差分回归测试（tests/）

这套测试把**重构前的原始插件**（`baseline/astrbot_plugin_memory_diary`）和**当前实现**
（仓库根目录；内部重构工作区里是 `work/astrbot_plugin_memory_diary`）同时加载进同一个进程，
用相同的场景各跑一遍，然后比较：

* 场景返回值 / 抛出异常的类型与消息；
* 插件数据目录下所有确定性文件的摘要（zip 按条目内容比较，并归一化绝对路径）；
* 每个场景内部对关键语义的断言（两种实现都必须满足同样的行为约定）。

测试是**只读**的：不会修改 `baseline/` 或当前实现里的任何源码；两个插件都会被复制到系统临时目录后导入，
数据目录也都在临时目录里，不触碰真实 AstrBot 数据。

---

## 运行方式（推荐，无需 pytest）

AstrBot 启动器的 venv 里通常没有 pytest，所以提供了独立 runner。在**仓库根目录**执行：

```powershell
# Windows：用 AstrBot Launcher 实例的 venv
& "$env:USERPROFILE\.astrbot_launcher\instances\<实例UUID>\venv\Scripts\python.exe" tests\run.py
```

```bash
# Linux / macOS（任意能 import astrbot 的 Python）
python3 tests/run.py
```

runner 需要一个能 `import astrbot` 的 Python 环境：

- 用 AstrBot Launcher 的 venv 运行时，会自动找到 `~/.astrbot_launcher/instances/*/core`
  并加入 `sys.path`（取第一个含 `astrbot/__init__.py` 的实例）；
- 换机器、换实例或用自己的 AstrBot 源码时，用环境变量指定：

```powershell
$env:ASTRBOT_CORE = "D:\path\to\astrbot\core"   # Windows
```

```bash
export ASTRBOT_CORE=/path/to/astrbot/core       # Linux / macOS
```

测试会把 `ASTRBOT_ROOT` 指向临时目录，所以 `astrbot` 导入时自动生成的 `data/`
（默认 `cmd_config.json` 与 `t2i_templates`）不会落在仓库里。

常用参数：

```powershell
... tests\run.py --list                 # 列出所有测试
... tests\run.py -k storage             # 只跑 id 含 storage 的测试
... tests\run.py -v                     # 失败时打印完整 traceback
... tests\run.py --fail-fast            # 第一个失败就停
... tests\run.py --baseline-only        # 只跑 baseline 的健全性检查（不导入 refactor）
```

`--baseline-only` 模式用于**重构进行中**：此时只实例化原始插件，场景断言照常执行，
但跳过“需要 baseline + refactor 双方”的 5 个加载测试和 1 个交叉迁移包测试，并跳过双方文件比较。
这样即使当前实现正在编辑、甚至暂时无法 import，也能确认原始行为基线。

退出码：0 = 全部通过 / 跳过；1 = 有失败。

### 可选 pytest

如果另一个环境装了 `pytest` + `pytest-asyncio`，也可以（先按上面的方式让 AstrBot 可导入）：

```powershell
& <python-with-pytest> -m pytest tests -q
```

`tests/conftest.py` 负责把 `tests/` 加入 `sys.path` 并注入 AstrBot core；
`tests/pytest.ini` 已设置 `asyncio_mode = auto`。未安装这两个包时请使用上面的独立 runner。

---

## 本次运行结果（harness 自检）

2026-10-28 时点、v0.5.0 / `main`：

* 差分模式：`71 passed, 0 failed, 0 skipped`（约 6 秒，Windows + Python 3.12.15）
* baseline-only：`65 passed, 0 failed, 6 skipped`（6 个 skip 均为“需要双方”的显式跳过）

---

## 目录结构

```
tests/
├── run.py                 # 独立 runner（发现/过滤/执行/统计/退出码）
├── conftest.py            # 可选 pytest 引导
├── pytest.ini             # 可选 pytest 配置（asyncio_mode=auto）
├── README.md
├── harness/
│   ├── env.py             # 路径发现（仓库根目录 / AstrBot core / ASTRBOT_CORE 覆盖）
│   ├── loader.py          # 两个命名空间 basepkg/newpkg 的插件装载
│   ├── world.py           # 一个 World = baseline + refactor 两套隔离实例 + 差分 runner
│   ├── fakes.py           # FakeContext/Provider/Event/ConversationManager/WebRequest/Upload
│   ├── session.py         # 把 session_waiter 换成可脚本化的 fake（驱动交互式分支）
│   ├── compare.py         # 返回值比较、数据目录快照、zip 条目摘要
│   └── errors.py          # SkipTest
├── test_load.py           # AstrBot 真实注册/模块路径/handler 元数据/实例化
├── test_storage.py        # 存储/签名/篡改/损坏隔离/迁移/偏好/备忘/素材游标
├── test_commands.py       # 全部聊天指令 + 交互式分支 + 导出/导入 + 人设口吻
├── test_web.py            # 日记本 Web API（列表/读取/导出/导入/偏好/图片/密码闸）
├── test_llm_request.py    # on_llm_request 注入、reset 代际、群聊过滤、哨兵阈值
├── test_auto_diary.py     # 每天定时自动写日记 tick、用户登记、失败通知、生命周期
└── test_bundle.py         # 迁移包校验/安装、换钥匙重签名、跨实现互导
```

## 差分机制

1. **两个独立命名空间**：把 `baseline/...` 和当前实现（仓库根目录，或重构工作区的 `work/...`）分别复制成
   `basepkg.astrbot_plugin_memory_diary`、`newpkg.astrbot_plugin_memory_diary` 后导入，
   让 `@register` / `@filter.command` / `@filter.on_llm_request` 都走 AstrBot 真实装饰器，
   相对 import 也各自落在自己的包里。`star_map` 与 `star_handlers_registry` 中会出现两条
   互不覆盖的注册记录。
2. **隔离数据目录**：每个实例把 `ASTRBOT_ROOT` 指向自己的临时根目录，
   `StarTools.get_data_dir()` 因此得到 `.../data/plugin_data/astrbot_plugin_memory_diary`。
3. **同条件**：两个数据目录预先写入**逐字节相同的 `.diary_key`**，因此
   `diary_<HMAC>.zip` 文件名、条目 HMAC 签名、`说明.txt` 都相同；
   实例的 `_now()` 被冻结在 `2026-10-23 20:30:00`，写日记/迁移/导出/自动任务的时间戳一致。
4. **快照比较**：数据目录中每个文件取摘要；`.zip` 按“条目名 + 归一化后条目内容”取摘要，
   并先把数据目录/WORLD_TMP 绝对路径替换成占位符，避免两边路径不同造成假差异。
   导出包随机文件名、PBKDF2 随机盐等**设计上随机**的内容，通过排除规则或 normaliser
   归一化后再比较（测试里能看到显式的 `snapshot_normalise=` / `snapshot_exclude=`）。
5. **场景错误不静默通过**：场景里未捕获的异常会直接判失败（避免“两边都因为测试 bug 报错所以相等”）。

## 覆盖清单

* **加载**：真实 AstrBot API 导入、`star_map` 名称/版本/模块路径、15 个 handler 的模块路径、
  `filter.command` handler 为 async generator、`on_llm_request` 为 coroutine、
  `metadata.yaml` 与注册信息一致、两个实例的 data_dir/Web API 路由。
* **存储**：full/brief/life 三种段落、同日追加、简记吸收标记与注入过滤、私聊/群聊场景隔离；
  HMAC 派生文件名、旧 MD5 文件名自动改名；`.diary_key` fail-closed（密钥不可用时拒绝写/读）；
  篡改条目“读取拒绝 + 整次保存拒绝”；损坏 zip 的 CRC 检测与 `.corrupt-<时间>.zip` 隔离；
  删除单天/区间/全部；注入范围前缀 `latest/all/days:N/range:` 解析与选择；
  备忘每场景 20 条上限、场景过滤、写日记后按场景吸收、闭合标签消毒；
  素材游标只读取新增历史、`last_extra_hash` 去重、偏好/游标/素材哈希落盘；
  v1 明文 `.md+.sig`、v2 单篇 zip、v3 平铺按天、v3 按篇命名四种迁移，含验签失败跳过。
* **指令**（直接调用 handler，消费 async generator；yield 与 `event.send` 的消息都收集）：
  帮助、群聊 @ 防冲突/`group_require_at`；备忘增/查/删（序号、全部、取消、非交互降级）；
  `/日记` 首次完整→之后简记、完整名额上限、操作冷却、`write_min_ratio` 门槛、人设口吻、
  provider 缺失/报错、无素材时 life 随笔与同日去重；查看/删除日记的列表 + 序号/日期/区间/
  全部二次确认/取消/超时/非交互降级；状态、范围设置、主动注入去重；
  日记密码 PBKDF2 设置/修改/取消/错误分支；导出命令（生成真实迁移包、manifest、
  user_key、发送后清理临时包）；导入命令（成功、重复、错主人、篡改包、取消、无文件、v1 包）。
* **Web API**（handler 内绑定 AstrBot `request` contextvar，用 fake request 驱动）：
  用户列表/日期列表/读取单日、`说明.txt` 缺 QQ 时的 owner 解析（qq hint 正确/错误/owner_map 记住）、
  查看密码闸（无密码/错密码/正确密码，读与下载两条路径）、导出下载、导出到本机下载目录
  （`Path.home()` 与 `Popen` 被替换，避免写真实 Downloads/弹资源管理器）、Web 导入（成功/坏包/
  缺文件/重复）、界面偏好读写与白名单钳制、自定义纸张/背景板上传读取（mime/3MB 上限/无文件）。
* **LLM 请求**：临时注入只在会话首个请求一次；`persist_inject` 每轮进 system_prompt；
  手动 `/注入日记` 在关闭自动注入时仍一次性生效并记录已读篇目；`/reset` 指令标记、
  `req.contexts` 清空、conversation_id 变化三种“新代际”都会重新注入；
  群聊注入过滤；`inject_max_chars` 截断提示与只保留最近篇目；
  哨兵提醒阈值、自动写日记阈值（写成功后同会话不再重复）、无新素材时提示 `/reset`、
  关闭哨兵/窗口为 0；阈值颠倒截断、越界钳制、token 估算、`TextPart` 不可用时回退 system_prompt。
* **自动写日记**：开关/时间未到/时间格式错误/超过补触发窗口都不触发；到点只触发一次并落盘
  `auto_diary_state.json`；登记用户逐个写、当天已写完整篇跳过、无素材静默跳过、正在写跳过；
  成功用固定文案主动私聊、失败只通知一次；从会话库补齐私聊用户（群聊不补）；`initialize()`
  加载并启动后台任务、`terminate()` 取消任务并清空全部内存状态。
* **迁移包**：`_verify_bundle` 对坏 zip、缺 manifest、格式版本不支持、缺 key、缺日记、
  内层验签失败、缺签名、空日记全部拒绝；`_install_bundle` 的 same_key / resign 两种模式、
  错主人拒绝且不落盘；baseline 导出的包含已用 baseline 自己的密钥签名，refactor 能原样导入
  （反向亦然），两边最终数据目录摘要一致。

## 测试环境的边界（诚实说明，没有静默跳过）

以下内容**没有被真实端到端覆盖**，属于已知覆盖缺口：

* 没有真实 QQ/NapCat 平台、没有走 ASGI 路由与 Dashboard 鉴权；指令和 Web handler 都是
  直接调用（但装饰器注册、`request` contextvar、响应对象都是 AstrBot 真实实现）。
* 交互式流程用 `tests/harness/session.py` 里可脚本化的 `session_waiter` 假实现驱动，
  真实 60/120 秒超时只通过伪造 `TimeoutError` 覆盖；没有真实事件循环里的并发等待。
* 自动写日记的后台循环 `_auto_diary_loop` 的 5s/20s 调度没有被真实时钟测试；
  测试直接调用 `_auto_diary_tick()`（触发判定、去重、写盘、通知逻辑都真实执行）。
* `web_export_local` 的“保存到真实下载目录 + 打开资源管理器”被替身覆盖（为了不污染测试机），
  只验证移动文件、命名冲突处理与 JSON 响应的逻辑。
* 文件权限（POSIX `0o600`）在 Windows 上无法验证；`fail-closed` 是通过让密钥路径不可读
  （同名目录）来触发，而不是真实 chmod。
* 人设（persona）用一个假 persona manager 提供静态 prompt；没有接入真实人格库。
* 没有多进程/并发压力、没有真实长时间运行后的状态上限（512 条）行为验证。

## 恢复“原始行为”时注意的既有约定（不是 bug，重构需保留）

这些行为在 baseline 与 refactor 中都被测试固定下来：

* 首次写日记默认写“完整”，之后默认“简记”；同一场景每天完整篇默认最多 1 篇。
* 写入前逐条验签：发现被篡改/缺签名的既有日记 → **整次保存拒绝**（`DiaryTamperedError`），
  绝不替被改过的内容重新签名。
* 损坏 zip → 先改名隔离成 `*.corrupt-<YYYYmmdd_HHMMSS>.zip` 再拒绝写入；读取路径也不把它当“空日记”。
* 迁移包导入：QQ 指令路径要求包内主人 QQ 与发送者一致；已存在本地日记 zip 时拒绝覆盖；
  Web 导入是管理员操作，允许换绑原主人入口，但仍用本机密钥重新签名。
* 群聊注入过滤只过滤“写于私聊”的片段；旧格式无场景标记的片段按私聊处理（宁可不注入）。
* 指令在群聊里按 `bot_call_names`/At 组件/`group_require_at` 决定是否响应；
  群聊里查看/删除/导出/导入/密码等隐私操作一律拒绝并提示私聊。
* 无新素材时 `/日记` 默认写“她的日常随笔”，同一天只写一篇；关闭该配置后返回固定文案。
* 非交互环境（`_SESSION_WAITER_OK=False`）时：查看日记降级为直接显示最新一篇，
  删除日记明确拒绝改动，导入提示去 Web 页面——都不静默改数据。

## 如何新增场景

1. 在对应 `test_*.py` 里写 `async def test_xxx(): async with World(config={...}) as world:`
2. 在 `scenario(inst)` 里只通过 `inst`（`inst.plugin`、`inst.provider`、`inst.ctx`、
   `inst.set_history()`、`inst.set_replies()`、`inst.set_now()`）操作，返回可 JSON 化/可比较的 dict；
3. `pair = await world.run_pair(scenario, label="...")`：它会自动比较双方返回值与数据目录；
   需要归一化随机内容时传 `snapshot_exclude=` / `snapshot_normalise=`；
4. 再用 `pair.value` 对**语义**做显式断言（两边都必须满足）；需要故意验证异常时，
   在 scenario 内部 `try/except` 并把类型/消息放进返回值，而不是让异常冒出去。

> 注意：loader 在**每次进程启动时**才复制插件源码，运行中途改的代码不会被重新加载。
> 想验证最新改动，请重新运行 `tests/run.py`，不要复用旧进程。
