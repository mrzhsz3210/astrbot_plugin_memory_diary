# -*- coding: utf-8 -*-
"""记忆日记修复工具（离线，命令行）。

用途：修复数据目录里的异常状态——假日记包（0 天/无主人/全量验签失败）、
登记册残留告警（文件被手动移走）、trash/ 里被误放的日记格式 zip，
以及把一本日记合并进另一本（跨人格/跨账户搬家）。

直接复用插件源码做全部验签（HMAC 口径与运行中的插件逐字节一致，
不重新实现签名逻辑）。默认**只体检、不改任何东西**；修复动作需显式
--fix（逐个确认）或 --yes（全部确认）。

用法（用 AstrBot 实例的 Python 跑）：

    python memory_diary_repair.py --plugin-dir <插件目录> --data-dir <数据目录>
    python memory_diary_repair.py ... --fix
    python memory_diary_repair.py ... --fix --yes
    python memory_diary_repair.py ... --merge <源zip> <目标身份>
    python memory_diary_repair.py ... --restore-trash <回收站条目id> <身份>

例（AstrBot Launcher 实例）：

    <实例>\\venv\\Scripts\\python.exe memory_diary_repair.py ^
        --plugin-dir "<实例>\\core\\data\\plugins\\astrbot_plugin_memory_diary" ^
        --data-dir  "<实例>\\core\\data\\plugin_data\\astrbot_plugin_memory_diary"

目标身份格式：原始 QQ（默认人格，如 2080210976）或 QQ::p::人格id
（如 2080210976::p::小A，与日记 zip 内 说明.txt 的「用户ID」行一致）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _load_core(plugin_dir: Path, data_dir: Path, config: dict):
    sys.path.insert(0, str(plugin_dir))
    for mod in [k for k in list(sys.modules) if k.startswith("memory_diary")]:
        del sys.modules[mod]
    from memory_diary.core import MemoryDiaryCore

    class _Stub:
        def __init__(self, *a, **k):
            pass

    class _Core(MemoryDiaryCore, _Stub):
        pass

    return _Core(None, config or {}, data_dir=data_dir)


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------


def _book_health(core, path: Path) -> dict:
    """单个 diary zip 的健康画像。"""
    info = {
        "file": path.name,
        "owner": "",
        "nick": "",
        "days": [],
        "status": "ok",
        "detail": "",
        "in_registry": path.name in core._load_registry(),
    }
    try:
        with core._diary_zip(path) as z:
            names = set(z.namelist())
            days = sorted(n for n in names if core.ENTRY_NAME_RE.match(n))
    except Exception as e:
        info["status"] = "unreadable"
        info["detail"] = str(e)
        return info
    info["days"] = [core._entry_day(n) for n in days]
    owner, nick = core._zip_owner_info(path)
    info["owner"], info["nick"] = owner, nick
    if not days:
        info["status"] = "empty_fake"
        info["detail"] = "没有日记条目（疑似被外部捏造的假包/放错的回收站包）"
        return info
    if not owner:
        info["status"] = "owner_unknown"
        info["detail"] = "说明.txt 缺失或没有用户ID行，无法确认主人"
        return info
    if core._all_entries_tampered(owner):
        info["status"] = "all_tampered"
        info["detail"] = "全部条目验签失败（被外部改写或放错的包）"
    return info


def scan(core, data_dir: Path) -> dict:
    import re as _re

    books = [
        _book_health(core, p)
        for p in sorted(data_dir.glob("diary_*.zip"))
        if _re.fullmatch(r"[0-9a-f]{16}", p.stem[len("diary_"):])
    ]
    registry = core._load_registry()
    vanished = [v["file"] for v in core._vanished_diary_alerts()]
    trash_bundles, trash_strays = [], []
    trash_dir = data_dir / "trash"
    if trash_dir.is_dir():
        for p in sorted(trash_dir.iterdir()):
            if not p.is_file():
                continue
            if p.name.startswith("trash_") and p.suffix == ".zip":
                meta, _payload = core._read_trash_item(p)
                trash_bundles.append(
                    {
                        "id": p.stem[len("trash_"):],
                        "owner": str((meta or {}).get("owner") or "?"),
                        "days": list((meta or {}).get("days") or []),
                        "valid": bool(meta),
                    }
                )
            else:
                trash_strays.append(p.name)
    backup_dir = data_dir / "backups"
    backups = (
        sorted(p.name for p in backup_dir.glob("*.zip"))
        if backup_dir.is_dir()
        else []
    )
    return {
        "books": books,
        "registry_vanished": vanished,
        "registry_total": len(registry),
        "trash_bundles": trash_bundles,
        "trash_strays": trash_strays,
        "backups": backups,
    }


def print_report(rep: dict) -> None:
    print("=" * 64)
    print("日记本（diary_*.zip）：")
    if not rep["books"]:
        print("  （没有）")
    for b in rep["books"]:
        owner = b["owner"] or "（主人未知）"
        days = f"{len(b['days'])} 天" if b["days"] else "0 天"
        flag = "" if b["status"] == "ok" else f"  ⚠️ {b['status']}: {b['detail']}"
        reg = "在册" if b["in_registry"] else "未登记"
        print(f"  · {b['file']}  主人={owner}  {days}  [{reg}]{flag}")
    print(f"\n登记册：{rep['registry_total']} 条；凭空消失（告警中）：{rep['registry_vanished'] or '无'}")
    print("\n回收站（trash/）：")
    if not rep["trash_bundles"] and not rep["trash_strays"]:
        print("  （空）")
    for t in rep["trash_bundles"]:
        mark = "有效" if t["valid"] else "⚠️ 验签失败"
        print(f"  · 条目 {t['id']}  主人={t['owner']}  天={t['days']}  [{mark}]")
    for s in rep["trash_strays"]:
        print(f"  · ⚠️ 杂散文件 {s}（不是回收站条目，可能被人手动移进来）")
    print(f"\n自动备份（backups/）：{len(rep['backups'])} 份")
    for b in rep["backups"]:
        print(f"  · {b}")


# ---------------------------------------------------------------------------
# 修复动作
# ---------------------------------------------------------------------------


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{prompt} → 是（--yes）")
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes", "是")


def fix(core, data_dir: Path, rep: dict, assume_yes: bool) -> None:
    ts = time.strftime("%Y%m%d_%H%M%S")
    # 1) 假包隔离（0 天 / 无主人 / 全量验签失败 / 读不出）
    for b in rep["books"]:
        if b["status"] == "ok":
            continue
        src = data_dir / b["file"]
        dest = src.with_name(f"{src.stem}.corrupt-{ts}{src.suffix}")
        if _confirm(f"隔离假包 {b['file']}（{b['status']}）→ {dest.name}", assume_yes):
            src.rename(dest)
            core._registry_unmark(b["owner"] or b["file"])
            print(f"  已隔离：{dest.name}")
    # 2) 登记册销警：trash/ 里有同名文件则先提议移回，否则销记
    for fname in list(rep["registry_vanished"]):
        stray = data_dir / "trash" / fname
        if stray.exists():
            if _confirm(f"{fname} 在 trash/ 里找到同名文件，移回数据目录？", assume_yes):
                stray.replace(data_dir / fname)
                hb = _book_health(core, data_dir / fname)
                if hb["status"] == "ok":
                    core._registry_mark(hb["owner"])
                    print(f"  已移回并重新登记：{fname}")
                else:
                    print(f"  ⚠️ 移回的文件不健康（{hb['status']}），请重新体检处理")
        else:
            if _confirm(f"登记册条目 {fname} 的文件已不存在，销记（放弃）？", assume_yes):
                reg = core._load_registry()
                reg.pop(fname, None)
                core._save_registry(reg)
                print(f"  已销记：{fname}")
    # 3) trash/ 杂散文件
    for name in rep["trash_strays"]:
        src = data_dir / "trash" / name
        if not src.exists():
            continue  # 可能在第 2 步（登记册销警）里已被移回数据目录
        if name.startswith("diary_") and name.endswith(".zip"):
            hb = _book_health(core, src)
            if hb["status"] == "ok" and _confirm(
                f"trash/ 里的 {name} 是健康日记包（{len(hb['days'])} 天），移回数据目录？",
                assume_yes,
            ):
                src.replace(data_dir / name)
                core._registry_mark(hb["owner"])
                print(f"  已移回并登记：{name}")
                continue
        if _confirm(f"删除 trash/ 杂散文件 {name}？", assume_yes):
            src.unlink()
            print(f"  已删除：{name}")


# ---------------------------------------------------------------------------
# 合并日记本
# ---------------------------------------------------------------------------


def merge(core, data_dir: Path, source: Path, target_id: str) -> bool:
    """把 source zip 的日记天合并进 target_id 的本子；已有的天不覆盖。"""
    owner, _nick = core._zip_owner_info(source)
    if not owner:
        print(f"失败：{source.name} 解析不出主人（说明.txt 缺失），拒绝合并")
        return False
    try:
        with core._diary_zip(source) as z:
            data = {n: z.read(n) for n in z.namelist()}
    except Exception as e:
        print(f"失败：读取 {source.name} 失败：{e}")
        return False
    names = set(data)
    entries = sorted(n for n in names if core.ENTRY_NAME_RE.match(n))
    if not entries:
        print("失败：源包里没有日记条目")
        return False
    # 全量验签（按源包主人身份），不过拒
    for n in entries:
        sig = data.get(core._sig_entry(n), b"").decode("utf-8", "ignore").strip()
        if not sig or not core._entry_sig_matches(
            owner, n, data[n].decode("utf-8"), sig
        ):
            print(f"失败：源包 {core._entry_label(n)} 验签失败，拒绝合并")
            return False
    images = {}
    for n in sorted(names):
        if core.IMAGE_NAME_RE.match(n):
            sig = data.get(core._image_sig_entry(n), b"").decode(
                "utf-8", "ignore"
            ).strip()
            if not sig or not core._image_sig_matches(owner, n, data[n], sig):
                print(f"失败：源包插图 {n} 验签失败，拒绝合并")
                return False
            images[n] = data[n]

    target_path = core._diary_path(target_id)
    live = {}
    if target_path.exists():
        with core._diary_zip(target_path) as z:
            live = {n: z.read(n) for n in z.namelist()}
    out = dict(live)
    merged, skipped = [], []
    for n in entries:
        if n in live:
            skipped.append(core._entry_day(n))
            continue
        text = data[n].decode("utf-8")
        out[n] = text.encode("utf-8")
        # 全部按目标身份重签（源签名域与目标不同）
        out[core._sig_entry(n)] = core._entry_sig(target_id, n, text).encode("utf-8")
        merged.append(core._entry_day(n))
    for n, blob in images.items():
        day = n.split("/")[-1][:8]
        if day not in merged or n in live:
            continue
        out[n] = blob
        out[core._image_sig_entry(n)] = core._image_sig(
            target_id, n, blob
        ).encode("utf-8")
    if not merged:
        print(f"没有可合并的天（{len(skipped)} 天在目标里已存在）：{skipped}")
        return False

    # 批注：合并锚定到被并入天的记录（按目标身份重签）
    if core.ANNOTATIONS_ENTRY in names:
        raw = data[core.ANNOTATIONS_ENTRY]
        actual = data.get(core.ANNOTATIONS_SIG_ENTRY, b"").decode(
            "utf-8", "ignore"
        ).strip()
        if core._annotations_sig_matches(owner, raw, actual):
            try:
                src_ann = json.loads(raw.decode("utf-8")).get("annotations") or []
            except Exception:
                src_ann = []
            src_ann = [
                x
                for x in src_ann
                if isinstance(x, dict) and str(x.get("entry") or "")[:8] in set(merged)
            ]
            if src_ann:
                core._merge_annotations_slice(target_id, out, src_ann)
                print(f"  批注合并：{len(src_ann)} 条")
        else:
            print("  ⚠️ 源包批注验签失败，跳过批注合并")
    if core.README_ZIP_ENTRY not in out:
        out[core.README_ZIP_ENTRY] = core._readme_text(target_id, "").encode("utf-8")
    core._write_zip_atomic(target_path, out)
    core._invalidate_search_index(target_id)
    core._registry_mark(target_id)
    print(
        f"合并完成：{len(merged)} 天并入 {target_id}"
        + (f"（{len(skipped)} 天已存在未覆盖：{skipped}）" if skipped else "")
    )
    return True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆日记修复工具（默认只体检）")
    ap.add_argument("--plugin-dir", required=True, help="插件目录（含 memory_diary/ 的目录）")
    ap.add_argument("--data-dir", required=True, help="插件数据目录 plugin_data/astrbot_plugin_memory_diary")
    ap.add_argument("--fix", action="store_true", help="执行修复（隔离假包、登记册销警、清理 trash 杂散）")
    ap.add_argument("--yes", action="store_true", help="修复动作全部确认（非交互）")
    ap.add_argument("--merge", nargs=2, metavar=("源zip", "目标身份"),
                    help="把源 zip 的日记合并进目标身份的本子")
    ap.add_argument("--restore-trash", nargs=2, metavar=("回收站条目id", "身份"),
                    help="恢复某条回收站条目")
    args = ap.parse_args()

    plugin_dir = Path(args.plugin_dir)
    data_dir = Path(args.data_dir)
    if not (plugin_dir / "memory_diary").is_dir():
        print(f"插件目录不对（没有找到 memory_diary/）：{plugin_dir}")
        return 2
    if not data_dir.is_dir():
        print(f"数据目录不存在：{data_dir}")
        return 2

    core = _load_core(plugin_dir, data_dir, {})
    if core._secret_key() is None:
        print("签名密钥（.diary_key）不可读，验签做不了，修复中止。")
        return 2

    if args.merge:
        source = Path(args.merge[0])
        if not source.is_absolute():
            source = data_dir / source.name
        return 0 if merge(core, data_dir, source, args.merge[1]) else 1

    if args.restore_trash:
        restored, skipped, err = core.restore_trash(
            args.restore_trash[1], args.restore_trash[0]
        )
        if err:
            print(f"恢复失败：{err}")
            return 1
        print(f"已恢复 {len(restored)} 天：{restored}"
              + (f"；{len(skipped)} 天已存在未覆盖：{skipped}" if skipped else ""))
        return 0

    rep = scan(core, data_dir)
    print_report(rep)
    if args.fix:
        print("\n" + "=" * 64)
        print("开始修复：")
        fix(core, data_dir, rep, args.yes)
        print("\n修复后复查：")
        print_report(scan(core, data_dir))
    else:
        print("\n（以上为只读体检；加 --fix 执行修复，加 --yes 跳过逐项确认）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
