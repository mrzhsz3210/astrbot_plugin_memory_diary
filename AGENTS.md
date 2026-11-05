# AGENTS.md — 给在这个仓库干活的 AI（和人）

## 项目一句话

记忆日记（astrbot_plugin_memory_diary）是一个 AstrBot 插件：让 AI 给"下一个失去记忆的自己"写日记，并在新上下文开启时自动注入最近日记。

- 语言：纯 Python（`main.py` + `adapter/` + `memory_diary/` + `views/diary-book/` 前端三件套）；无构建步骤；依赖见 `requirements.txt`。

## 测试（每个任务都要遵守，无一例外）

```bash
<带 AstrBot 依赖的 python> tests/run.py          # 全量
<带 AstrBot 依赖的 python> tests/run.py -k 关键字  # 过滤
<带 AstrBot 依赖的 python> tests/run.py -v / --list
```

- **必须用装有 AstrBot 依赖的 Python 环境跑**（测试要 import astrbot，缺 sqlalchemy 等依赖会直接挂）；
  裸的托管/系统 python 不行。仓库 CI 的等价做法是克隆 AstrBot core v4.29.3 装依赖后跑同一入口。
- 不依赖 pytest——`tests/run.py` 是自带的独立 runner（`conftest.py`/`pytest.ini` 仅为兼容存在）。
- 仓库 CI（`.github/workflows/ci.yml`）= `compileall` 三版本语法检查 + `_conf_schema.json` JSON 校验 +
  上述回归；触发范围仅限 `.py` / `_conf_schema.json` / CI 文件改动，**纯文档提交 CI 不拦**。

## 解耦纪律（改代码前必读 `ARCHITECTURE.md`）

本插件经过两轮解耦：`main.py` 只是注册 + 组装的入口薄壳（约 200 行），不许再变胖。三条铁律：

1. **`memory_diary/` 领域核心绝不 import astrbot**——只依赖 AstrBot 的东西全部放 `adapter/`；
2. 新指令/新事件钩子：实现写进 `adapter/` 对应模块的 `_handle_<入口名>`，`main.py` 只加装饰器 +
   同签名薄壳（模板见 ARCHITECTURE.md）；
3. 新 Web 接口：写进 `adapter/web_*.py`，并在 `web_diary.py` 的 `_register_web_apis()` 里登记
   （Web 路由不靠装饰器，整个不进 main.py）。
4. 自检：`main.py` 除 `__init__` 外不应出现 `if`/`try` 业务分支；超过约 250 行说明实现漏进了薄壳。

"新增功能时代码放哪"的完整对照表在 `ARCHITECTURE.md` 第 54 行起——**拿不准放哪时去读它，不要凭感觉**。

## 文档与发布纪律

- `docs/README.md` 是完整版文档的**唯一改动源**；根目录 `README.md` 是展示短版，**不要直接改**。
  发布前把「版本引用块 / 共建者招募 / 更新说明」三节从完整版同步到短版，并核对官网链接
  （astrbotmemorydiary.ok.kimi.link）与问卷链接是否变化。
- `main.py` 的 `@register(...)` 版本号、`metadata.yaml` 的 `version:`、短版 README 版本引用块
  **三处必须同步**，否则 `test_metadata_yaml_matches_registration` 会红。
- 发布 zip 只含运行时文件（对照上一版包清单）；**绝不打包** `tests/`、`baseline/`、`docs/`、
  `.diary_key`、`__pycache__/`。打包脚本（如 `_pack_v*.py`）自带禁入项自检，用它，不要手搓 zip。
- `CHANGELOG.md` 每个版本补一节；`RELEASE_NOTES_v<版本>.md` 随发布目录走（含致谢与回退说明）。
- 改 `_conf_schema.json` 时跑一遍 `python -m json.tool`（CI 也会查）。

## 数据与安全红线

- `.diary_key`（HMAC 密钥）是机密：不进任何包/仓库/备份的公开副本，但用户自己备份迁移时必须带上它；
- 验签依赖它——任何"让签名失效"的改动都必须视为数据破坏级事故，先跟小杨确认。

## 已知空白（待维护者拍板，AI 不要自行发明约定）

- 无 lint / 格式化 / 类型检查配置——语法编译检查就是全部静态保障了；
- CI 把 AstrBot 钉在 v4.29.3，上游新版本兼容性无人值守；
- 无打包/发布 CI 作业，发布包正确性靠打包脚本自检 + 人工 diff 上一版包。
