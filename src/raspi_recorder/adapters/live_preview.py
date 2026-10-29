"""camera単位で共有する有界1fps JPEGプレビュー。"""

from __future__ import annotations

import importlib
import io
from threading import Condition, Thread
from typing import Any

from raspi_recorder.adapters.timestamp_overlay import draw_timestamp_overlay
from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.frames import FrameEnvelope
from raspi_recorder.ports.clock import Clock

_PREVIEW_INTERVAL_NS = 1_000_000_000
_PREVIEW_FRESHNESS_NS = 5_000_000_000
_PREVIEW_DEMAND_TTL_NS = 10_000_000_000
_PREVIEW_MAX_WIDTH = 640
_PREVIEW_MAX_HEIGHT = 360
_PREVIEW_MAX_BYTES = 1_000_000


class LivePreviewPublisher:
    """最後の1枚だけを共有し、画像処理をcameraごとに毎秒1回行う。"""

    def __init__(
        self,
        *,
        generation: int,
        settings: CameraSettings,
        clock: Clock,
    ) -> None:
        if type(generation) is not int or generation < 1:
            raise ValueError("generationは1以上の整数で指定してください")
        if not isinstance(settings, CameraSettings):
            raise ValueError("settingsはCameraSettingsで指定してください")
        self._generation = generation
        self._settings = settings
        self._clock = clock
        self._condition = Condition()
        self._pending: FrameEnvelope | None = None
        self._jpeg: bytes | None = None
        self._published_mono_ns: int | None = None
        self._viewer_expires_ns = 0
        self._viewer_revision = 0
        self._last_render_started_ns: int | None = None
        self._error: str | None = None
        self._stop = False
        self._started = False
        self._thread: Thread | None = None

    @property
    def status(self) -> dict[str, Any]:
        """利用者向けに安全な鮮度と更新状態だけを返す。"""
        with self._condition:
            now_ns = self._clock.monotonic_ns()
            active = now_ns < self._viewer_expires_ns
            fresh = (
                self._jpeg is not None
                and self._published_mono_ns is not None
                and now_ns - self._published_mono_ns <= _PREVIEW_FRESHNESS_NS
            )
            return {
                "available": bool(active and fresh and self._error is None),
                "fresh": bool(fresh),
                "error": self._error,
            }

    @property
    def viewer_demand_active(self) -> bool:
        """有効な画面需要があるかを返す。"""
        with self._condition:
            return self._clock.monotonic_ns() < self._viewer_expires_ns

    def start(self) -> None:
        """単一のbounded render workerを開始する。"""
        with self._condition:
            if self._started:
                raise RuntimeError("preview publisherは既に起動しています")
            self._stop = False
            self._started = True
            self._thread = Thread(
                target=self._run,
                name=f"recorder-live-preview-cam{self._generation}",
                daemon=True,
            )
            self._thread.start()

    def request_viewer(self) -> bool:
        """カメラ単位の画面需要を10秒延長する。"""
        with self._condition:
            if not self._started or self._stop:
                return False
            self._viewer_expires_ns = (
                self._clock.monotonic_ns() + _PREVIEW_DEMAND_TTL_NS
            )
            self._condition.notify_all()
            return True

    def release_viewer(self) -> None:
        """画面が隠れたとき需要と保持frameをすぐ解放する。"""
        with self._condition:
            self._viewer_revision += 1
            self._viewer_expires_ns = 0
            self._pending = None
            self._jpeg = None
            self._published_mono_ns = None
            self._condition.notify_all()

    def submit(self, frame: FrameEnvelope) -> bool:
        """captureを待たせず、最新候補1枚だけを差し替える。"""
        if not isinstance(frame, FrameEnvelope):
            raise ValueError("frameはFrameEnvelopeが必要です")
        if frame.generation != self._generation:
            return False
        with self._condition:
            if (
                not self._started
                or self._stop
                or self._clock.monotonic_ns() >= self._viewer_expires_ns
            ):
                return False
            self._pending = frame
            self._condition.notify()
            return True

    def update_settings(self, settings: CameraSettings) -> None:
        """次に作る画像から日時表示設定を反映する。"""
        if not isinstance(settings, CameraSettings):
            raise ValueError("settingsはCameraSettingsで指定してください")
        with self._condition:
            self._settings = settings
            self._condition.notify_all()

    def fresh_image(self) -> bytes | None:
        """5秒以内に作った画像を返し、古い画像は更新不能として隠す。"""
        with self._condition:
            now_ns = self._clock.monotonic_ns()
            if (
                self._jpeg is None
                or self._published_mono_ns is None
                or now_ns - self._published_mono_ns > _PREVIEW_FRESHNESS_NS
            ):
                return None
            return self._jpeg

    def close(self, timeout_seconds: float = 2.0) -> None:
        """workerを止め、保持画像・処理待ちframeを解放する。"""
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_secondsは正の数値が必要です")
        with self._condition:
            if not self._started:
                return
            self._stop = True
            self._pending = None
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(float(timeout_seconds))
            if thread.is_alive():
                raise TimeoutError("live_preview_worker_stop_timeout")
        with self._condition:
            self._jpeg = None
            self._published_mono_ns = None
            self._thread = None
            self._started = False

    def _run(self) -> None:
        """視聴需要がある間だけ最新frameを一秒間隔で描画する。"""
        while True:
            with self._condition:
                while not self._stop:
                    now_ns = self._clock.monotonic_ns()
                    if now_ns >= self._viewer_expires_ns:
                        self._pending = None
                        self._condition.wait(0.5)
                        continue
                    last_started = self._last_render_started_ns
                    if last_started is not None:
                        wait_ns = _PREVIEW_INTERVAL_NS - (now_ns - last_started)
                        if wait_ns > 0:
                            self._condition.wait(min(wait_ns / 1_000_000_000, 0.5))
                            continue
                    if self._pending is None:
                        self._condition.wait(0.5)
                        continue
                    frame = self._pending
                    self._pending = None
                    settings = self._settings
                    viewer_revision = self._viewer_revision
                    self._last_render_started_ns = now_ns
                    break
                else:
                    return
                if self._stop:
                    return
            try:
                jpeg = self._render(frame, settings)
            except Exception as exc:
                with self._condition:
                    if viewer_revision == self._viewer_revision:
                        self._error = type(exc).__name__
                continue
            with self._condition:
                if self._stop:
                    return
                if (
                    viewer_revision != self._viewer_revision
                    or self._clock.monotonic_ns() >= self._viewer_expires_ns
                ):
                    continue
                self._jpeg = jpeg
                self._published_mono_ns = self._clock.monotonic_ns()
                self._error = None

    @staticmethod
    def _render(frame: FrameEnvelope, settings: CameraSettings) -> bytes:
        """resize・timestamp・JPEG処理を現在frameだけに行う。"""
        image_module = importlib.import_module("PIL.Image")
        with image_module.open(io.BytesIO(frame.jpeg_bytes)) as source:
            source.load()
            image = source.convert("RGB")
        resampling = getattr(image_module, "Resampling", image_module).LANCZOS
        image.thumbnail((_PREVIEW_MAX_WIDTH, _PREVIEW_MAX_HEIGHT), resampling)
        draw_timestamp_overlay(image, frame, settings)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=76, optimize=False)
        result = output.getvalue()
        if not result or len(result) > _PREVIEW_MAX_BYTES:
            raise ValueError("preview_image_size_out_of_range")
        return result
