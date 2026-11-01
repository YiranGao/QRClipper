"""大图分块滑窗扫描测试（本次 BugFix 的核心回归）。

背景：4K 屏（3840x2160）全屏截图上，二维码只占很小一块。整体识别失败时
需要退化为「切成有重叠的小块逐块识别」，否则用户就会看到「识别不到」。

    python tests/test_decoder_tiles.py
    python -m unittest tests.test_decoder_tiles -v
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decoder import (  # noqa: E402
    TILE_BUDGET_MS,
    TILE_MIN_SOURCE,
    _tile_spans,
    decode_image,
    decode_tiles,
    iter_tiles,
)
from PIL import Image  # noqa: E402

BIG = (3840, 2160)


def make_qr(content: str, size: int):
    """生成边长 size 像素的二维码图片（模块边缘保持锐利）。"""
    import qrcode

    qr = qrcode.QRCode(box_size=1, border=4)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return image.resize((size, size), Image.NEAREST)


def big_background(color: int = 235) -> Image.Image:
    return Image.new("RGB", BIG, (color, color, color))


class TestTileGeometry(unittest.TestCase):
    """分块切图本身：尺寸、重叠、边界覆盖。"""

    def test_tiles_cover_whole_image(self):
        image = big_background()
        tiles = list(iter_tiles(image, 0.5))
        self.assertGreater(len(tiles), 1)
        for tile in tiles:
            self.assertGreater(tile.width, 0)
            self.assertGreater(tile.height, 0)
        # 边长应约等于最短边的一半
        self.assertEqual(tiles[0].width, 1080)
        self.assertEqual(tiles[0].height, 1080)

    def test_last_tile_touches_edges(self):
        """滑窗必须从 0 起、贴到边界收尾，且相邻块有重叠（不漏边角）。"""
        spans = _tile_spans(BIG[1], 1080, 864)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], BIG[1])
        for (start_a, end_a), (start_b, _end_b) in zip(spans, spans[1:]):
            self.assertLess(start_b, end_a)  # 后一块起点在前一块内部 => 有重叠
            self.assertGreater(start_b, start_a)  # 同时不能原地踏步

    def test_large_image_needs_tiling(self):
        """大图才走分块；小图直接识别即可（阈值自身的一致性）。"""
        self.assertGreater(min(BIG), TILE_MIN_SOURCE)


class TestLargeImageDecode(unittest.TestCase):
    """核心场景：3840x2160 大图 + 角落小二维码。"""

    @classmethod
    def setUpClass(cls):
        cls.qr_cache: dict = {}

    def _qr(self, content: str, size: int) -> Image.Image:
        key = (content, size)
        if key not in self.qr_cache:
            self.qr_cache[key] = make_qr(content, size)
        return self.qr_cache[key]

    def test_small_qr_in_each_corner(self):
        """把 160px 的小码分别贴到四个角落，都必须识别出来。"""
        corners = {
            "左上": (30, 30),
            "右上": (BIG[0] - 190, 30),
            "左下": (30, BIG[1] - 190),
            "右下": (BIG[0] - 190, BIG[1] - 190),
        }
        for name, position in corners.items():
            with self.subTest(corner=name):
                content = f"CORNER-{name}"
                canvas = big_background()
                canvas.paste(self._qr(content, 160), position)
                started = time.perf_counter()
                texts = decode_image(canvas)
                elapsed_ms = (time.perf_counter() - started) * 1000
                self.assertIn(content, texts)
                # 全屏识别应保持在 1~2 秒量级（分块有预算约束）
                self.assertLess(elapsed_ms, 4000, f"{name} 耗时 {elapsed_ms:.0f} ms")

    def test_tiny_qr_near_edge(self):
        """极度边缘的小码：最容易落在分块边界上，验证重叠是否生效。"""
        canvas = big_background()
        canvas.paste(self._qr("EDGE-TINY", 120), (BIG[0] - 140, BIG[1] - 140))
        self.assertIn("EDGE-TINY", decode_image(canvas))

    def test_multiple_codes_on_4k(self):
        canvas = big_background()
        canvas.paste(self._qr("MULTI-A", 220), (60, 60))
        canvas.paste(self._qr("MULTI-B", 220), (BIG[0] - 280, BIG[1] - 280))
        texts = decode_image(canvas)
        self.assertIn("MULTI-A", texts)
        self.assertIn("MULTI-B", texts)

    def test_no_code_returns_empty_and_fast(self):
        """没有码时不应因为分块扫描把耗时拖到几秒。"""
        canvas = big_background()
        started = time.perf_counter()
        texts = decode_image(canvas)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(texts, [])
        self.assertLess(elapsed_ms, TILE_BUDGET_MS + 2500)


class TestTileScanWithFakeEngine(unittest.TestCase):
    """注入假引擎，确定性地验证「分块扫描能救回整体识别失败」。"""

    @staticmethod
    def _small_only_engine(max_side: int = 1200, tag: str = "TILE-HIT"):
        """只在小图上成功的引擎：模拟「大图识别不出来」的引擎能力上限。"""

        def engine(image: Image.Image):
            if max(image.width, image.height) <= max_side:
                return [tag]
            return []

        return ("fake-small-only", engine)

    def test_direct_decode_fails_but_tiles_find_it(self):
        canvas = big_background()
        canvas.paste(make_qr("UNUSED", 160), (40, 40))
        engines = [self._small_only_engine()]
        # 关掉分块：整图 + 0.5 缩小（1920x1080）都超过引擎能力 -> 识别不到
        self.assertEqual(decode_image(canvas, engines=engines, tile_scan=False), [])
        # 打开分块：1080x1080 的小块命中
        self.assertEqual(decode_image(canvas, engines=engines, tile_scan=True), ["TILE-HIT"])

    def test_tile_scan_respects_budget(self):
        """超慢引擎 + 极小预算：必须在预算附近收手，不能无限扫下去。"""

        def slow_engine(image: Image.Image):
            time.sleep(0.15)
            return []

        canvas = big_background()
        started = time.perf_counter()
        texts = decode_tiles(canvas, engines=[("slow", slow_engine)], budget_ms=300)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(texts, [])
        self.assertLess(elapsed_ms, 900, f"分块扫描超时：{elapsed_ms:.0f} ms")

    def test_tiles_dedupe_repeated_hits(self):
        """同一码会在多个重叠块里被重复识别，结果必须去重。"""
        canvas = big_background()
        canvas.paste(make_qr("DUP", 160), (BIG[0] // 2 - 80, BIG[1] // 2 - 80))
        engines = [self._small_only_engine()]
        texts = decode_image(canvas, engines=engines, tile_scan=True)
        self.assertEqual(texts, ["TILE-HIT"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
