"""撮影時刻を映像画素へ焼き込む共通renderer。"""

from __future__ import annotations

import importlib
from collections import OrderedDict
from datetime import UTC, datetime
from threading import local
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from raspi_recorder.domain.camera import CameraSettings, OverlayCorner, OverlaySize
from raspi_recorder.domain.frames import FrameEnvelope

_OVERLAY_HEIGHT_RATIO = {
    OverlaySize.SMALL: 0.025,
    OverlaySize.MEDIUM: 0.035,
    OverlaySize.LARGE: 0.05,
}
_MAX_FONT_CACHE_ENTRIES = 16
_MAX_MASK_CACHE_ENTRIES = 8
_MAX_MASK_CACHE_BYTES = 2 * 1024 * 1024
_THREAD_CACHE = local()
_MaskKey = tuple[str, str, int, int, OverlayCorner, OverlaySize]
_MaskEntry = tuple[Any, int, int, Any, int, int]


class _OverlayThreadCache:
    """Pillow objectを作成したrender thread内だけで再利用する。"""

    def __init__(self) -> None:
        self.fonts: OrderedDict[int, Any] = OrderedDict()
        self.masks: OrderedDict[_MaskKey, _MaskEntry] = OrderedDict()
        self.mask_bytes = 0
        self.font_loads = 0
        self.mask_hits = 0
        self.mask_misses = 0

    def font(self, font_size: int, image_font: Any) -> Any:
        font = self.fonts.get(font_size)
        if font is not None:
            self.fonts.move_to_end(font_size)
            return font
        font = image_font.load_default(size=font_size)
        self.font_loads += 1
        self.fonts[font_size] = font
        while len(self.fonts) > _MAX_FONT_CACHE_ENTRIES:
            self.fonts.popitem(last=False)
        return font

    def mask(self, key: _MaskKey) -> _MaskEntry | None:
        entry = self.masks.get(key)
        if entry is None:
            self.mask_misses += 1
            return None
        self.masks.move_to_end(key)
        self.mask_hits += 1
        return entry

    def remember_mask(self, key: _MaskKey, entry: _MaskEntry) -> None:
        stroke_mask, _stroke_x, _stroke_y, fill_mask, _fill_x, _fill_y = entry
        size_bytes = int(stroke_mask.width) * int(stroke_mask.height)
        size_bytes += int(fill_mask.width) * int(fill_mask.height)
        if size_bytes > _MAX_MASK_CACHE_BYTES:
            return
        previous = self.masks.pop(key, None)
        if previous is not None:
            previous_stroke, _x, _y, previous_fill, _fx, _fy = previous
            self.mask_bytes -= int(previous_stroke.width) * int(previous_stroke.height)
            self.mask_bytes -= int(previous_fill.width) * int(previous_fill.height)
        self.masks[key] = entry
        self.mask_bytes += size_bytes
        while (
            len(self.masks) > _MAX_MASK_CACHE_ENTRIES
            or self.mask_bytes > _MAX_MASK_CACHE_BYTES
        ):
            _old_key, old_entry = self.masks.popitem(last=False)
            old_stroke, _x, _y, old_fill, _fx, _fy = old_entry
            self.mask_bytes -= int(old_stroke.width) * int(old_stroke.height)
            self.mask_bytes -= int(old_fill.width) * int(old_fill.height)


def _thread_cache() -> _OverlayThreadCache:
    cache = getattr(_THREAD_CACHE, "cache", None)
    if cache is None:
        cache = _OverlayThreadCache()
        _THREAD_CACHE.cache = cache
    return cache


def draw_timestamp_overlay(
    image: Any,
    envelope: FrameEnvelope,
    settings: CameraSettings,
    *,
    enabled: bool | None = None,
) -> None:
    """設定された位置・サイズで、frame取得時刻をOS timezoneへ焼き込む。

    Font objectと同一秒の小さなRGBA maskは呼出元thread内だけで共有する。
    Cache件数とpixel bytesに上限を設け、画像全体や過去時刻は保持しない。
    """
    if not isinstance(envelope, FrameEnvelope):
        raise ValueError("envelopeはFrameEnvelopeが必要です")
    if not isinstance(settings, CameraSettings):
        raise ValueError("settingsはCameraSettingsが必要です")
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("enabledはboolまたはNoneで指定してください")
    should_draw = settings.overlay_enabled if enabled is None else enabled
    if not should_draw:
        return

    try:
        timezone = ZoneInfo(envelope.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("frame timezoneがIANA timezoneとして不正です") from exc
    seconds, nanos = divmod(envelope.capture_utc_ns, 1_000_000_000)
    timestamp = (
        datetime.fromtimestamp(seconds, tz=UTC)
        .replace(microsecond=nanos // 1_000)
        .astimezone(timezone)
    )
    text = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    width = int(image.width)
    height = int(image.height)
    key = (
        text,
        envelope.timezone,
        width,
        height,
        settings.overlay_corner,
        settings.overlay_size,
    )

    image_draw = importlib.import_module("PIL.ImageDraw")
    image_font = importlib.import_module("PIL.ImageFont")
    image_module = importlib.import_module("PIL.Image")
    cache = _thread_cache()
    cached = cache.mask(key)
    if cached is None:
        draw = image_draw.Draw(image)
        font_size = max(
            8,
            round(height * _OVERLAY_HEIGHT_RATIO[settings.overlay_size]),
        )
        margin = max(2, round(height * 0.02))
        stroke_width = max(1, round(font_size * 0.08))
        while True:
            font = cache.font(font_size, image_font)
            left, top, right, bottom = draw.textbbox(
                (0, 0), text, font=font, stroke_width=stroke_width
            )
            text_width = right - left
            text_height = bottom - top
            if text_width <= width - 2 * margin or font_size <= 8:
                break
            font_size -= 1
            stroke_width = max(1, round(font_size * 0.08))

        text_width = max(1, text_width)
        text_height = max(1, text_height)
        if settings.overlay_corner in {
            OverlayCorner.TOP_LEFT,
            OverlayCorner.BOTTOM_LEFT,
        }:
            text_x = margin
        else:
            text_x = width - margin - text_width
        if settings.overlay_corner in {
            OverlayCorner.TOP_LEFT,
            OverlayCorner.TOP_RIGHT,
        }:
            text_y = margin
        else:
            text_y = height - margin - text_height
        text_anchor = (text_x - left, text_y - top)
        getmask2 = getattr(font, "getmask2", None)
        if getmask2 is None:
            raise RuntimeError("Pillowのfontはgetmask2をサポートしていません")
        stroke_core, stroke_offset = getmask2(
            text,
            mode="L",
            stroke_width=stroke_width,
            ink=0,
            start=(0.0, 0.0),
        )
        fill_core, fill_offset = getmask2(
            text,
            mode="L",
            ink=255,
            start=(0.0, 0.0),
        )
        stroke_mask = image_module.frombytes("L", stroke_core.size, bytes(stroke_core))
        fill_mask = image_module.frombytes("L", fill_core.size, bytes(fill_core))
        cached = (
            stroke_mask,
            text_anchor[0] + stroke_offset[0],
            text_anchor[1] + stroke_offset[1],
            fill_mask,
            text_anchor[0] + fill_offset[0],
            text_anchor[1] + fill_offset[1],
        )
        cache.remember_mask(key, cached)

    stroke_mask, stroke_x, stroke_y, fill_mask, fill_x, fill_y = cached
    image.paste((0, 0, 0), (stroke_x, stroke_y), stroke_mask)
    image.paste((255, 255, 255), (fill_x, fill_y), fill_mask)
