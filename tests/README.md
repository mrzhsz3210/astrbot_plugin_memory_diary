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
... tests\run.py --baseline-only        # 只做 baseline 的健全性检查（差分场景不比较 refactor）
```

`--baseline-only` 模式用于**重构进行中**：差分场景只实例化原始插件、跳过双方比较，
并跳过“需要 baseline + refactor 双方”的 5 个 `test_load` + 1 个 `test_bundle` 交叉迁移包
+ 2 个 `test_compat_v054_v055` 用例（共 8 条）。
**注意**：`test_v055_features.py`、`test_v055_lowfix.py` 等只驱动 refactor 的用例仍会
实例化 refactor，所以“refactor 完全不导入”只在纯差分场景范围内成立；当前结果约为
`141 passed / 8 skipped`（正式全量是 149 passed）。

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

2026-10-30 时点、v0.5.5 / `main`（含第二轮低危清理）：

* 差分模式：`149 passed, 0 failed, 0 skipped`（约 7 秒，Windows + Python 3.12）

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
├── image_testkit.py        # 插图测试共用工具（假图片/zip 篡改/迁移包重打包）
├── annotation_testkit.py  # 批注测试共用工具（配置/记录构造/种子/侧车篡改/迁移包）
├── test_load.py           # AstrBot 真实注册/模块路径/handler 元数据/实例化
├── test_storage.py        # 存储/签名/篡改/损坏隔离/迁移/偏好/备忘/素材游标
├── test_commands.py       # 全部聊天指令 + 交互式分支 + 导出/导入 + 人设口吻
├── test_web.py            # 日记本 Web API（列表/读取/导出/导入/偏好/图片/密码闸）
├── test_llm_request.py    # on_llm_request 注入、reset 代际、群聊过滤、哨兵阈值
├── test_auto_diary.py     # 每天定时自动写日记 tick、用户登记、失败通知、生命周期
├── test_bundle.py         # 迁移包校验/安装、换钥匙重签名、跨实现互导
├── test_v055_features.py  # v0.5.5 新功能：白名单/零宽字符/一键备份/token 估算与校准（只驱动 refactor）
├── test_v055_lowfix.py    # v0.5.5 低危清理：原文验签（读/迁移包/主人解析）/素材哈希清理/冷却兜底/stat 竞态/双向互斥与锁粒度/全角参数（只驱动 refactor）
├── test_annotation_storage.py    # v0.6 D7 批注存储：侧车往返/签名三态/级联删除/500 上限/迁移包（只驱动 refactor）
├── test_annotation_generation.py # v0.6 D8 批注生成：prompt/JSON 解析/额度/锚点/去重/降级（只驱动 refactor）
├── test_annotation_inject.py     # v0.6 D8 批注注入：格式/裁剪/群聊隔离/关闭态逐字节一致（只驱动 refactor）
├── test_annotation_commands.py   # v0.6 D9/D10 /查看批注、/写回批注、QQ 第五部分（只驱动 refactor）
├── test_annotation_web.py        # v0.6 D10 web_read_day 字段/鉴权/导入带批注（只驱动 refactor）
└── test_annotation_view.py       # v0.6 D10 app.js/style.css/schema/README 静态断言（只驱动 refactor）
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
  素材游标只读取新增历史、`last_extra_hash` 去重、偏好/游标落盘（第二轮起不再写 `last_material_hash`）；
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
* **v0.5.5 新功能**（`test_v055_features.py`；行为与 baseline 有意不同，故**只驱动 refactor 侧**）：
  白名单解析与空名单全放行、名单外指令被拦截（私聊一次性 UID 提示、群聊/普通闲聊静默）/
  注入与哨兵被挡/定时任务登记与触发过滤；
  零宽与方向控制字符在 `_command_arg`/`memo_cmd` 被剥离；指令 token 内文本 @ 一律沉默
  （旧配置 `bot_call_names` 残留失效）；Web `export_all` 一键备份（含全部用户、不含 `.diary_key`、
  附 `备份说明.txt`）；token 估算器纯函数（分类加权/消息与多模态开销/两入口一致）；
  EWMA 校准更新、钳制与异常跳过、`token_calibration.json` 持久化往返、
  请求→响应全链路；v2 关闭时状态行与旧格式逐字一致；v2 下 `/日记状态`、手动写日记门槛
  与哨兵复用同一个估算数；日记注入后校准样本 E 会按最终请求重算；`/日记密码` 参数零宽剥离；
  旧配置 `enable_context_watch` 键保留在 schema 中（AstrBot 不会清洗掉，迁移能命中）；
  校准 key 用 `provider_config["id"]` 拼 `provider::model`。

* **v0.5.5 低危清理**（`test_v055_lowfix.py`；行为与 baseline 有意不同，故**只驱动 refactor 侧**）：
  读路径/迁移包校验/Web 主人解析都用 zip 原文验签（带行尾空白的合法日记可读、可导出导入、
  改内容仍判篡改）；`last_material_hash` 不再写入且旧字段被清理；`_op_cooldown_remaining`
  在 aware/naive 混存时按无冷却兜底；`web_list_users` 的 stat 竞态失败只降级 size=0；
  手动写与自动写双向互斥、锁粒度是 sender_id（同群不同用户不互拒、同用户跨场景互斥）、
  占位前复查（门槛 await 期间被插队也不写盘）、哨兵/每天定时两处锁键护栏（锁被占时不调 LLM
  不写盘）；全角空格分隔的指令参数解析不变。

* **v0.6 批注**（`test_annotation_*`；行为与 baseline 有意不同，故**只驱动 refactor 侧**）：
  `annotations.json` 侧车（zip 内 + `sigs/annotations.json.sig` 整体 HMAC）往返与签名三态
  （missing 不告警 / tampered 不展示不重签不覆盖 / no_key=不可信）；`_save_diary` 逐字节保留
  批注、删日记级联删锚定该天的批注、500 条上限先丢最旧已解；迁移包 `_verify_bundle` 5 元组
  往返（旧包无批注为 `None`）、包内批注验签失败拒绝导入、`_install_bundle` 用本机钥匙重签；
  生成（开关关闭/群聊零模型调用、prompt 含人设/天数/额度/已有未解清单、疑问句软引导开关、
  对象/纯数组/```json 围栏/坏 JSON 解析、额度按字数、锚点找不到丢弃、anchor+note 去重、
  超时/异常/落盘失败静默降级、id 递增与 round/title 落盘、LLM 输出消毒）；注入（契约固定格式
  与纪律文案、只注入 open/deferred、上限按「最新疑问句优先」、按时间升序、1500 字预算保险丝、
  tampered 不注入但日记照常、群聊不生成不注入、关闭态注入文本与 baseline 逐字节一致、
  reset 新代际才重新生成、persist 模式每轮注入但只生成一次）；指令（`/查看批注` 关闭/群聊/
  空态/本轮+悬置筛选/已解摘要/验签告警；`/写回批注` 列表/选号/草稿/确认落盘/悬置/取消/
  非法输入/超时/非交互拒绝/蒸馏失败；`/查看日记` 第五部分零批注逐字节一致、有批注收在文末、
  篡改一行警告、按天取批注、帮助菜单两行）；Web（`web_read_day` 条件字段、零批注不加字段、
  tampered 暴露、日记篡改早退不泄露、密码/主人闸不回归、Web 导入与 `/导入日记` 带批注）；
  视图与配置（app.js node 语法检查、角标/卡片两区/失配页边兜底/验签警告/零批注不改 DOM/
  全文本 escape、style.css 选择器、4 个配置项类型默认值条件与顺序、README §10.13 四个 bullet；
  另在 `test_v055_features.py` 校验配置页 schema 键顺序与 README §10 的 10.12/10.13/10.16 逐项一致、
  「改进版token估算 → 自动校准」条件显示、时区与废弃项固定在最后两项）。

## 差分与 v0.5.5 的相处方式

* `tests/harness/world.py` 的 `DEFAULT_CONFIG` 固定 `token_estimation_v2=False`、
  `token_calibration_enabled=False`、`allowed_users=""`——差分套件默认走旧公式路径，
  保证与 v0.5.4 baseline 逐字节可比；新行为由 `test_v055_features.py` 单独验证。
* 有意改文案的差分豁免集中在 `World.normalise_text`：v0.5.5 注入模板新增的小标题说明句
  在两边比较前统一归一掉（见 `_INJECT_TEMPLATE_V055_SENTENCE`），语义断言不受影响。
* 第二轮低危清理的差分豁免在 `harness/compare.py::normalise_bytes`：`last_material_hash` 已废弃，
  baseline 仍写、refactor 不再写并在偏好重写时清掉；比较前统一删掉该键（只命中 JSON，
  zip 条目不受影响）。`test_storage` 的 `pref_keys` 也显式排除了这个死字段。
* v0.5.5 新增的 Web 路由 `/export_all` 与 handler 在 `test_load.py` 里对 baseline 侧补齐后再比较。

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
* 指令在群聊里按 At 组件/`group_require_at` 决定是否响应（v0.5.5 起删除 `bot_call_names`，
  指令 token 内的文本 @ 一律视为叫别人、保持沉默——该差异由 `test_v055_features.py` 单独固定）；
  群聊里查看/删除/导出/导入/密码等隐私操作一律拒绝并提示私聊。
* 无新素材时 `/日记` 默认写“她的日常随笔”，同一天只写一篇；关闭该配置后返回固定文案。
* 非交互环境（`_SESSION_WAITER_OK=False`）时：查看日记降级为直接显示最新一篇，
  删除日记明确拒绝改动，导入提示去 Web 页面——都不静默改数据。

## 发布收尾检查（改完源码后必做）

发布包 `astrbot_plugin_memory_diary_v0.5.5.zip` 是**快照**，不是源码目录的符号链接：
改了源码却不重建 zip，发布包就还是旧代码（v0.5.5 遗留问题修复报告 §0 硬约束第 5 条）。
当前发布目录里的 `..._v0.5.5.zip` 已经是**修复后构建**（sha256 `6903d823…`），不存在单独的
`..._patched.zip`；重建时直接覆盖同名 `..._v0.5.5.zip`。
每次改完源码按下面顺序收尾：

1. **跑全量测试**：`tests\run.py` 必须全绿；把最终 passed / failed / skipped 数同步到
   `CHANGELOG.md`、`RELEASE_NOTES_v0.5.5.md` 与本文档（三处数字保持一致）。
2. **重建发布 zip**：只打包 21 个运行时文件（脚本见 `v0.5.5 遗留问题修复报告_给下一个AI.md` 第 0 节）。
   zip 根目录名必须是 `astrbot_plugin_memory_diary/`；**不要从 `_extract/` 重打包**
   （那是旧一轮解包目录，带 `__pycache__`）。
3. **逐文件 SHA256 对比源码**：解压 zip，对每个文件取 SHA256，与源码目录同名文件逐一比对，
   任何一处不一致都不算通过（下面是 Windows PowerShell 写法，`<用户名>` 按实际替换）：
   ```powershell
   $root = "C:\Users\<用户名>\Desktop\记忆日记"
   $src  = "$root\记忆日记v0.5.4+版本更新工作\源码_v0.5.5"
   $zip  = "$root\记忆日记发布_v0.5.5\astrbot_plugin_memory_diary_v0.5.5.zip"
   $tmp  = Join-Path $env:TEMP "md_zip_verify"
   Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
   Expand-Archive $zip $tmp
   $pkg  = Join-Path $tmp "astrbot_plugin_memory_diary"
   $files = Get-ChildItem $pkg -Recurse -File
   "文件数：$($files.Count)（应为 21）"
   $mismatch = $files | Where-Object {
       $rel = $_.FullName.Substring($pkg.Length + 1)
       (Get-FileHash $_.FullName -Algorithm SHA256).Hash -ne
       (Get-FileHash (Join-Path $src $rel) -Algorithm SHA256).Hash
   }
   if ($mismatch) { "SHA256 不一致："; $mismatch.FullName } else { "全部文件 SHA256 一致" }
   ```
4. **包内禁入项自查**：zip 里**不得含** `.diary_key`（及任何 `*key*` 密钥文件）、
   `tests/`、`baseline/`、`__pycache__/`、`*.corrupt-<时间戳>.zip`、`diary_*.zip`、
   `*.tmp` / `*.tmp-<pid>` 临时残骸；解压后 `Get-ChildItem -Recurse` 看一遍确认。
5. **安装验收**：用发布 zip 在本机 AstrBot 重装并重载插件，确认配置页、`/日记状态`、
   日记本页面与 Web 一键备份均正常，再对外发布。

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
