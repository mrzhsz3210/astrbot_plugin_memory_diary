# -*- coding: utf-8 -*-
"""把成对回答随机标成展示 A/B；映射写到单独文件。仅本地离线使用。"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pairs", type=Path, help="[{case_id, baseline, memory_diary}, ...]")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    args = parser.parse_args()
    pairs = json.loads(args.pairs.read_text(encoding="utf-8"))
    blind, mapping = [], {}
    for pair in pairs:
        swap = bool(secrets.randbits(1))
        shown = {
            "case_id": pair["case_id"],
            "A": pair["memory_diary"] if swap else pair["baseline"],
            "B": pair["baseline"] if swap else pair["memory_diary"],
        }
        blind.append(shown)
        mapping[str(pair["case_id"])] = {"A": "memory_diary" if swap else "baseline", "B": "baseline" if swap else "memory_diary"}
    args.out.write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
    args.mapping.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
