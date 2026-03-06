from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderMode(str, Enum):
    alpha = "alpha"
    random = "random"
    m3u = "m3u"


class QualityMode(str, Enum):
    fast = "fast"
    balanced = "balanced"
    best = "best"


class SmartOrderingMode(str, Enum):
    bpm_first = "bpm_first"
    bpm_key_balanced = "bpm_key_balanced"


class PresetName(str, Enum):
    tokyo_cassette = "tokyo_cassette"
    vinyl_room = "vinyl_room"
    cleaner_lofi = "cleaner_lofi"
    night_owl_fm = "night_owl_fm"
    rainy_microcassette = "rainy_microcassette"
    velvet_room = "velvet_room"
    sunrise_clean = "sunrise_clean"


class OutputFormat(str, Enum):
    auto = "auto"
    mp3 = "mp3"
    wav = "wav"


class RunConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    songs_folder: Path
    output: Path
    rain: Optional[Path] = None
    order: OrderMode = OrderMode.alpha
    seed: Optional[int] = None
    target_duration_min: Optional[int] = None
    crossfade_sec: float = 6.0
    smart_crossfade: bool = False
    smart_ordering: bool = False
    smart_ordering_mode: SmartOrderingMode = SmartOrderingMode.bpm_key_balanced
    lufs: float = -14.0
    preset: PresetName = PresetName.tokyo_cassette
    metadata_tags: dict[str, str] = Field(default_factory=dict)
    metadata_json: Optional[Path] = None
    quality_mode: QualityMode = QualityMode.best
    rain_level_db: float = -28.0
    mix_log: Optional[Path] = None
    enable_warp: bool = False
    output_format: OutputFormat = OutputFormat.auto
    bitrate: str = "192k"
    strict_analysis: bool = False
    chunk_threshold: int = Field(default=180, ge=50)
    adaptive_lofi: bool = False
    adaptive_report: Path = Path("adaptive_report.json")
    adaptive_lpf_max_cut_hz: float = 2000.0
    adaptive_noise_max_db: float = -30.0
    adaptive_stereo_min_width: float = 0.75
    adaptive_centroid_threshold: float = 3200.0
    adaptive_rolloff_threshold: float = 10000.0
    adaptive_crest_threshold_low: float = 8.0
    adaptive_crest_threshold_high: float = 14.0

    @field_validator("songs_folder")
    @classmethod
    def validate_songs_folder(cls, value: Path) -> Path:
        if not value.exists() or not value.is_dir():
            raise ValueError(f"songs folder does not exist or is not a directory: {value}")
        return value

    @field_validator("rain")
    @classmethod
    def validate_rain(cls, value: Optional[Path]) -> Optional[Path]:
        if value is not None and (not value.exists() or not value.is_file()):
            raise ValueError(f"rain file does not exist: {value}")
        return value

    @field_validator("crossfade_sec")
    @classmethod
    def validate_crossfade(cls, value: float) -> float:
        if not 0.5 <= value <= 30.0:
            raise ValueError("crossfade must be between 0.5 and 30 seconds")
        return value

    @field_validator("lufs")
    @classmethod
    def validate_lufs(cls, value: float) -> float:
        if not -30.0 <= value <= -5.0:
            raise ValueError("LUFS target must be between -30 and -5")
        return value

    @field_validator("rain_level_db")
    @classmethod
    def validate_rain_level(cls, value: float) -> float:
        if not -60.0 <= value <= -5.0:
            raise ValueError("rain level must be between -60 and -5 dB")
        return value

    @field_validator("target_duration_min")
    @classmethod
    def validate_target_duration_min(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value <= 0:
            raise ValueError("target-duration-min must be > 0")
        return value

    @field_validator("bitrate")
    @classmethod
    def validate_bitrate(cls, value: str) -> str:
        if not value.endswith("k"):
            raise ValueError("bitrate must be in ffmpeg style, e.g. 192k")
        return value

    @field_validator("adaptive_lpf_max_cut_hz")
    @classmethod
    def validate_adaptive_lpf_max_cut_hz(cls, value: float) -> float:
        if not 0.0 <= value <= 10000.0:
            raise ValueError("adaptive-lpf-max-cut-hz must be between 0 and 10000")
        return value

    @field_validator("adaptive_noise_max_db")
    @classmethod
    def validate_adaptive_noise_max_db(cls, value: float) -> float:
        if not -60.0 <= value <= -10.0:
            raise ValueError("adaptive-noise-max-db must be between -60 and -10 dB")
        return value

    @field_validator("adaptive_stereo_min_width")
    @classmethod
    def validate_adaptive_stereo_min_width(cls, value: float) -> float:
        if not 0.5 <= value <= 1.0:
            raise ValueError("adaptive-stereo-min-width must be between 0.5 and 1.0")
        return value

    @field_validator("adaptive_centroid_threshold")
    @classmethod
    def validate_adaptive_centroid_threshold(cls, value: float) -> float:
        if not 1000.0 <= value <= 8000.0:
            raise ValueError("adaptive-centroid-threshold must be between 1000 and 8000 Hz")
        return value

    @field_validator("adaptive_rolloff_threshold")
    @classmethod
    def validate_adaptive_rolloff_threshold(cls, value: float) -> float:
        if not 5000.0 <= value <= 20000.0:
            raise ValueError("adaptive-rolloff-threshold must be between 5000 and 20000 Hz")
        return value

    @field_validator("adaptive_crest_threshold_low")
    @classmethod
    def validate_adaptive_crest_threshold_low(cls, value: float) -> float:
        if not 1.0 <= value <= 20.0:
            raise ValueError("adaptive-crest-threshold-low must be between 1 and 20 dB")
        return value

    @field_validator("adaptive_crest_threshold_high")
    @classmethod
    def validate_adaptive_crest_threshold_high(cls, value: float) -> float:
        if not 4.0 <= value <= 30.0:
            raise ValueError("adaptive-crest-threshold-high must be between 4 and 30 dB")
        return value

    @model_validator(mode="after")
    def validate_output_compat(self) -> "RunConfig":
        resolved = self.resolve_output_format()
        if resolved == OutputFormat.wav and self.bitrate != "192k":
            # bitrate is irrelevant for wav, but keeping deterministic config output helps logs.
            self.bitrate = "192k"
        if self.adaptive_crest_threshold_low >= self.adaptive_crest_threshold_high:
            raise ValueError("adaptive-crest-threshold-low must be less than adaptive-crest-threshold-high")
        return self

    def resolve_output_format(self) -> OutputFormat:
        if self.output_format != OutputFormat.auto:
            return self.output_format
        suffix = self.output.suffix.lower()
        if suffix == ".wav":
            return OutputFormat.wav
        return OutputFormat.mp3

    def resolved_output_path(self) -> Path:
        resolved_format = self.resolve_output_format()
        if resolved_format == OutputFormat.wav and self.output.suffix.lower() != ".wav":
            return self.output.with_suffix(".wav")
        if resolved_format == OutputFormat.mp3 and self.output.suffix.lower() != ".mp3":
            return self.output.with_suffix(".mp3")
        return self.output
