"""二维码识别：zxing-cpp 与 zbar 双引擎 + 多尺度重试 + 大图分块滑窗。

识别流程：
1. 先用 zxing-cpp 在原图上识别（对大图小码的鲁棒性最好）；
2. 失败则换 zbar 再试；
3. 仍失败，则对图像做缩放（放大便于识别小码、缩小便于识别超大码）后重复上述过程；
4. 多尺度都失败且图像很大（4K 全屏）时，再做一次**有重叠的分块滑窗扫描**：
   把大图切成若干小块逐块识别，小码在块内占比更高，命中率明显提升。

解码得到的字节按 utf-8 → gb18030 → latin-1 顺序尝试，保证中文二维码不乱码。
"""

from __future__ import annotations

import logging
import time
from typing import Iterator, List, Optional, Sequence, Tuple

from PIL import Image

logger = logging.getLogger("QRClipper")

# 缩放策略：先原图，再放大（抓小码），最后缩小（超大二维码）
SCALES = (1.0, 2.0, 0.5)

# 超过该像素数就不再做放大：4K 图放大 2x 是 3300 万像素，
# 光 resize + 识别就要 1 秒以上且吃 100MB+ 内存，实测对命中率没有帮助。
UPSCALE_MAX_PIXELS = 8_000_000

# 最短边超过该值才启用分块扫描
TILE_MIN_SOURCE = 2_000
# 分块扫描总时间预算（毫秒）：4K 全图识别失败时，加上前面的多尺度重试，
# 总耗时控制在 1~2 秒内（实测 4K 桌面：多尺度 ~0.4s + 分块 ~0.8s）。
TILE_BUDGET_MS = 800
# 相邻分块的重叠比例，避免码正好被切在边界上
TILE_OVERLAP = 0.2
# 分块边长 = 最短边 * 该比例，依次尝试（粗切先跑，预算有余再细切）
TILE_FRACTIONS = (0.5, 0.34)
# 分块本身足够小才值得再放大一次
TILE_UPSCALE_MAX_PIXELS = 1_000_000


def _load_engines() -> List[tuple]:
    """惰性加载识别引擎，返回 [(名称, 识别函数)]，缺少依赖时自动跳过。"""
    engines: List[tuple] = []

    try:
        import zxingcpp  # type: ignore

        def _zxing(image: Image.Image) -> List[str]:
            results = zxingcpp.read_barcodes(image)  # type: ignore[attr-defined]
            return [r.text for r in results if getattr(r, "text", None)]

        engines.append(("zxingcpp", _zxing))
    except Exception as exc:  # pragma: no cover - 依赖缺失时
        logger.warning("zxing-cpp 不可用：%s", exc)

    try:
        from pyzbar.pyzbar import decode as _zbar_decode  # type: ignore

        def _zbar(image: Image.Image) -> List[str]:
            results = _zbar_decode(image)
            return [_to_text(r.data) for r in results if getattr(r, "data", None)]

        engines.append(("zbar", _zbar))
    except Exception as exc:  # pragma: no cover
        logger.warning("pyzbar 不可用：%s", exc)

    return engines


def _to_text(data: bytes) -> str:
    """把二维码原始字节解码为文本，兼容 UTF-8 与 GBK 编码的中文二维码。"""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _rescale(image: Image.Image, scale: float) -> Image.Image:
    """按倍率缩放图像，scale 为 1.0 时原样返回。"""
    if abs(scale - 1.0) < 1e-6:
        return image
    width, height = image.size
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    # 放大时用最近邻保留二维码边缘锐利，缩小时用 LANCZOS 减少锯齿
    resample = Image.NEAREST if scale > 1.0 else Image.LANCZOS
    return image.resize(new_size, resample)


def _dedupe(items: List[str]) -> List[str]:
    """去重并保持原有顺序。"""
    seen = set()
    result: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _run_engines(image: Image.Image, engines: Sequence[tuple], scales: Sequence[float],
                 max_pixels: int) -> List[str]:
    """按 缩放 x 引擎 的顺序识别一张图，返回去重后的结果（可能为空）。"""
    for scale in scales:
        scaled = _rescale(image, scale)
        if scaled.width * scaled.height > max_pixels:
            continue
        for name, engine in engines:
            try:
                texts = _dedupe([t for t in engine(scaled) if t])
            except Exception as exc:
                logger.debug("引擎 %s 在 %.1fx 下失败：%s", name, scale, exc)
                continue
            if texts:
                logger.info("引擎 %s 在 %.1fx 下识别到 %d 个码", name, scale, len(texts))
                return texts
    return []


def _scale_plan(image: Image.Image, scales: Sequence[float], max_pixels: int) -> Tuple[float, ...]:
    """按图像尺寸裁剪缩放序列：大图跳过无意义的放大，超大的直接放弃。"""
    pixels = image.width * image.height
    plan: List[float] = []
    for scale in scales:
        if scale > 1.0 and pixels > UPSCALE_MAX_PIXELS:
            logger.debug("图像 %d 像素超过放大上限，跳过 %.1fx", pixels, scale)
            continue
        if int(image.width * scale) * int(image.height * scale) > max_pixels:
            continue
        plan.append(scale)
    return tuple(plan)


def _tile_spans(size: int, tile: int, step: int) -> List[Tuple[int, int]]:
    """生成 [0, size) 上的滑窗区间，保证最后一块贴到边界。"""
    spans: List[Tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + tile, size)
        spans.append((start, end))
        if end >= size:
            break
        start += step
    return spans


def iter_tiles(image: Image.Image, fraction: float,
               overlap: float = TILE_OVERLAP) -> Iterator[Image.Image]:
    """把图像切成边长为「最短边 * fraction」、带重叠的若干块。"""
    width, height = image.size
    tile = max(256, int(min(width, height) * fraction))
    step = max(1, int(tile * (1.0 - overlap)))
    for top, bottom in _tile_spans(height, tile, step):
        for left, right in _tile_spans(width, tile, step):
            yield image.crop((left, top, right, bottom))


def decode_tiles(image: Image.Image, engines: Optional[Sequence[tuple]] = None,
                 budget_ms: int = TILE_BUDGET_MS,
                 fractions: Sequence[float] = TILE_FRACTIONS,
                 max_pixels: int = 40_000_000) -> List[str]:
    """对大图做有重叠的分块滑窗扫描，返回识别到的内容（可能为空）。

    受 budget_ms 时间预算约束：每处理一块前检查是否超时，超时立即收手，
    保证 4K 全屏识别的总耗时维持在 1~2 秒量级。
    """
    if engines is None:
        engines = _load_engines()
    if not engines:
        return []

    started = time.perf_counter()
    found: List[str] = []
    scanned = 0
    for fraction in fractions:
        for tile in iter_tiles(image, fraction):
            if (time.perf_counter() - started) * 1000.0 > budget_ms:
                logger.info(
                    "分块扫描达到 %d ms 预算（已扫 %d 块），停止；累计 %d 个码",
                    budget_ms, scanned, len(found),
                )
                return _dedupe(found)
            scanned += 1
            if tile.width * tile.height <= TILE_UPSCALE_MAX_PIXELS:
                scales: Tuple[float, ...] = (1.0, 2.0)
            else:
                scales = (1.0,)
            found.extend(_run_engines(tile, engines, scales, max_pixels))
        logger.debug("分块扫描 %.2f 边长完成：%d 块，累计 %d 个码", fraction, scanned, len(found))
        if found:
            break

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info("分块扫描结束：%d 块，%d 个码，耗时 %d ms", scanned, len(found), elapsed_ms)
    return _dedupe(found)


def decode_image(
    image: Image.Image,
    scales: Sequence[float] = SCALES,
    max_pixels: int = 40_000_000,
    tile_scan: bool = True,
    engines: Optional[Sequence[tuple]] = None,
) -> List[str]:
    """识别图像中的二维码，返回文本内容列表（可能为空）。

    Args:
        image: 待识别图像。
        scales: 多尺度重试序列。
        max_pixels: 单张图的像素上限，超过则跳过该尺度。
        tile_scan: 大图直接识别失败时是否启用分块滑窗扫描。
        engines: 识别引擎列表，None 表示自动加载；测试可注入假引擎。
    """
    if engines is None:
        engines = _load_engines()
    if not engines:
        logger.error("没有任何可用的二维码识别引擎")
        return []

    if image.mode != "RGB":
        image = image.convert("RGB")

    texts = _run_engines(image, engines, _scale_plan(image, scales, max_pixels), max_pixels)
    if texts:
        return texts

    if tile_scan and min(image.width, image.height) > TILE_MIN_SOURCE:
        logger.info("多尺度识别失败，对 %dx%d 图像启用分块扫描", image.width, image.height)
        return decode_tiles(image, engines, TILE_BUDGET_MS, TILE_FRACTIONS, max_pixels)
    return []


def decode_file(path: str) -> List[str]:
    """识别本地图片文件中的二维码。"""
    with Image.open(path) as img:
        return decode_image(img.convert("RGB"))


__all__ = [
    "SCALES",
    "TILE_BUDGET_MS",
    "TILE_FRACTIONS",
    "TILE_MIN_SOURCE",
    "TILE_OVERLAP",
    "UPSCALE_MAX_PIXELS",
    "decode_file",
    "decode_image",
    "decode_tiles",
    "iter_tiles",
]
