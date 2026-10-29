"""二维码识别：zxing-cpp 与 zbar 双引擎 + 多尺度重试。

识别流程：
1. 先用 zxing-cpp 在原图上识别（对大图小码的鲁棒性最好）；
2. 失败则换 zbar 再试；
3. 仍失败，则对图像做缩放（放大便于识别小码、缩小便于识别超大码）后重复上述过程。

解码得到的字节按 utf-8 → gb18030 → latin-1 顺序尝试，保证中文二维码不乱码。
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from PIL import Image

logger = logging.getLogger("QRClipper")

# 缩放策略：先原图，再放大（抓小码），最后缩小（超大二维码）
SCALES = (1.0, 2.0, 0.5)


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


def decode_image(
    image: Image.Image, scales: tuple = SCALES, max_pixels: int = 40_000_000
) -> List[str]:
    """识别图像中的二维码，返回文本内容列表（可能为空）。"""
    engines = _load_engines()
    if not engines:
        logger.error("没有任何可用的二维码识别引擎")
        return []

    if image.mode != "RGB":
        image = image.convert("RGB")

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


def decode_file(path: str) -> List[str]:
    """识别本地图片文件中的二维码。"""
    with Image.open(path) as img:
        return decode_image(img.convert("RGB"))
