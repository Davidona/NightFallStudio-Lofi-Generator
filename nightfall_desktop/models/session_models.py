from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.config import OutputFormat, PresetName, QualityMode, RainPresence, SmartOrderingMode
from nightfall_mix.mixer import MixPlan, TrackSource


@dataclass
class PresetOverrides:
    lpf_hz: Optional[float] = None
    hpf_hz: Optional[float] = None
    lpf_q: Optional[float] = None
    saturation_scale: Optional[float] = None
    tape_drive: Optional[float] = None
    tape_bias: Optional[float] = None
    compression_scale: Optional[float] = None
    comp_attack_ms: Optional[float] = None
    comp_release_ms: Optional[float] = None
    comp_ratio: Optional[float] = None
    bit_depth: Optional[int] = None
    sample_rate_reduction_hz: Optional[float] = None
    wow_depth: Optional[float] = None
    wow_rate_hz: Optional[float] = None
    flutter_depth: Optional[float] = None
    flutter_rate_hz: Optional[float] = None
    stereo_width: Optional[float] = None
    vinyl_noise_level_db: Optional[float] = None
    tape_hiss_level_db: Optional[float] = None
    atmosphere_volume_db: Optional[float] = None
    atmosphere_stereo_width: Optional[float] = None
    atmosphere_lpf_hz: Optional[float] = None


class WorkspaceMode(str, Enum):
    simple = "simple"
    advanced = "advanced"


@dataclass
class GuiSettings:
    songs_folder: Path
    output_path: Path
    cache_folder: Optional[Path] = None
    rain_path: Optional[Path] = None
    preset: PresetName = PresetName.tokyo_cassette
    quality_mode: QualityMode = QualityMode.best
    output_format: OutputFormat = OutputFormat.mp3
    bitrate: str = "192k"
    output_chunks_enabled: bool = False
    output_chunk_minutes: int = 10
    adaptive_lofi: bool = False
    adaptive_report: Path = Path("adaptive_report.json")
    adaptive_lpf_max_cut_hz: float = 2000.0
    adaptive_noise_max_db: float = -30.0
    adaptive_stereo_min_width: float = 0.75
    adaptive_centroid_threshold: float = 3200.0
    adaptive_rolloff_threshold: float = 10000.0
    adaptive_crest_threshold_low: float = 8.0
    adaptive_crest_threshold_high: float = 14.0
    rain_level_db: float = -28.0
    rain_presence: RainPresence = RainPresence.balanced
    rain_preserve_low_drops: bool = True
    crossfade_sec: float = 6.0
    lufs: float = -14.0
    shuffle: bool = False
    seed: Optional[int] = 123
    target_duration_min: Optional[int] = None
    smart_crossfade: bool = True
    smart_ordering: bool = False
    smart_ordering_mode: SmartOrderingMode = SmartOrderingMode.bpm_key_balanced
    preview_mode: bool = False
    preview_duration_sec: float = 60.0
    workspace_mode: WorkspaceMode = WorkspaceMode.advanced
    metadata_tags: dict[str, str] = field(default_factory=dict)
    metadata_json: Optional[Path] = None
    mix_log: Optional[Path] = None
    strict_analysis: bool = False
    preset_overrides: PresetOverrides = field(default_factory=PresetOverrides)
    preset_overrides_by_name: dict[PresetName, PresetOverrides] = field(default_factory=dict)


@dataclass
class TrackRowModel:
    track_id: str
    path: Path
    duration_ms: int
    bpm: Optional[float]
    key: Optional[str]
    adaptive_score: Optional[float]


@dataclass
class EngineSessionModel:
    settings: GuiSettings
    ordered_paths: list[Path]
    track_sources: list[TrackSource]
    analyses: dict[str, TrackAnalysis]
    mix_plan: MixPlan
    warnings: list[str] = field(default_factory=list)


@dataclass
class RenderArtifactsModel:
    output_audio_path: Path
    tracklist_txt_path: Path
    tracklist_json_path: Path
    timestamps_txt_path: Path
    timestamps_csv_path: Path
    adaptive_report_path: Optional[Path] = None
    processing_tracklist_path: Optional[Path] = None
    metadata_json_path: Optional[Path] = None
    chunk_output_paths: list[Path] = field(default_factory=list)
