"""生成一份小样本并打印落盘结果，用来肉眼确认 schema。

用法: python tests/sample_dump.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import smoke_test as S  # noqa: E402  (复用桩环境与事件构造)

OUT = HERE / "_sample_out"


async def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    cfg = {
        "data_dir": str(OUT),
        "max_file_mb": 128,
        "timezone": "Asia/Shanghai",
        "download_media": True,
        "log_stats_interval_s": 0,
    }
    plugin = S.plugin_mod.TrainLogPlugin(context=None, config=cfg)
    ing = await plugin._get_ingestor()

    srv, port = S.start_server()
    img = f"http://127.0.0.1:{port}/img.png"
    base = 1756700000000  # 2025-09-01 12:13:20 +08:00

    # 群聊：A 提议，B 应答，A 回复引用
    ing.offer(
        S.make_event(
            message_type="group", group_id="123456789", user_id="111",
            text="今晚吃什么", msg_id="m1", ts=base,
        )
    )
    ing.offer(
        S.make_event(
            message_type="group", group_id="123456789", user_id="222",
            text="火锅吧 回复", msg_id="m2", ts=base + 4000,
        )
    )
    ing.offer(
        S.make_event(
            message_type="group", group_id="123456789", user_id="111",
            text="行，那家新开的", msg_id="m3", ts=base + 8000,
            images=(img,),
        )
    )
    # 私聊
    ing.offer(
        S.make_event(
            message_type="private", user_id="222",
            text="在吗", msg_id="m4", ts=base + 12000,
        )
    )
    await ing.aclose()
    srv.shutdown()

    print(f"数据目录: {OUT}\n")
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(OUT).as_posix()}")
    print()
    for p in sorted(OUT.rglob("*.jsonl")):
        if p.name == "_manifest.jsonl":
            continue
        print(f"--- {p.relative_to(OUT).as_posix()}")
        for line in p.read_text(encoding="utf-8").splitlines():
            print(json.dumps(json.loads(line), ensure_ascii=False, indent=None)[:1200])
        print()


if __name__ == "__main__":
    asyncio.run(main())