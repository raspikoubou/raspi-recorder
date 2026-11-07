"""物理録画のread lease・確認付き削除・媒体内安全open。"""

from __future__ import annotations

import secrets
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import RLock
from typing import BinaryIO

from raspi_recorder.domain.capacity import RecordingFileState
from raspi_recorder.domain.recordings import RecordingSnapshot
from raspi_recorder.ports.clock import Clock
from raspi_recorder.ports.recording_index import RecordingIndex
from raspi_recorder.ports.storage import ManagedVolumeLocator, VolumeStore


class RecordingAccessError(RuntimeError):
    """録画state・lease・確認の競合。"""


class RecordingReadLease:
    """1つのopen fileと削除を防ぐin-process lease。"""

    def __init__(
        self,
        recording: RecordingSnapshot,
        handle: BinaryIO,
        context: AbstractContextManager[BinaryIO],
        relative_path: str,
        release: Callable[[str], None],
    ) -> None:
        self.recording = recording
        self.file = handle
        self.relative_path = relative_path
        self._context = context
        self._release = release
        self._closed = False

    @property
    def closed(self) -> bool:
        """leaseが解放済みか返す。"""
        return self._closed

    def close(self) -> None:
        """fileをcloseしてread countを戻す。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._context.__exit__(None, None, None)
        finally:
            self._release(self.recording.recording_id)

    def __enter__(self) -> RecordingReadLease:
        if self._closed:
            raise RuntimeError("解放済みread leaseは再利用できません")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class DeletePreview:
    """短時間だけ有効な、ID明示削除の確認情報。"""

    confirmation_id: str
    recording_ids: tuple[str, ...]
    recording_count: int
    size_bytes: int
    expires_mono_ns: int


@dataclass(frozen=True, slots=True)
class DeleteItemResult:
    """一括削除の1物理file結果。"""

    recording_id: str
    deleted: bool
    code: str | None


class RecordingAccessService:
    """検索・再生・download・deleteの同一lease調停。"""

    _PREVIEW_LIFETIME_NS = 120_000_000_000
    _MAX_DELETE_IDS = 100
    _MAX_DELETE_PREVIEWS = 1_024

    def __init__(
        self,
        catalog: RecordingIndex,
        volumes: ManagedVolumeLocator,
        volume_store: VolumeStore,
        clock: Clock,
    ) -> None:
        self._catalog = catalog
        self._volumes = volumes
        self._volume_store = volume_store
        self._clock = clock
        self._lock = RLock()
        self._read_counts: Counter[str] = Counter()
        self._previews: dict[str, DeletePreview] = {}

    def read_lease(
        self, recording_id: str, *, purpose: str = "playback"
    ) -> RecordingReadLease:
        """同じ調停lock内でstateを検証し、safe fdを開いてleaseする。"""
        if purpose not in {"playback", "download", "export"}:
            raise ValueError("purposeが不正です")
        with self._lock:
            recording = self._require_connected_recording(recording_id)
            permitted = {RecordingFileState.FINALIZED}
            if purpose == "download":
                # Preserve manual recovery downloads, but never put abnormal
                # partial files into a batch ZIP export.
                permitted.add(RecordingFileState.ABNORMAL)
            if recording.state not in permitted:
                if recording.state is RecordingFileState.WRITING:
                    raise RecordingAccessError("recording_is_writing")
                raise RecordingAccessError("recording_not_readable")
            volume, root = self._volumes.resolve(recording.volume_id)
            candidates = [recording.relative_path]
            if recording.state is RecordingFileState.ABNORMAL:
                candidates.append(f"{recording.relative_path}.partial")
            opened: tuple[AbstractContextManager[BinaryIO], BinaryIO, str] | None = None
            for relative_path in candidates:
                context = self._volume_store.open_managed_file(
                    volume, root, relative_path
                )
                try:
                    handle = context.__enter__()
                except FileNotFoundError:
                    continue
                opened = (context, handle, relative_path)
                break
            if opened is None:
                raise RecordingAccessError("recording_file_missing")
            context, handle, opened_path = opened
            self._read_counts[recording_id] += 1
            return RecordingReadLease(
                recording,
                handle,
                context,
                opened_path,
                self._release_read_lease,
            )

    def read_lease_count(self, recording_id: str) -> int:
        """自動・手動削除との競合判定用read count。"""
        with self._lock:
            return self._read_counts[recording_id]

    def preview_delete(self, recording_ids: list[str]) -> DeletePreview:
        """現在の状態と総byteを再検証して短命confirmation IDを作る。"""
        normalized = self._validate_ids(recording_ids)
        with self._lock:
            self._expire_previews_locked()
            recordings = [
                self._require_connected_recording(recording_id)
                for recording_id in normalized
            ]
            if any(
                recording.state
                not in {RecordingFileState.FINALIZED, RecordingFileState.ABNORMAL}
                for recording in recordings
            ):
                raise RecordingAccessError("recording_not_deletable")
            preview = DeletePreview(
                confirmation_id=secrets.token_urlsafe(32),
                recording_ids=normalized,
                recording_count=len(recordings),
                size_bytes=sum(recording.size_bytes or 0 for recording in recordings),
                expires_mono_ns=(
                    self._clock.monotonic_ns() + self._PREVIEW_LIFETIME_NS
                ),
            )
            while len(self._previews) >= self._MAX_DELETE_PREVIEWS:
                oldest_token = next(iter(self._previews))
                self._previews.pop(oldest_token, None)
            self._previews[preview.confirmation_id] = preview
            return preview

    def delete_confirmed(
        self, confirmation_id: str, recording_ids: list[str]
    ) -> tuple[DeleteItemResult, ...]:
        """同じIDだけを再検証し、各fileを個別にunlink・index削除する。"""
        normalized = self._validate_ids(recording_ids)
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise ValueError("confirmation_idが必要です")
        with self._lock:
            self._expire_previews_locked()
            preview = self._previews.pop(confirmation_id, None)
            if preview is None:
                raise RecordingAccessError("delete_confirmation_expired")
            if preview.recording_ids != normalized:
                raise RecordingAccessError("delete_confirmation_ids_changed")

            results: list[DeleteItemResult] = []
            for recording_id in normalized:
                results.append(self._delete_one_locked(recording_id))
            return tuple(results)

    def delete_automatic(self, recording_id: str) -> DeleteItemResult:
        """容量保護用に、同じlease lockの下で最古候補を1件消す。"""
        if not isinstance(recording_id, str) or not recording_id:
            raise ValueError("recording_idが必要です")
        with self._lock:
            if self._read_counts[recording_id] > 0:
                return DeleteItemResult(recording_id, False, "recording_in_use")
            return self._delete_one_locked(recording_id)

    def _delete_one_locked(self, recording_id: str) -> DeleteItemResult:
        try:
            recording = self._require_connected_recording(recording_id)
            if recording.state not in {
                RecordingFileState.FINALIZED,
                RecordingFileState.ABNORMAL,
            }:
                return DeleteItemResult(recording_id, False, "recording_not_deletable")
            if self._read_counts[recording_id] > 0:
                return DeleteItemResult(recording_id, False, "recording_in_use")
            volume, root = self._volumes.resolve(recording.volume_id)
            previous_state = self._catalog.mark_recording_deleting(recording_id)
            removed_path: str | None = None
            candidates = [recording.relative_path]
            if recording.state is RecordingFileState.ABNORMAL:
                candidates.append(f"{recording.relative_path}.partial")
            try:
                for relative_path in candidates:
                    try:
                        self._volume_store.unlink_managed_file(
                            volume, root, relative_path
                        )
                    except FileNotFoundError:
                        continue
                    removed_path = relative_path
                    break
            except Exception:
                try:
                    self._catalog.restore_recording_state(recording_id, previous_state)
                except Exception:
                    pass
                raise
            if removed_path is None:
                self._catalog.mark_recording_missing(recording_id)
                return DeleteItemResult(recording_id, False, "recording_file_missing")
            manifest_path = f"{recording.relative_path}.recording.json"
            try:
                self._volume_store.unlink_managed_file(volume, root, manifest_path)
            except FileNotFoundError:
                pass
            except Exception:
                try:
                    self._catalog.mark_recording_missing(recording_id)
                except Exception:
                    pass
                return DeleteItemResult(
                    recording_id, False, "recording_manifest_delete_failed"
                )
            try:
                self._catalog.delete_recording_index(recording_id)
            except Exception:
                try:
                    self._catalog.mark_recording_missing(recording_id)
                except Exception:
                    pass
                raise
            return DeleteItemResult(recording_id, True, None)
        except RecordingAccessError as exc:
            return DeleteItemResult(recording_id, False, str(exc))
        except (KeyError, FileNotFoundError):
            return DeleteItemResult(recording_id, False, "recording_or_volume_missing")
        except Exception as exc:
            return DeleteItemResult(
                recording_id,
                False,
                f"delete_failed:{type(exc).__name__}",
            )

    def _require_connected_recording(self, recording_id: str) -> RecordingSnapshot:
        if not isinstance(recording_id, str) or not recording_id:
            raise ValueError("recording_idが必要です")
        recording = self._catalog.get_recording(recording_id)
        if recording is None:
            raise RecordingAccessError("recording_not_found")
        volumes = {
            volume.volume_id: volume
            for volume in self._catalog.list_volumes(connected_only=True)
        }
        volume = volumes.get(recording.volume_id)
        if volume is None:
            raise RecordingAccessError("volume_not_connected")
        mounted, _ = self._volumes.resolve(recording.volume_id)
        if mounted.persistent_id != volume.persistent_id:
            raise RecordingAccessError("volume_identity_changed")
        return recording

    def _release_read_lease(self, recording_id: str) -> None:
        with self._lock:
            if self._read_counts[recording_id] <= 1:
                self._read_counts.pop(recording_id, None)
            else:
                self._read_counts[recording_id] -= 1

    def _expire_previews_locked(self) -> None:
        now_ns = self._clock.monotonic_ns()
        self._previews = {
            token: preview
            for token, preview in self._previews.items()
            if preview.expires_mono_ns > now_ns
        }

    @classmethod
    def _validate_ids(cls, recording_ids: list[str]) -> tuple[str, ...]:
        if not isinstance(recording_ids, list) or not 1 <= len(recording_ids) <= (
            cls._MAX_DELETE_IDS
        ):
            raise ValueError("recording_idsは1～100件の配列が必要です")
        if not all(isinstance(item, str) and item for item in recording_ids):
            raise ValueError("recording_idsに空または不正なIDがあります")
        if len(set(recording_ids)) != len(recording_ids):
            raise ValueError("recording_idsの重複は許可されません")
        return tuple(recording_ids)
