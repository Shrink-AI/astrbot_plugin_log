"""媒体下载与内容寻址存储。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap as B

media = B.media
MediaStore = media.MediaStore


class MediaTestBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = B.LocalHTTP()

    @classmethod
    def tearDownClass(cls):
        cls.http.close()

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-media-")
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def store(self, **kw):
        kw.setdefault("max_bytes", 1024 * 1024)
        kw.setdefault("timeout", 5.0)
        kw.setdefault("concurrency", 4)
        return MediaStore(root=self.root, **kw)

    def files(self):
        return [p for p in (self.root / "media").rglob("*") if p.is_file()]


class FetchTests(MediaTestBase):
    async def test_png_ok(self):
        st = self.store()
        res = await st.fetch(self.http.url("/png"))
        self.assertNotIn("err", res)
        self.assertEqual(res["sha256"], hashlib.sha256(B.PNG).hexdigest())
        self.assertEqual(res["mime"], "image/png")
        self.assertEqual(res["bytes"], len(B.PNG))
        self.assertTrue(res["path"].startswith("media/"))
        self.assertTrue(res["path"].endswith(".png"))
        saved = self.root / res["path"]
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), B.PNG)

    async def test_jpeg_and_gif_ext(self):
        st = self.store()
        j = await st.fetch(self.http.url("/jpeg"))
        g = await st.fetch(self.http.url("/gif"))
        self.assertTrue(j["path"].endswith(".jpg"))
        self.assertTrue(g["path"].endswith(".gif"))

    async def test_dedupe_same_bytes(self):
        st = self.store()
        a = await st.fetch(self.http.url("/png"))
        b = await st.fetch(self.http.url("/png"))
        self.assertEqual(a["path"], b["path"])
        self.assertEqual(len(self.files()), 1)

    async def test_content_type_wins_over_wrong_header(self):
        st = self.store()
        res = await st.fetch(self.http.url("/octet"))
        self.assertEqual(res["mime"], "image/png")
        self.assertTrue(res["path"].endswith(".png"))

    async def test_text_content(self):
        st = self.store()
        res = await st.fetch(self.http.url("/text"))
        self.assertNotIn("err", res)
        self.assertEqual(res["mime"], "text/plain")
        self.assertTrue(res["path"].endswith(".bin"))

    async def test_empty_body(self):
        st = self.store()
        res = await st.fetch(self.http.url("/empty"))
        self.assertEqual(res.get("err"), "empty")

    async def test_404(self):
        st = self.store()
        res = await st.fetch(self.http.url("/nope"))
        self.assertIn("err", res)

    async def test_connection_refused(self):
        st = self.store()
        res = await st.fetch(f"http://127.0.0.1:{B.free_port()}/png")
        self.assertIn("err", res)

    async def test_too_large(self):
        st = self.store(max_bytes=1024)
        res = await st.fetch(self.http.url("/big"))
        self.assertEqual(res.get("err"), "too_large")

    async def test_timeout(self):
        st = self.store(timeout=0.3)
        res = await st.fetch(self.http.url("/slow"))
        self.assertIn("err", res)

    async def test_urllib_backend(self):
        st = self.store()
        st.backend = "urllib"
        res = await st.fetch(self.http.url("/png"))
        self.assertNotIn("err", res)
        self.assertEqual(res["sha256"], hashlib.sha256(B.PNG).hexdigest())


class StoreTests(MediaTestBase):
    async def test_store_bytes_layout(self):
        st = self.store()
        res = st.store_bytes(B.PNG, "image/png")
        self.assertEqual(len(res["sha256"]), 64)
        self.assertEqual(res["path"], f"media/{res['sha256'][:2]}/{res['sha256']}.png")

    async def test_concurrent_same_bytes(self):
        st = self.store()
        results = await asyncio.gather(
            *[asyncio.to_thread(st.store_bytes, B.PNG, "image/png") for _ in range(8)]
        )
        # 并发落盘同一个 sha 时，任何一路都不许报错（Windows 上 os.replace 会短暂占用目标）
        for r in results:
            self.assertNotIn("err", r, r)
            self.assertEqual(r["sha256"], hashlib.sha256(B.PNG).hexdigest())
        self.assertEqual(len({r["path"] for r in results}), 1)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.files()[0].read_bytes(), B.PNG)
        leftovers = [p for p in self.root.rglob("*.tmp")]
        self.assertEqual(leftovers, [])

    async def test_store_race_target_already_landed(self):
        st = self.store()
        first = st.store_bytes(B.PNG, "image/png")
        self.assertNotIn("err", first)
        target = self.root / first["path"]
        target.unlink()  # 让第二路真的走进落盘流程

        def fake_replace(src, dst):
            Path(dst).write_bytes(B.PNG)  # 模拟另一路刚好抢先写完
            raise PermissionError("locked")

        # 改名被拒，但目标文件已经在位且内容一致 -> 视为成功
        with mock.patch("os.replace", side_effect=fake_replace):
            res = st.store_bytes(B.PNG, "image/png")
        self.assertNotIn("err", res)
        self.assertEqual(res["path"], first["path"])
        self.assertEqual([p.name for p in self.root.rglob("*.tmp")], [])

    async def test_store_failure_returns_err_dict(self):
        st = self.store()
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            res = st.store_bytes(B.PNG, "image/png")
        self.assertIn("err", res)
        self.assertEqual(res["sha256"], hashlib.sha256(B.PNG).hexdigest())

    async def test_fetch_never_raises_when_store_fails(self):
        st = self.store()
        st.store_bytes = mock.Mock(side_effect=OSError("disk full"))
        res = await st.fetch(self.http.url("/png"))
        self.assertIn("err", res)

    async def test_store_bytes_unknown_type(self):
        st = self.store()
        res = st.store_bytes(b"\x00\x01\x02\x03", "")
        self.assertTrue(res["path"].endswith(".bin"))

    async def test_existing_file_not_rewritten(self):
        st = self.store()
        first = st.store_bytes(B.PNG, "image/png")
        target = self.root / first["path"]
        before = target.stat().st_mtime_ns
        st.store_bytes(B.PNG, "image/png")
        self.assertEqual(target.stat().st_mtime_ns, before)


class SniffTests(unittest.TestCase):
    def test_magic(self):
        self.assertEqual(media._sniff(B.PNG), ("image/png", ".png"))
        self.assertEqual(media._sniff(B.JPEG), ("image/jpeg", ".jpg"))
        self.assertEqual(media._sniff(B.GIF), ("image/gif", ".gif"))
        self.assertEqual(media._sniff(b"\x00\x01"), ("application/octet-stream", ".bin"))


if __name__ == "__main__":
    unittest.main()