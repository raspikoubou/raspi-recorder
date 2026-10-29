"""録画artifactとclock boundary規則。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from raspi_recorder.domain.capacity import RecordingFileState, VolumeKind

NANOSECONDS_PER_SECOND = 1_000_000_000
CONTINUOUS_SEGMENT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class RecordingArtifact:
    """正常確定した物理MP4。"""

    path: str
    frame_count: int
    first_sequence: int
    last_sequence: int
    first_mono_ns: int
    last_mono_ns: int
    first_utc_ns: int
    last_utc_ns: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RecordingProgress:
    """writing中に索引へ定期反映する、file frame進捗。"""

    frame_count: int
    first_sequence: int
    last_sequence: int
    start_mono_ns: int
    end_mono_ns: int
    start_utc_ns: int
    end_utc_ns: int
    size_bytes: int
    spans: tuple[RecordingTimeSpan, ...]


@dataclass(frozen=True, slots=True)
class VolumeSnapshot:
    """永続識別子と現在の接続状態を持つ媒体。"""

    volume_id: str
    persistent_id: str
    kind: VolumeKind
    fs_type: str
    marker_id: str
    connected: bool
    selected: bool
    last_seen_utc_ns: int


@dataclass(frozen=True, slots=True)
class RecordingTimeSpan:
    """wall clockの段差・timezone変更をまたがない撮影区間。"""

    first_sequence: int
    last_sequence: int
    start_mono_ns: int
    end_mono_ns: int
    start_utc_ns: int
    end_utc_ns: int
    timezone: str


@dataclass(frozen=True, slots=True)
class RecordingSnapshot:
    """1つの物理fileと検索・復旧に使う索引情報。"""

    recording_id: str
    creation_sequence: int
    volume_id: str
    relative_path: str
    mode: str  # Legacy catalog metadata; never selects a recording behavior.
    camera_id: int
    logical_event_id: str | None
    part_index: int
    state: RecordingFileState
    boot_id: str
    writer_epoch: str
    start_mono_ns: int | None
    end_mono_ns: int | None
    start_utc_ns: int | None
    end_utc_ns: int | None
    duration_ns: int | None
    size_bytes: int | None
    created_utc_ns: int
    end_reason: str | None
    spans: tuple[RecordingTimeSpan, ...]


def next_continuous_boundary_mono_ns(
    monotonic_ns: int, utc_ns: int, timezone_name: str
) -> int:
    """次のローカル00/05/…分境界を、固定monotonic期限へ変換する。"""
    if type(monotonic_ns) is not int or monotonic_ns < 0:
        raise ValueError("monotonic_nsは0以上の整数で指定してください")
    if type(utc_ns) is not int or utc_ns < 0:
        raise ValueError("utc_nsは0以上の整数で指定してください")
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone_nameは有効なIANA名で指定してください") from exc

    current_second, _ = divmod(utc_ns, NANOSECONDS_PER_SECOND)
    for seconds_ahead in range(1, CONTINUOUS_SEGMENT_SECONDS + 2):
        boundary_second = current_second + seconds_ahead
        local = datetime.fromtimestamp(boundary_second, UTC).astimezone(timezone)
        if local.second == 0 and local.minute % 5 == 0:
            boundary_utc_ns = boundary_second * NANOSECONDS_PER_SECOND
            return monotonic_ns + (boundary_utc_ns - utc_ns)
    raise RuntimeError("次の5分境界を計算できません")
