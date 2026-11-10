"""数据体检：按会话列出文件、条数、时间范围、媒体数量。

用法: python tools/inspect.py <数据根目录> [--no-time]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="数据根目录（含 g/ p/ media/）")
    ap.add_argument("--no-time", action="store_true", help="不解析时间范围（更快）")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"目录不存在: {root}")
        return 1

    convs: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(root.rglob("*.jsonl")):
        if p.name == "_manifest.jsonl":
            continue
        convs[p.parent.relative_to(root).as_posix()].append(p)

    if not convs:
        print("没有找到任何 .jsonl 数据文件")
        return 0

    print(f"数据根目录: {root}\n")
    print(f"{'会话':<32}{'文件':>6}{'记录':>10}{'大小':>10}  时间范围")
    print("-" * 96)

    total_records = 0
    total_bytes = 0
    for conv in sorted(convs):
        files = convs[conv]
        lines = 0
        size = 0
        lo = None
        hi = None
        for f in files:
            size += f.stat().st_size
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    lines += 1
                    if args.no_time:
                        continue
                    try:
                        ts = json.loads(raw).get("ts")
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ts, int):
                        lo = ts if lo is None else min(lo, ts)
                        hi = ts if hi is None else max(hi, ts)
        total_records += lines
        total_bytes += size
        span = ""
        if lo is not None and hi is not None:
            from datetime import datetime

            span = (
                f"{datetime.fromtimestamp(lo / 1000):%Y-%m-%d %H:%M} ~ "
                f"{datetime.fromtimestamp(hi / 1000):%Y-%m-%d %H:%M}"
            )
        print(f"{conv:<32}{len(files):>6}{lines:>10}{human(size):>10}  {span}")

    print("-" * 96)
    print(f"{'合计':<32}{'':>6}{total_records:>10}{human(total_bytes):>10}")

    media_dir = root / "media"
    if media_dir.exists():
        files = [p for p in media_dir.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        print(f"\n媒体: {len(files)} 个文件, {human(size)}")

    manifest = root / "_manifest.jsonl"
    if manifest.exists():
        try:
            n = sum(1 for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip())
            print(f"manifest: {n} 条文件创建记录")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())