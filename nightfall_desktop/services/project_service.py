from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Optional, TypeVar

from nightfall_mix.config import OutputFormat, PresetName, QualityMode, SmartOrderingMode
from nightfall_desktop.models.session_models import GuiSettings, PresetOverrides, WorkspaceMode

SUPPORTED_PROJECT_VERSION = 1
E = TypeVar("E", bound=Enum)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_enum(enum_cls: type[E], raw_value: object, default: E) -> E:
    if raw_value is None:
        return default
    try:
        return enum_cls(raw_value)
    except ValueError:
        return default


def _serialize_override(override: PresetOverrides) -> dict[str, Optional[float]]:
    return {
        "lpf_hz": override.lpf_hz,
        "saturation_scale": override.saturation_scale,
        "compression_scale": override.compression_scale,
    }


def _deserialize_override(payload: object) -> PresetOverrides:
    if not isinstance(payload, dict):
        payload = {}
    lpf_override = _as_optional_float(payload.get("lpf_hz"))
    if lpf_override is not None:
        lpf_override = _clamp(lpf_override, 4000.0, 18000.0)
    return PresetOverrides(
        lpf_hz=lpf_override,
        saturation_scale=_clamp(
            _as_float(payload.get("saturation_scale", 1.0), 1.0),
            0.3,
            1.5,
        ),
        compression_scale=_clamp(
            _as_float(payload.get("compression_scale", 1.0), 1.0),
            0.3,
            1.5,
        ),
    )


def save_project_file(path: Path, settings: GuiSettings, ordered_paths: list[Path]) -> None:
    overrides_by_name = settings.preset_overrides_by_name or {
        settings.preset: settings.preset_overrides,
    }
    payload = {
        "version": SUPPORTED_PROJECT_VERSION,
        "settings": {
            "songs_folder": str(settings.songs_folder),
            "output_path": str(settings.output_path),
            "cache_folder": str(settings.cache_folder) if settings.cache_folder else None,
            "rain_path": str(settings.rain_path) if settings.rain_path else None,
            "preset": settings.preset.value,
            "quality_mode": settings.quality_mode.value,
            "output_format": settings.output_format.value,
            "bitrate": settings.bitrate,
            "output_chunks_enabled": settings.output_chunks_enabled,
            "output_chunk_minutes": settings.output_chunk_minutes,
            "adaptive_lofi": settings.adaptive_lofi,
            "adaptive_report": str(settings.adaptive_report),
            "adaptive_lpf_max_cut_hz": settings.adaptive_lpf_max_cut_hz,
            "adaptive_noise_max_db": settings.adaptive_noise_max_db,
            "adaptive_stereo_min_width": settings.adaptive_stereo_min_width,
            "adaptive_centroid_threshold": settings.adaptive_centroid_threshold,
            "adaptive_rolloff_threshold": settings.adaptive_rolloff_threshold,
            "adaptive_crest_threshold_low": settings.adaptive_crest_threshold_low,
            "adaptive_crest_threshold_high": settings.adaptive_crest_threshold_high,
            "rain_level_db": settings.rain_level_db,
            "crossfade_sec": settings.crossfade_sec,
            "lufs": settings.lufs,
            "shuffle": settings.shuffle,
            "seed": settings.seed,
            "target_duration_min": settings.target_duration_min,
            "smart_crossfade": settings.smart_crossfade,
            "smart_ordering": settings.smart_ordering,
            "smart_ordering_mode": settings.smart_ordering_mode.value,
            "preview_mode": settings.preview_mode,
            "preview_duration_sec": settings.preview_duration_sec,
            "workspace_mode": settings.workspace_mode.value,
            "metadata_tags": dict(settings.metadata_tags),
            "metadata_json": str(settings.metadata_json) if settings.metadata_json else None,
            "mix_log": str(settings.mix_log) if settings.mix_log else None,
            "strict_analysis": settings.strict_analysis,
            "preset_overrides": _serialize_override(settings.preset_overrides),
            "preset_overrides_by_name": {
                preset.value: _serialize_override(override)
                for preset, override in overrides_by_name.items()
            },
        },
        "ordered_paths": [str(p) for p in ordered_paths],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def load_project_file(path: Path) -> tuple[GuiSettings, list[Path]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("version", SUPPORTED_PROJECT_VERSION)
    if version != SUPPORTED_PROJECT_VERSION:
        raise ValueError(
            f"Unsupported project file version {version}; expected {SUPPORTED_PROJECT_VERSION}"
        )

    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("Invalid project file: missing settings object")
    if "songs_folder" not in settings or "output_path" not in settings:
        raise ValueError("Invalid project file: songs_folder and output_path are required")

    loaded_preset = _parse_enum(PresetName, settings.get("preset"), PresetName.tokyo_cassette)
    legacy_override = _deserialize_override(settings.get("preset_overrides", {}))
    raw_overrides_by_name = settings.get("preset_overrides_by_name", {})
    parsed_overrides_by_name: dict[PresetName, PresetOverrides] = {}
    if isinstance(raw_overrides_by_name, dict):
        for preset_name, override_payload in raw_overrides_by_name.items():
            try:
                preset = PresetName(preset_name)
            except ValueError:
                continue
            parsed_overrides_by_name[preset] = _deserialize_override(override_payload)
    if not parsed_overrides_by_name:
        parsed_overrides_by_name[loaded_preset] = legacy_override

    loaded = GuiSettings(
        songs_folder=Path(settings["songs_folder"]),
        output_path=Path(settings["output_path"]),
        cache_folder=Path(settings["cache_folder"]) if settings.get("cache_folder") else None,
        rain_path=Path(settings["rain_path"]) if settings.get("rain_path") else None,
        preset=loaded_preset,
        quality_mode=_parse_enum(QualityMode, settings.get("quality_mode"), QualityMode.best),
        output_format=_parse_enum(OutputFormat, settings.get("output_format"), OutputFormat.mp3),
        bitrate=settings.get("bitrate", "192k"),
        output_chunks_enabled=_as_bool(settings.get("output_chunks_enabled", False), False),
        output_chunk_minutes=max(1, _as_optional_int(settings.get("output_chunk_minutes")) or 10),
        adaptive_lofi=_as_bool(settings.get("adaptive_lofi", False), False),
        adaptive_report=Path(settings.get("adaptive_report", "adaptive_report.json")),
        adaptive_lpf_max_cut_hz=_as_float(settings.get("adaptive_lpf_max_cut_hz", 2000.0), 2000.0),
        adaptive_noise_max_db=_as_float(settings.get("adaptive_noise_max_db", -30.0), -30.0),
        adaptive_stereo_min_width=_as_float(settings.get("adaptive_stereo_min_width", 0.75), 0.75),
        adaptive_centroid_threshold=_as_float(settings.get("adaptive_centroid_threshold", 3200.0), 3200.0),
        adaptive_rolloff_threshold=_as_float(settings.get("adaptive_rolloff_threshold", 10000.0), 10000.0),
        adaptive_crest_threshold_low=_as_float(settings.get("adaptive_crest_threshold_low", 8.0), 8.0),
        adaptive_crest_threshold_high=_as_float(settings.get("adaptive_crest_threshold_high", 14.0), 14.0),
        rain_level_db=_as_float(settings.get("rain_level_db", -28.0), -28.0),
        crossfade_sec=_as_float(settings.get("crossfade_sec", 6.0), 6.0),
        lufs=_as_float(settings.get("lufs", -14.0), -14.0),
        shuffle=_as_bool(settings.get("shuffle", False), False),
        seed=settings.get("seed"),
        target_duration_min=_as_optional_int(settings.get("target_duration_min")),
        smart_crossfade=_as_bool(settings.get("smart_crossfade", True), True),
        smart_ordering=_as_bool(settings.get("smart_ordering", False), False),
        smart_ordering_mode=_parse_enum(
            SmartOrderingMode,
            settings.get("smart_ordering_mode"),
            SmartOrderingMode.bpm_key_balanced,
        ),
        preview_mode=_as_bool(settings.get("preview_mode", False), False),
        preview_duration_sec=_as_float(settings.get("preview_duration_sec", 60.0), 60.0),
        workspace_mode=_parse_enum(
            WorkspaceMode,
            settings.get("workspace_mode"),
            WorkspaceMode.advanced,
        ),
        metadata_tags={
            str(k): str(v)
            for k, v in (settings.get("metadata_tags") or {}).items()
            if str(k).strip() and str(v).strip()
        }
        if isinstance(settings.get("metadata_tags"), dict)
        else {},
        metadata_json=Path(settings["metadata_json"]) if settings.get("metadata_json") else None,
        mix_log=Path(settings["mix_log"]) if settings.get("mix_log") else None,
        strict_analysis=_as_bool(settings.get("strict_analysis", False), False),
        preset_overrides=parsed_overrides_by_name.get(loaded_preset, legacy_override),
        preset_overrides_by_name=parsed_overrides_by_name,
    )
    ordered_payload = payload.get("ordered_paths", [])
    if not isinstance(ordered_payload, list):
        ordered_payload = []
    ordered = [Path(p) for p in ordered_payload if isinstance(p, str) and p]
    return loaded, ordered
