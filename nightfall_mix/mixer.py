from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Optional

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.config import OrderMode, SmartOrderingMode
from nightfall_mix.utils import ffprobe_json, read_playlist_file, seeded_random

SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".m4a",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
}

KEY_TO_PC = {
    "C": 0,
    "C#": 1,
    "D": 2,
    "D#": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "G": 7,
    "G#": 8,
    "A": 9,
    "A#": 10,
    "B": 11,
}


@dataclass
class TrackSource:
    id: str
    path: Path
    duration_ms: int
    sample_rate: Optional[int] = None
    channels: Optional[int] = None


@dataclass
class TrackInstance:
    instance_index: int
    track: TrackSource
    cycle_index: int


@dataclass
class TransitionPlan:
    from_track_id: str
    to_track_id: str
    crossfade_ms: int
    smart_used: bool
    reason: str
    key_distance: Optional[int] = None
    lpf_duck_ms: Optional[int] = None


@dataclass
class TimelineEntry:
    instance_index: int
    track_id: str
    filename: str
    start_time_ms: int
    end_time_ms: int
    cycle_index: int
    analysis_snapshot: dict[str, Optional[float | str]] = field(default_factory=dict)


@dataclass
class MixPlan:
    instances: list[TrackInstance]
    timeline: list[TimelineEntry]
    transitions: list[TransitionPlan]
    estimated_duration_ms: int
    target_reached: bool


def discover_audio_files(folder: Path) -> list[Path]:
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS]
    return sorted(files, key=natural_name_key)


def natural_name_key(path: Path) -> tuple:
    # Natural sort: "2.mp3" comes before "10.mp3" while keeping case-insensitive text ordering.
    parts = re.split(r"(\d+)", path.name.casefold())
    key: list[object] = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part)
    return tuple(key)


def _find_m3u_file(folder: Path) -> Optional[Path]:
    playlists = sorted(folder.glob("*.m3u"), key=natural_name_key)
    return playlists[0] if playlists else None


def order_track_paths(
    file_paths: list[Path],
    order: OrderMode,
    songs_folder: Path,
    seed: Optional[int],
) -> list[Path]:
    if order == OrderMode.alpha:
        return sorted(file_paths, key=natural_name_key)
    if order == OrderMode.random:
        rng = seeded_random(seed)
        shuffled = list(file_paths)
        rng.shuffle(shuffled)
        return shuffled

    m3u_file = _find_m3u_file(songs_folder)
    if m3u_file is None:
        raise FileNotFoundError("order=m3u requested but no .m3u found in songs folder")

    available_by_name = {p.name.casefold(): p for p in file_paths}
    available_by_abs = {str(p.resolve()).casefold(): p for p in file_paths}
    ordered: list[Path] = []
    missing: list[str] = []
    for entry in read_playlist_file(m3u_file):
        candidate = Path(entry)
        if not candidate.is_absolute():
            candidate = (songs_folder / candidate).resolve()
        key_abs = str(candidate).casefold()
        key_name = Path(entry).name.casefold()
        if key_abs in available_by_abs:
            ordered.append(available_by_abs[key_abs])
        elif key_name in available_by_name:
            ordered.append(available_by_name[key_name])
        else:
            missing.append(entry)
    if not ordered:
        raise RuntimeError("m3u playlist did not map to any discoverable audio files")
    if missing:
        warnings.warn(
            f"m3u references missing files (using matched entries only): {', '.join(missing[:5])}",
            UserWarning,
            stacklevel=2,
        )
    return ordered


def build_track_sources(paths: list[Path], logger) -> list[TrackSource]:
    sources: list[TrackSource] = []
    for idx, path in enumerate(paths):
        data = ffprobe_json(path, logger=logger)
        fmt = data.get("format", {})
        duration_s = fmt.get("duration")
        if duration_s is None:
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "audio" and stream.get("duration") is not None:
                    duration_s = stream.get("duration")
                    break
        if duration_s is None:
            raise RuntimeError(f"ffprobe could not determine duration for {path}")
        sample_rate = None
        channels = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                if stream.get("sample_rate") is not None:
                    sample_rate = int(stream["sample_rate"])
                if stream.get("channels") is not None:
                    channels = int(stream["channels"])
                break
        sources.append(
            TrackSource(
                id=f"t{idx}",
                path=path,
                duration_ms=max(1, int(float(duration_s) * 1000)),
                sample_rate=sample_rate,
                channels=channels,
            )
        )
    return sources


def build_instances(
    base_tracks: list[TrackSource],
    order: OrderMode,
    seed: Optional[int],
    target_duration_min: Optional[int],
    crossfade_sec: float = 0.0,
) -> list[TrackInstance]:
    if not base_tracks:
        return []

    rng = seeded_random(seed)
    target_ms = target_duration_min * 60_000 if target_duration_min else None
    total_ms = 0
    cycle_index = 0
    instances: list[TrackInstance] = []
    next_idx = 0

    while True:
        if cycle_index == 0:
            cycle_tracks = list(base_tracks)
        elif order == OrderMode.random:
            cycle_tracks = list(base_tracks)
            rng.shuffle(cycle_tracks)
        else:
            cycle_tracks = list(base_tracks)

        for track in cycle_tracks:
            instances.append(TrackInstance(instance_index=next_idx, track=track, cycle_index=cycle_index))
            next_idx += 1
            total_ms += track.duration_ms

        if target_ms is None:
            break
        overlap_penalty = max(0, len(instances) - 1) * int(crossfade_sec * 1000)
        effective_ms = max(0, total_ms - overlap_penalty)
        if effective_ms >= target_ms:
            break
        cycle_index += 1
    return instances


def _parse_key(key: Optional[str]) -> Optional[int]:
    if not key:
        return None
    note = key.split(":")[0]
    return KEY_TO_PC.get(note)


def key_distance(key_a: Optional[str], key_b: Optional[str]) -> Optional[int]:
    a = _parse_key(key_a)
    b = _parse_key(key_b)
    if a is None or b is None:
        return None
    diff = abs(a - b)
    return min(diff, 12 - diff)


def order_sources_by_transition_fit(
    sources: list[TrackSource],
    analyses: dict[str, TrackAnalysis],
    mode: SmartOrderingMode = SmartOrderingMode.bpm_key_balanced,
) -> list[TrackSource]:
    if len(sources) < 2:
        return list(sources)

    if not any((analyses.get(src.id) and analyses[src.id].bpm is not None) for src in sources):
        return list(sources)

    def confidence(analysis: Optional[TrackAnalysis]) -> float:
        if analysis is None:
            return 0.0
        return float((analysis.bpm_confidence or 0.0) + (analysis.key_confidence or 0.0))

    def transition_cost(left: TrackSource, right: TrackSource) -> float:
        left_analysis = analyses.get(left.id)
        right_analysis = analyses.get(right.id)
        score = 0.0

        if left_analysis and right_analysis and left_analysis.bpm and right_analysis.bpm:
            bpm_delta = min(abs(left_analysis.bpm - right_analysis.bpm), 80.0)
            score += bpm_delta / 16.0
        else:
            score += 2.0

        dist = key_distance(
            left_analysis.key if left_analysis else None,
            right_analysis.key if right_analysis else None,
        )
        if mode == SmartOrderingMode.bpm_key_balanced:
            score += (dist / 6.0) if dist is not None else 0.7
        else:
            score += (dist / 16.0) if dist is not None else 0.25

        score += max(0.0, 0.8 - confidence(right_analysis))
        return score

    remaining = list(sources)
    remaining.sort(key=lambda s: s.path.name.casefold())
    start = max(remaining, key=lambda s: (confidence(analyses.get(s.id)), -len(s.path.name)))
    ordered: list[TrackSource] = [start]
    remaining.remove(start)
    current = start

    while remaining:
        nxt = min(
            remaining,
            key=lambda candidate: (transition_cost(current, candidate), candidate.path.name.casefold()),
        )
        ordered.append(nxt)
        remaining.remove(nxt)
        current = nxt
    return ordered


def _crossfade_bounds(left: TrackSource, right: TrackSource) -> tuple[int, int]:
    min_ms = 500
    max_allowed = max(min_ms, min(left.duration_ms, right.duration_ms) - 500)
    return min_ms, max_allowed


def _smart_crossfade_ms(
    left: TrackSource,
    right: TrackSource,
    left_analysis: Optional[TrackAnalysis],
    right_analysis: Optional[TrackAnalysis],
    base_ms: int,
) -> tuple[int, str]:
    min_ms, max_allowed = _crossfade_bounds(left, right)
    max_ms = 10000
    if max_allowed <= min_ms:
        return max(min_ms, min(base_ms, max_allowed)), "fixed-short-track"

    if not left_analysis or not right_analysis:
        return max(min_ms, min(base_ms, max_allowed)), "fixed-no-analysis"
    if not left_analysis.tail_rms_curve or not right_analysis.head_rms_curve:
        return max(min_ms, min(base_ms, max_allowed)), "fixed-rms-missing"

    candidates = [
        ms
        for ms in range(min_ms, min(max_ms, max_allowed) + 1, 250)
        if ms <= max_allowed
    ]
    if base_ms not in candidates and min_ms <= base_ms <= max_allowed:
        candidates.append(base_ms)

    best_ms = max(min_ms, min(base_ms, max_allowed))
    best_score = float("inf")
    for ms in sorted(set(candidates)):
        frames = max(1, int(ms / 50))
        tail = left_analysis.tail_rms_curve
        head = right_analysis.head_rms_curve
        tail_idx = max(0, len(tail) - frames - 1)
        head_idx = min(len(head) - 1, frames - 1)
        score = tail[tail_idx] + head[head_idx]
        score += 0.15 * abs(ms - base_ms) / max(base_ms, 1)
        if score < best_score:
            best_score = score
            best_ms = ms

    left_bpm = left_analysis.bpm
    right_bpm = right_analysis.bpm
    left_conf = left_analysis.bpm_confidence or 0.0
    right_conf = right_analysis.bpm_confidence or 0.0
    if left_bpm and right_bpm and left_conf >= 0.35 and right_conf >= 0.35:
        bpm = (left_bpm + right_bpm) / 2.0
        beat_ms = 60_000.0 / bpm
        options = [int(round(2 * beat_ms)), int(round(4 * beat_ms))]
        nearest = min(options, key=lambda x: abs(x - best_ms))
        if abs(nearest - best_ms) <= 750:
            best_ms = max(min_ms, min(nearest, max_allowed))
            return best_ms, "smart-rms+bpm"
    return best_ms, "smart-rms"


def build_mix_plan(
    instances: list[TrackInstance],
    analyses: dict[str, TrackAnalysis],
    crossfade_sec: float,
    smart_crossfade: bool,
    target_duration_min: Optional[int],
) -> MixPlan:
    if not instances:
        raise ValueError("cannot build mix plan with no track instances")
    base_ms = int(crossfade_sec * 1000)
    transitions: list[TransitionPlan] = []

    for idx in range(len(instances) - 1):
        left = instances[idx]
        right = instances[idx + 1]
        left_analysis = analyses.get(left.track.id)
        right_analysis = analyses.get(right.track.id)
        if smart_crossfade:
            crossfade_ms, reason = _smart_crossfade_ms(
                left.track,
                right.track,
                left_analysis=left_analysis,
                right_analysis=right_analysis,
                base_ms=base_ms,
            )
            smart_used = reason.startswith("smart")
        else:
            min_ms, max_allowed = _crossfade_bounds(left.track, right.track)
            crossfade_ms = max(min_ms, min(base_ms, max_allowed))
            reason = "fixed"
            smart_used = False

        dist = key_distance(left_analysis.key if left_analysis else None, right_analysis.key if right_analysis else None)
        lpf_duck_ms: Optional[int] = None
        if (
            dist is not None
            and left_analysis is not None
            and right_analysis is not None
            and (left_analysis.key_confidence or 0.0) >= 0.4
            and (right_analysis.key_confidence or 0.0) >= 0.4
            and dist >= 5
        ):
            min_ms, max_allowed = _crossfade_bounds(left.track, right.track)
            crossfade_ms = min(crossfade_ms + 500, max(min_ms, max_allowed))
            lpf_duck_ms = 1200
            reason = f"{reason}+key-mask"

        transitions.append(
            TransitionPlan(
                from_track_id=left.track.id,
                to_track_id=right.track.id,
                crossfade_ms=crossfade_ms,
                smart_used=smart_used,
                reason=reason,
                key_distance=dist,
                lpf_duck_ms=lpf_duck_ms,
            )
        )

    timeline: list[TimelineEntry] = []
    cursor_ms = 0
    for idx, instance in enumerate(instances):
        if idx > 0:
            cursor_ms -= transitions[idx - 1].crossfade_ms
        start = max(0, cursor_ms)
        end = start + instance.track.duration_ms
        analysis = analyses.get(instance.track.id)
        timeline.append(
            TimelineEntry(
                instance_index=instance.instance_index,
                track_id=instance.track.id,
                filename=instance.track.path.name,
                start_time_ms=start,
                end_time_ms=end,
                cycle_index=instance.cycle_index,
                analysis_snapshot={
                    "bpm": analysis.bpm if analysis else None,
                    "key": analysis.key if analysis else None,
                    "key_confidence": analysis.key_confidence if analysis else None,
                    "loudness_i": analysis.loudness.input_i if analysis else None,
                },
            )
        )
        cursor_ms = end

    estimated_duration = timeline[-1].end_time_ms
    target_reached = True
    if target_duration_min is not None:
        target_reached = estimated_duration >= target_duration_min * 60_000

    return MixPlan(
        instances=instances,
        timeline=timeline,
        transitions=transitions,
        estimated_duration_ms=estimated_duration,
        target_reached=target_reached,
    )
