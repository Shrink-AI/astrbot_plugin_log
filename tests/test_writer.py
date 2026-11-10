"""分片写入器：滚动、续写、修复、并发、异常路径。"""

from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path

import _bootstrap as B

writer = B.writer
ShardedWriter = writer.ShardedWriter

KB = 1024


def make(root, **kw):
    kw.setdefault("flush_interval", 0.02)
    kw.setdefault("fsync_interval", 0)
    kw.setdefault("logger", None)
    return ShardedWriter(root=Path(root), **kw)


class BasicTests(unittest.TestCase):
    def test_writes_one_line(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            self.assertTrue(w.submit("g/1", "2025-01-01", json.dumps({"a": 1})))
            w.close()
            f = Path(td) / "g" / "1" / "2025-01-01.000.jsonl"
            self.assertTrue(f.exists())
            rows = B.read_jsonl(f)
            self.assertEqual(rows, [{"a": 1}])

    def test_creates_directories(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            w.submit("g/999", "2025-01-01", "{}")
            w.close()
            self.assertTrue((Path(td) / "g" / "999").is_dir())

    def test_appends_newline_and_strips_extra(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            w.submit("g/1", "2025-01-01", '{"a":1}\n\n')
            w.close()
            raw = (Path(td) / "g" / "1" / "2025-01-01.000.jsonl").read_text(encoding="utf-8")
            self.assertEqual(raw.count("\n"), 1)
            self.assertTrue(raw.endswith("\n"))

    def test_unicode_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            payload = {"text": "中文😀é\u200b", "emoji": "🎉"}
            w.submit("g/1", "2025-01-01", json.dumps(payload, ensure_ascii=False))
            w.close()
            rows = B.read_jsonl(Path(td) / "g" / "1" / "2025-01-01.000.jsonl")
            self.assertEqual(rows[0], payload)

    def test_many_conversations(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB, max_open_files=8)
            for i in range(200):
                w.submit(f"g/{i}", "2025-01-01", json.dumps({"i": i}))
            w.close()
            for i in range(200):
                rows = B.read_jsonl(Path(td) / "g" / str(i) / "2025-01-01.000.jsonl")
                self.assertEqual(rows, [{"i": i}])


class RollingTests(unittest.TestCase):
    def test_rolls_by_size(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            line = json.dumps({"pad": "x" * 200})
            n = 2000
            for _ in range(n):
                w.submit("g/1", "2025-01-01", line)
            w.close()
            parts = sorted((Path(td) / "g" / "1").glob("2025-01-01.*.jsonl"))
            self.assertGreater(len(parts), 1)
            total = sum(B.lines_of(p) for p in parts)
            self.assertEqual(total, n)
            for p in parts:
                self.assertLessEqual(p.stat().st_size, 64 * KB)

    def test_parts_are_ordered(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            line = json.dumps({"pad": "y" * 500})
            for _ in range(1000):
                w.submit("g/1", "2025-01-01", line)
            w.close()
            parts = sorted((Path(td) / "g" / "1").glob("2025-01-01.*.jsonl"))
            names = [p.name for p in parts]
            self.assertEqual(names, sorted(names))
            self.assertEqual(names[0], "2025-01-01.000.jsonl")

    def test_day_switch_creates_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=1024 * KB)
            w.submit("g/1", "2025-01-01", "{}")
            w.submit("g/1", "2025-01-02", "{}")
            w.submit("g/1", "2025-01-01", "{}")  # 迟到的前一天消息
            w.close()
            d = Path(td) / "g" / "1"
            self.assertEqual(B.lines_of(d / "2025-01-01.000.jsonl"), 2)
            self.assertEqual(B.lines_of(d / "2025-01-02.000.jsonl"), 1)

    def test_single_line_larger_than_limit(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            huge = json.dumps({"pad": "z" * (100 * KB)})
            self.assertTrue(w.submit("g/1", "2025-01-01", huge))
            w.submit("g/1", "2025-01-01", "{}")
            w.close()
            parts = sorted((Path(td) / "g" / "1").glob("2025-01-01.*.jsonl"))
            self.assertGreaterEqual(len(parts), 1)
            total = sum(B.lines_of(p) for p in parts)
            self.assertEqual(total, 2)
            # 超长行自己一个文件，不把后续记录挤丢
            self.assertEqual(B.lines_of(parts[0]), 1)


class ResumeTests(unittest.TestCase):
    def test_appends_to_existing_part(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            f = d / "2025-01-01.000.jsonl"
            f.write_text('{"old":1}\n', encoding="utf-8")
            w = make(td, max_file_bytes=1024 * KB)
            w.submit("g/1", "2025-01-01", '{"new":2}')
            w.close()
            self.assertEqual(B.lines_of(f), 2)
            self.assertEqual(len(list(d.glob("*.jsonl"))), 1)

    def test_rolls_when_existing_part_full(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            f = d / "2025-01-01.000.jsonl"
            # 老文件已经贴近上限（一条 63KB 的记录）
            f.write_text(json.dumps({"pad": "x" * (63 * KB)}) + "\n", encoding="utf-8")
            before = f.stat().st_size
            w = make(td, max_file_bytes=64 * KB)
            for _ in range(5):
                w.submit("g/1", "2025-01-01", json.dumps({"pad": "y" * (20 * KB)}))
            w.close()
            parts = sorted(d.glob("2025-01-01.*.jsonl"))
            self.assertGreater(len(parts), 1)
            self.assertEqual(len(B.read_jsonl(f)), 1)  # 老文件没被动
            self.assertEqual(f.stat().st_size, before)

    def test_junk_filenames_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            (d / "2025-01-01.abc.jsonl").write_text("junk\n", encoding="utf-8")
            (d / "2025-01-01.000.jsonl.bak").write_text("junk\n", encoding="utf-8")
            w = make(td, max_file_bytes=1024 * KB)
            w.submit("g/1", "2025-01-01", '{"a":1}')
            w.close()
            self.assertEqual(B.lines_of(d / "2025-01-01.000.jsonl"), 1)


class RepairTests(unittest.TestCase):
    def test_truncates_half_line(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            f = d / "2025-01-01.000.jsonl"
            f.write_bytes(b'{"good":1}\n{"half":tr')
            w = make(td, max_file_bytes=1024 * KB)
            w.submit("g/1", "2025-01-01", '{"new":1}')
            w.close()
            rows = B.read_jsonl(f)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0], {"good": 1})
            self.assertEqual(rows[1], {"new": 1})

    def test_no_trailing_newline_at_all(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            f = d / "2025-01-01.000.jsonl"
            f.write_bytes(b'{"only":"partial')
            w = make(td, max_file_bytes=1024 * KB)
            w.submit("g/1", "2025-01-01", '{"ok":1}')
            w.close()
            rows = B.read_jsonl(f)
            self.assertEqual(rows, [{"ok": 1}])

    def test_complete_file_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            f = d / "2025-01-01.000.jsonl"
            f.write_bytes(b'{"a":1}\n{"b":2}\n')
            w = make(td, max_file_bytes=1024 * KB)
            w.submit("g/1", "2025-01-01", '{"c":3}')
            w.close()
            self.assertEqual(B.lines_of(f), 3)


class LifecycleTests(unittest.TestCase):
    def test_submit_after_close_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            w.submit("g/1", "2025-01-01", "{}")
            w.close()
            self.assertFalse(w.submit("g/1", "2025-01-01", "{}"))

    def test_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            w.submit("g/1", "2025-01-01", "{}")
            w.close()
            w.close()
            self.assertEqual(B.lines_of(Path(td) / "g" / "1" / "2025-01-01.000.jsonl"), 1)

    def test_queue_full_does_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)

            class FullQueue:
                def put_nowait(self, item):
                    raise queue.Full

            w._q = FullQueue()
            self.assertFalse(w.submit("g/1", "2025-01-01", "{}"))
            self.assertEqual(w.dropped, 1)

            w._q = queue.Queue(maxsize=100)
            w.close()

    def test_manifest_written(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            w.submit("g/1", "2025-01-01", "{}")
            w.close()
            rows = B.read_jsonl(Path(td) / "_manifest.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["conv"], "g/1")
            self.assertEqual(rows[0]["day"], "2025-01-01")
            self.assertEqual(rows[0]["path"], "g/1/2025-01-01.000.jsonl")

    def test_manifest_records_each_part(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=64 * KB)
            line = json.dumps({"pad": "x" * 200})
            for _ in range(2000):
                w.submit("g/1", "2025-01-01", line)
            w.close()
            rows = B.read_jsonl(Path(td) / "_manifest.jsonl")
            parts = sorted((Path(td) / "g" / "1").glob("2025-01-01.*.jsonl"))
            self.assertEqual(len(rows), len(parts))


class ConcurrencyTests(unittest.TestCase):
    def test_multi_thread_submit_no_loss(self):
        with tempfile.TemporaryDirectory() as td:
            w = make(td, max_file_bytes=4096 * KB)
            threads = []
            per = 300

            def worker(tid):
                for i in range(per):
                    w.submit("g/1", "2025-01-01", json.dumps({"tid": tid, "i": i}))

            for tid in range(8):
                t = threading.Thread(target=worker, args=(tid,))
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
            w.close()

            rows = B.read_jsonl(Path(td) / "g" / "1" / "2025-01-01.000.jsonl")
            self.assertEqual(len(rows), 8 * per)
            seen = {(r["tid"], r["i"]) for r in rows}
            self.assertEqual(len(seen), 8 * per)
            for tid in range(8):
                self.assertEqual(sum(1 for r in rows if r["tid"] == tid), per)


if __name__ == "__main__":
    unittest.main()