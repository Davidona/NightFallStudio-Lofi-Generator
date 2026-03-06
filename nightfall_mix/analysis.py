from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from nightfall_mix.config import RunConfig
from nightfall_mix.effects_presets import PresetSpec
from nightfall_mix.utils import CommandError, parse_loudnorm_json, run_command, run_command_binary

try:
    import librosa

    LIBROSA_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    librosa = None
    LIBROSA_AVAILABLE = False

EPS = 1e-12
ANALYSIS_SIDECAR_VERSION = 1


@dataclass
class LoudnessStats:
    input_i: Optional[float] = None
    input_tp: Optional[float] = None
    input_lra: Optional[float] = None
    input_thresh: Optional[float] = None
    target_offset: Optional[float] = None
    recommended_gain_db: Optional[float] = None
    measured: dict[str, float] = field(default_factory=dict)


@dataclass
class AdaptiveMetrics:
    lufs: Optional[float] = None
    rms_dbfs: Optional[float] = None
    crest_factor_db: Optional[float] = None
    spectral_centroid_hz: Optional[float] = None
    rolloff_hz: Optional[float] = None
    stereo_width: Optional[float] = None
    noise_floor_dbfs: Optional[float] = None


@dataclass
class AdaptiveProcessing:
    lpf_cutoff_hz: Optional[float]
    saturation_strength: float
    compression_strength: float
    stereo_width_target: float
    noise_added_db: Optional[float]
    lofi_needed_score: float
    rationale: str
    used_fallback: bool = False


@dataclass
class TrackAnalysis:
    track_id: str
    bpm: Optional[float] = None
    bpm_confidence: Optional[float] = None
    key: Optional[str] = None
    key_confidence: Optional[float] = None
    tail_rms_curve: list[float] = field(default_factory=list)
    head_rms_curve: list[float] = field(default_factory=list)
    loudness: LoudnessStats = field(default_factory=LoudnessStats)
    adaptive_metrics: Optional[AdaptiveMetrics] = None
    adaptive_processing: Optional[AdaptiveProcessing] = None
    warnings: list[str] = field(default_factory=list)


MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _null_sink() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def _to_db(value: float) -> float:
    return 20.0 * math.log10(max(EPS, value))


def measure_loudness(path: Path, target_lufs: float, logger: logging.Logger) -> LoudnessStats:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "info",
        "-i",
        str(path),
        "-af",
        f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11:print_format=json",
        "-f",
        "null",
        _null_sink(),
    ]
    try:
        result = run_command(cmd, logger=logger)
    except CommandError as exc:
        raise RuntimeError(f"loudness measurement failed for {path}: {exc}") from exc

    measured = parse_loudnorm_json(result.stderr or "")
    stats = LoudnessStats()
    if measured:
        stats.measured = measured
        stats.input_i = measured.get("input_i")
        stats.input_tp = measured.get("input_tp")
        stats.input_lra = measured.get("input_lra")
        stats.input_thresh = measured.get("input_thresh")
        stats.target_offset = measured.get("target_offset")
        if stats.input_i is not None:
            stats.recommended_gain_db = target_lufs - stats.input_i
    return stats


def _rms_curve(y: np.ndarray, sr: int, hop_sec: float = 0.05) -> list[float]:
    hop = max(128, int(sr * hop_sec))
    frame = max(hop * 2, 2048)
    if len(y) < frame:
        y = np.pad(y, (0, frame - len(y)))
    rms = librosa.feature.rms(y=y, frame_length=frame, hop_length=hop).flatten()
    return [float(v) for v in rms]


def _detect_key(y: np.ndarray, sr: int) -> tuple[Optional[str], Optional[float]]:
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    profile = chroma.mean(axis=1)
    if np.allclose(profile, 0):
        return None, None
    best_key: Optional[str] = None
    best_score = -1.0
    second_best = -1.0
    for i in range(12):
        major_score = np.corrcoef(profile, np.roll(MAJOR_PROFILE, i))[0, 1]
        minor_score = np.corrcoef(profile, np.roll(MINOR_PROFILE, i))[0, 1]
        for mode, score in (("maj", major_score), ("min", minor_score)):
            if np.isnan(score):
                continue
            if score > best_score:
                second_best = best_score
                best_score = float(score)
                best_key = f"{KEY_NAMES[i]}:{mode}"
            elif score > second_best:
                second_best = float(score)
    if best_key is None:
        return None, None
    confidence = max(0.0, min(1.0, (best_score - second_best + 0.1) / 1.1))
    return best_key, confidence


def _normalize_bpm_for_lofi(tempo: float) -> float:
    # Beat tracking often flips by x2/x0.5 for chill genres; fold tempo to a stable range.
    normalized = float(tempo)
    while normalized > 120.0:
        normalized /= 2.0
    while normalized < 60.0:
        normalized *= 2.0
    return normalized


def _detect_bpm(y: np.ndarray, sr: int) -> tuple[Optional[float], Optional[float]]:
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    if tempo is None or np.isnan(float(tempo)):
        return None, None
    duration_min = max(len(y) / sr / 60.0, 1e-6)
    beat_density = len(beats) / duration_min
    confidence = max(0.0, min(1.0, beat_density / 180.0))
    return _normalize_bpm_for_lofi(float(tempo)), float(confidence)


def _adaptive_sidecar_path(track_path: Path) -> Path:
    return track_path.with_name(f"{track_path.name}.nightfall_adaptive.json")


def _analysis_sidecar_path(track_path: Path) -> Path:
    return track_path.with_name(f"{track_path.name}.nightfall_analysis.json")


def _source_signature(track_path: Path) -> dict[str, int]:
    stat = track_path.stat()
    return {
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
    }


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _coerce_float_list(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    out: list[float] = []
    for value in values:
        num = _coerce_optional_float(value)
        if num is not None:
            out.append(num)
    return out


def _has_loudness_payload(loudness_payload: dict[str, Any]) -> bool:
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if _coerce_optional_float(loudness_payload.get(key)) is not None:
            return True
    measured = loudness_payload.get("measured")
    if isinstance(measured, dict) and measured:
        return True
    return False


def _load_analysis_sidecar(
    track_id: str,
    path: Path,
    target_lufs: float,
) -> Optional[tuple[TrackAnalysis, bool, bool, bool]]:
    sidecar = _analysis_sidecar_path(path)
    if not sidecar.exists():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None

    if payload.get("version") != ANALYSIS_SIDECAR_VERSION:
        return None

    source = payload.get("source", {})
    signature = _source_signature(path)
    if source.get("mtime_ns") != signature["mtime_ns"] or source.get("size") != signature["size"]:
        return None

    computed = payload.get("computed", {})
    loudness_done = bool(computed.get("loudness"))
    bpm_key_done = bool(computed.get("bpm_key"))
    rms_edges_done = bool(computed.get("rms_edges"))

    analysis_payload = payload.get("analysis", {})
    analysis = TrackAnalysis(track_id=track_id)
    analysis.bpm = _coerce_optional_float(analysis_payload.get("bpm"))
    analysis.bpm_confidence = _coerce_optional_float(analysis_payload.get("bpm_confidence"))
    analysis.key = analysis_payload.get("key") if isinstance(analysis_payload.get("key"), str) else None
    analysis.key_confidence = _coerce_optional_float(analysis_payload.get("key_confidence"))
    analysis.head_rms_curve = _coerce_float_list(analysis_payload.get("head_rms_curve"))
    analysis.tail_rms_curve = _coerce_float_list(analysis_payload.get("tail_rms_curve"))

    loudness_payload = analysis_payload.get("loudness", {})
    if isinstance(loudness_payload, dict):
        measured_payload = loudness_payload.get("measured")
        measured: dict[str, float] = {}
        if isinstance(measured_payload, dict):
            for key, value in measured_payload.items():
                num = _coerce_optional_float(value)
                if num is not None:
                    measured[key] = num
        input_i = _coerce_optional_float(loudness_payload.get("input_i"))
        analysis.loudness = LoudnessStats(
            input_i=input_i,
            input_tp=_coerce_optional_float(loudness_payload.get("input_tp")),
            input_lra=_coerce_optional_float(loudness_payload.get("input_lra")),
            input_thresh=_coerce_optional_float(loudness_payload.get("input_thresh")),
            target_offset=_coerce_optional_float(loudness_payload.get("target_offset")),
            measured=measured,
            recommended_gain_db=(target_lufs - input_i) if input_i is not None else None,
        )
        if loudness_done and not _has_loudness_payload(loudness_payload):
            loudness_done = False
    else:
        loudness_done = False

    warnings_payload = analysis_payload.get("warnings")
    if isinstance(warnings_payload, list):
        analysis.warnings = [str(item) for item in warnings_payload if isinstance(item, str)]

    return analysis, loudness_done, bpm_key_done, rms_edges_done


def _save_analysis_sidecar(
    path: Path,
    analysis: TrackAnalysis,
    loudness_done: bool,
    bpm_key_done: bool,
    rms_edges_done: bool,
) -> None:
    sidecar = _analysis_sidecar_path(path)
    payload = {
        "version": ANALYSIS_SIDECAR_VERSION,
        "source": _source_signature(path),
        "computed": {
            "loudness": bool(loudness_done),
            "bpm_key": bool(bpm_key_done),
            "rms_edges": bool(rms_edges_done),
        },
        "analysis": {
            "bpm": analysis.bpm,
            "bpm_confidence": analysis.bpm_confidence,
            "key": analysis.key,
            "key_confidence": analysis.key_confidence,
            "head_rms_curve": analysis.head_rms_curve,
            "tail_rms_curve": analysis.tail_rms_curve,
            "loudness": {
                "input_i": analysis.loudness.input_i,
                "input_tp": analysis.loudness.input_tp,
                "input_lra": analysis.loudness.input_lra,
                "input_thresh": analysis.loudness.input_thresh,
                "target_offset": analysis.loudness.target_offset,
                "measured": analysis.loudness.measured,
            },
            "warnings": analysis.warnings,
        },
    }
    sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_analysis_cache_summary(path: Path) -> Optional[dict[str, Any]]:
    has_adaptive_metrics = _load_adaptive_sidecar(path) is not None
    sidecar = _analysis_sidecar_path(path)
    if not sidecar.exists():
        if has_adaptive_metrics:
            return {
                "bpm": None,
                "key": None,
                "has_loudness": False,
                "has_bpm_key": False,
                "has_rms_edges": False,
                "has_adaptive_metrics": True,
            }
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        if has_adaptive_metrics:
            return {
                "bpm": None,
                "key": None,
                "has_loudness": False,
                "has_bpm_key": False,
                "has_rms_edges": False,
                "has_adaptive_metrics": True,
            }
        return None

    source = payload.get("source", {})
    signature = _source_signature(path)
    if source.get("mtime_ns") != signature["mtime_ns"] or source.get("size") != signature["size"]:
        if has_adaptive_metrics:
            return {
                "bpm": None,
                "key": None,
                "has_loudness": False,
                "has_bpm_key": False,
                "has_rms_edges": False,
                "has_adaptive_metrics": True,
            }
        return None

    analysis_payload = payload.get("analysis", {})
    computed = payload.get("computed", {})
    if not isinstance(analysis_payload, dict) or not isinstance(computed, dict):
        if has_adaptive_metrics:
            return {
                "bpm": None,
                "key": None,
                "has_loudness": False,
                "has_bpm_key": False,
                "has_rms_edges": False,
                "has_adaptive_metrics": True,
            }
        return None
    return {
        "bpm": _coerce_optional_float(analysis_payload.get("bpm")),
        "key": analysis_payload.get("key") if isinstance(analysis_payload.get("key"), str) else None,
        "has_loudness": bool(computed.get("loudness")),
        "has_bpm_key": bool(computed.get("bpm_key")),
        "has_rms_edges": bool(computed.get("rms_edges")),
        "has_adaptive_metrics": has_adaptive_metrics,
    }


def _load_adaptive_sidecar(track_path: Path) -> Optional[AdaptiveMetrics]:
    sidecar = _adaptive_sidecar_path(track_path)
    if not sidecar.exists():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None
    source = payload.get("source", {})
    stat = track_path.stat()
    if source.get("mtime_ns") != stat.st_mtime_ns or source.get("size") != stat.st_size:
        return None
    metrics = payload.get("metrics", {})
    return AdaptiveMetrics(
        lufs=metrics.get("lufs"),
        rms_dbfs=metrics.get("rms_dbfs"),
        crest_factor_db=metrics.get("crest_factor_db"),
        spectral_centroid_hz=metrics.get("spectral_centroid_hz"),
        rolloff_hz=metrics.get("rolloff_hz"),
        stereo_width=metrics.get("stereo_width"),
        noise_floor_dbfs=metrics.get("noise_floor_dbfs"),
    )


def _save_adaptive_sidecar(track_path: Path, metrics: AdaptiveMetrics) -> None:
    sidecar = _adaptive_sidecar_path(track_path)
    stat = track_path.stat()
    payload = {
        "version": 1,
        "source": {
            "path": str(track_path),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        },
        "metrics": asdict(metrics),
    }
    sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _decode_pcm_window(
    path: Path,
    offset_sec: float,
    duration_sec: float,
    sample_rate: int,
    channels: int,
    logger: logging.Logger,
) -> np.ndarray:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        f"{offset_sec:.3f}",
        "-t",
        f"{duration_sec:.3f}",
        "-i",
        str(path),
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "pipe:1",
    ]
    result = run_command_binary(cmd, logger=logger)
    raw = result.stdout or b""
    if not raw:
        return np.empty((0, channels), dtype=np.float32)
    data = np.frombuffer(raw, dtype=np.float32)
    frame_count = (data.size // channels) * channels
    data = data[:frame_count]
    if frame_count == 0:
        return np.empty((0, channels), dtype=np.float32)
    return data.reshape(-1, channels)


def _analysis_offsets(duration_sec: float, window_sec: float = 30.0, max_windows: int = 6) -> list[float]:
    if duration_sec <= window_sec:
        return [0.0]
    windows = min(max_windows, max(2, int(math.ceil(duration_sec / window_sec))))
    upper = max(0.0, duration_sec - window_sec)
    return [float(x) for x in np.linspace(0.0, upper, num=windows)]


def _chunk_spectral_metrics(mono: np.ndarray, sample_rate: int) -> tuple[float, float]:
    if mono.size == 0:
        return 0.0, 0.0
    spec = np.abs(np.fft.rfft(mono))
    power = np.maximum(spec, EPS)
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / sample_rate)
    total = float(np.sum(power))
    if total <= EPS:
        return 0.0, 0.0
    centroid = float(np.sum(freqs * power) / total)
    cumulative = np.cumsum(power)
    rolloff_idx = int(np.searchsorted(cumulative, 0.85 * cumulative[-1], side="left"))
    rolloff_idx = min(max(0, rolloff_idx), len(freqs) - 1)
    rolloff = float(freqs[rolloff_idx])
    return centroid, rolloff


def _chunk_noise_floor_dbfs(mono: np.ndarray) -> float:
    frame = 2048
    hop = 1024
    if mono.size < frame:
        rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64) + EPS))
        return _to_db(rms)
    values: list[float] = []
    for start in range(0, mono.size - frame + 1, hop):
        window = mono[start : start + frame]
        rms = float(np.sqrt(np.mean(np.square(window), dtype=np.float64) + EPS))
        values.append(rms)
    if not values:
        return -120.0
    sorted_vals = np.array(sorted(values), dtype=np.float64)
    count = max(1, int(len(sorted_vals) * 0.2))
    low = float(np.mean(sorted_vals[:count]))
    return _to_db(low)


def analyze_adaptive_metrics(
    path: Path,
    duration_ms: int,
    loudness: LoudnessStats,
    logger: logging.Logger,
) -> AdaptiveMetrics:
    cached = _load_adaptive_sidecar(path)
    if cached is not None:
        return cached

    sample_rate = 22050
    channels = 2
    duration_sec = max(0.1, duration_ms / 1000.0)
    offsets = _analysis_offsets(duration_sec)

    crest_values: list[float] = []
    centroid_values: list[float] = []
    rolloff_values: list[float] = []
    stereo_width_values: list[float] = []
    noise_floor_values: list[float] = []
    rms_values: list[float] = []

    for offset in offsets:
        win = min(30.0, duration_sec - offset)
        if win <= 0:
            continue
        pcm = _decode_pcm_window(
            path=path,
            offset_sec=offset,
            duration_sec=win,
            sample_rate=sample_rate,
            channels=channels,
            logger=logger,
        )
        if pcm.size == 0:
            continue
        left = pcm[:, 0]
        right = pcm[:, 1] if pcm.shape[1] > 1 else pcm[:, 0]
        mono = 0.5 * (left + right)

        rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64) + EPS))
        peak = float(np.max(np.abs(mono)) + EPS)
        crest = _to_db(peak) - _to_db(rms)
        centroid, rolloff = _chunk_spectral_metrics(mono, sample_rate=sample_rate)
        mid = 0.5 * (left + right)
        side = 0.5 * (left - right)
        mid_energy = float(np.mean(np.square(mid), dtype=np.float64))
        side_energy = float(np.mean(np.square(side), dtype=np.float64))
        width = side_energy / (mid_energy + EPS)
        noise_floor = _chunk_noise_floor_dbfs(mono)

        rms_values.append(_to_db(rms))
        crest_values.append(crest)
        centroid_values.append(centroid)
        rolloff_values.append(rolloff)
        stereo_width_values.append(width)
        noise_floor_values.append(noise_floor)

    if not crest_values:
        raise RuntimeError(f"adaptive analysis failed: no decodable PCM for {path.name}")

    metrics = AdaptiveMetrics(
        lufs=(loudness.input_i if loudness.input_i is not None else float(np.median(rms_values))),
        rms_dbfs=float(np.median(rms_values)),
        crest_factor_db=float(np.median(crest_values)),
        spectral_centroid_hz=float(np.median(centroid_values)),
        rolloff_hz=float(np.median(rolloff_values)),
        stereo_width=float(np.median(stereo_width_values)),
        noise_floor_dbfs=float(np.median(noise_floor_values)),
    )
    try:
        _save_adaptive_sidecar(path, metrics)
    except Exception as exc:
        logger.warning("adaptive sidecar write failed for %s: %s", path.name, exc)
    return metrics


def _score_lofi_needed(metrics: AdaptiveMetrics, config: RunConfig) -> float:
    centroid = metrics.spectral_centroid_hz or config.adaptive_centroid_threshold
    crest = metrics.crest_factor_db or config.adaptive_crest_threshold_low
    width = metrics.stereo_width or 0.85
    noise_floor = metrics.noise_floor_dbfs if metrics.noise_floor_dbfs is not None else -45.0

    brightness = max(0.0, min(1.0, (centroid - config.adaptive_centroid_threshold) / 1800.0))
    crest_excess = max(
        0.0,
        min(
            1.0,
            (crest - config.adaptive_crest_threshold_low)
            / (config.adaptive_crest_threshold_high - config.adaptive_crest_threshold_low),
        ),
    )
    width_excess = max(0.0, min(1.0, (width - 0.9) / 0.4))
    noise_absence = max(0.0, min(1.0, ((-noise_floor) - 40.0) / 20.0))
    return round((brightness + crest_excess + width_excess + noise_absence) * 25.0, 2)


def derive_adaptive_processing(
    metrics: AdaptiveMetrics,
    preset: PresetSpec,
    config: RunConfig,
) -> AdaptiveProcessing:
    rolloff = metrics.rolloff_hz if metrics.rolloff_hz is not None else config.adaptive_rolloff_threshold
    centroid = metrics.spectral_centroid_hz if metrics.spectral_centroid_hz is not None else config.adaptive_centroid_threshold
    crest = metrics.crest_factor_db if metrics.crest_factor_db is not None else config.adaptive_crest_threshold_low
    stereo_width = metrics.stereo_width if metrics.stereo_width is not None else preset.stereo_width
    noise_floor = metrics.noise_floor_dbfs if metrics.noise_floor_dbfs is not None else -45.0

    notes: list[str] = []

    if rolloff < config.adaptive_rolloff_threshold:
        lpf_cutoff_hz: Optional[float] = None
        notes.append(f"LPF skipped (rolloff {rolloff:.0f}Hz)")
    else:
        target_lpf = max(8500.0, min(10000.0, rolloff - 1500.0))
        min_relative = preset.lpf_hz - config.adaptive_lpf_max_cut_hz
        target_lpf = max(target_lpf, min_relative)
        lpf_cutoff_hz = round(target_lpf, 1)
        notes.append(f"LPF {lpf_cutoff_hz:.0f}Hz")

    if centroid < config.adaptive_centroid_threshold:
        base_saturation = 0.30
    elif centroid <= 4500.0:
        base_saturation = 0.60
    else:
        base_saturation = 1.00

    if crest < config.adaptive_crest_threshold_low:
        base_compression = 0.50
        notes.append(f"compression reduced (crest {crest:.1f}dB)")
    elif crest <= config.adaptive_crest_threshold_high:
        base_compression = 1.00
        notes.append(f"compression moderate (crest {crest:.1f}dB)")
    else:
        base_compression = 1.20
        notes.append(f"compression increased (crest {crest:.1f}dB)")

    if stereo_width < 0.75:
        stereo_target = stereo_width
        notes.append(f"stereo unchanged ({stereo_width:.2f})")
    elif stereo_width <= 1.0:
        stereo_target = 0.85
        notes.append("stereo narrowed to 0.85")
    else:
        stereo_target = 0.80
        notes.append("stereo narrowed to 0.80")
    if stereo_width >= config.adaptive_stereo_min_width:
        stereo_target = max(stereo_target, config.adaptive_stereo_min_width)

    noise_added_db: Optional[float]
    if noise_floor > -40.0:
        noise_added_db = None
        notes.append(f"noise skipped (floor {noise_floor:.1f}dBFS)")
    elif -50.0 <= noise_floor <= -40.0:
        noise_added_db = min(config.adaptive_noise_max_db, -35.0)
        notes.append(f"subtle noise {noise_added_db:.1f}dB")
    else:
        noise_added_db = config.adaptive_noise_max_db
        notes.append(f"noise added {noise_added_db:.1f}dB")

    score = _score_lofi_needed(metrics, config=config)
    smooth = 0.85 + 0.30 * (score / 100.0)
    saturation_strength = max(0.2, min(1.2, base_saturation * smooth))
    compression_strength = max(0.45, min(1.2, base_compression * (0.95 + 0.10 * (score / 100.0))))
    if noise_added_db is not None and noise_floor < -50.0:
        noise_added_db = max(-60.0, noise_added_db - (1.0 - score / 100.0) * 6.0)

    return AdaptiveProcessing(
        lpf_cutoff_hz=lpf_cutoff_hz,
        saturation_strength=round(saturation_strength, 3),
        compression_strength=round(compression_strength, 3),
        stereo_width_target=round(stereo_target, 3),
        noise_added_db=round(noise_added_db, 2) if noise_added_db is not None else None,
        lofi_needed_score=score,
        rationale=", ".join(notes),
    )


def fallback_adaptive_processing(
    preset: PresetSpec,
    config: RunConfig,
    warning: str,
) -> AdaptiveProcessing:
    return AdaptiveProcessing(
        lpf_cutoff_hz=preset.lpf_hz,
        saturation_strength=1.0,
        compression_strength=1.0,
        stereo_width_target=max(config.adaptive_stereo_min_width, preset.stereo_width),
        noise_added_db=(preset.noise_level_db if preset.noise_level_db > -90.0 else None),
        lofi_needed_score=50.0,
        rationale=f"adaptive fallback: {warning}",
        used_fallback=True,
    )


def analyze_track(
    track_id: str,
    path: Path,
    duration_ms: int,
    target_lufs: float,
    smart_crossfade: bool,
    smart_ordering: bool,
    logger: logging.Logger,
) -> TrackAnalysis:
    need_bpm_key = smart_crossfade or smart_ordering
    need_rms_edges = smart_crossfade

    cached = _load_analysis_sidecar(track_id=track_id, path=path, target_lufs=target_lufs)
    if cached is not None:
        analysis, loudness_done, bpm_key_done, rms_edges_done = cached
    else:
        analysis = TrackAnalysis(track_id=track_id)
        loudness_done = False
        bpm_key_done = False
        rms_edges_done = False

    if not loudness_done:
        try:
            analysis.loudness = measure_loudness(path, target_lufs=target_lufs, logger=logger)
            loudness_done = True
        except Exception as exc:
            analysis.warnings.append(str(exc))

    if not LIBROSA_AVAILABLE:
        if need_bpm_key or need_rms_edges:
            analysis.warnings.append("librosa unavailable; smart analysis disabled")
        try:
            _save_analysis_sidecar(
                path=path,
                analysis=analysis,
                loudness_done=loudness_done,
                bpm_key_done=bpm_key_done,
                rms_edges_done=rms_edges_done,
            )
        except Exception:
            pass
        return analysis

    if not need_bpm_key and not need_rms_edges:
        try:
            _save_analysis_sidecar(
                path=path,
                analysis=analysis,
                loudness_done=loudness_done,
                bpm_key_done=bpm_key_done,
                rms_edges_done=rms_edges_done,
            )
        except Exception:
            pass
        return analysis

    sr = 22050
    if need_rms_edges and not rms_edges_done:
        try:
            edge_sec = min(15.0, max(1.0, duration_ms / 1000.0))
            y_head, _ = librosa.load(path.as_posix(), sr=sr, mono=True, duration=edge_sec)
            analysis.head_rms_curve = _rms_curve(y_head, sr=sr)

            offset_sec = max(0.0, duration_ms / 1000.0 - edge_sec)
            y_tail, _ = librosa.load(path.as_posix(), sr=sr, mono=True, offset=offset_sec, duration=edge_sec)
            analysis.tail_rms_curve = _rms_curve(y_tail, sr=sr)
            rms_edges_done = True
        except Exception as exc:
            analysis.warnings.append(f"rms edge analysis failed for {path.name}: {exc}")

    if need_bpm_key and not bpm_key_done:
        try:
            preview_sec = min(120.0, duration_ms / 1000.0)
            y_preview, _ = librosa.load(path.as_posix(), sr=sr, mono=True, duration=preview_sec)
            bpm, bpm_conf = _detect_bpm(y_preview, sr=sr)
            analysis.bpm = bpm
            analysis.bpm_confidence = bpm_conf
            key, key_conf = _detect_key(y_preview, sr=sr)
            analysis.key = key
            analysis.key_confidence = key_conf
            bpm_key_done = True
        except Exception as exc:
            analysis.warnings.append(f"bpm/key analysis failed for {path.name}: {exc}")

    if analysis.bpm is not None and analysis.bpm <= 0:
        analysis.bpm = None
        analysis.bpm_confidence = None
    if analysis.key_confidence is not None and math.isnan(analysis.key_confidence):
        analysis.key = None
        analysis.key_confidence = None

    try:
        _save_analysis_sidecar(
            path=path,
            analysis=analysis,
            loudness_done=loudness_done,
            bpm_key_done=bpm_key_done,
            rms_edges_done=rms_edges_done,
        )
    except Exception as exc:
        logger.warning("analysis sidecar write failed for %s: %s", path.name, exc)

    return analysis
