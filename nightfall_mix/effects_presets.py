from __future__ import annotations

from dataclasses import dataclass

from nightfall_mix.config import PresetName


@dataclass(frozen=True)
class PresetSpec:
    name: PresetName
    hpf_hz: float
    lpf_hz: float
    softclip_threshold: float
    comp_ratio: float
    comp_threshold_db: float
    comp_attack_ms: float
    comp_release_ms: float
    stereo_width: float
    wow_rate_hz: float
    wow_depth: float
    noise_level_db: float


PRESETS: dict[PresetName, PresetSpec] = {
    PresetName.tokyo_cassette: PresetSpec(
        name=PresetName.tokyo_cassette,
        hpf_hz=38.0,
        lpf_hz=9000.0,
        softclip_threshold=0.983,
        comp_ratio=2.1,
        comp_threshold_db=-17.0,
        comp_attack_ms=24.0,
        comp_release_ms=210.0,
        stereo_width=0.78,
        wow_rate_hz=0.27,
        wow_depth=0.0045,
        noise_level_db=-45.0,
    ),
    PresetName.vinyl_room: PresetSpec(
        name=PresetName.vinyl_room,
        hpf_hz=55.0,
        lpf_hz=7800.0,
        softclip_threshold=0.976,
        comp_ratio=2.4,
        comp_threshold_db=-16.0,
        comp_attack_ms=35.0,
        comp_release_ms=300.0,
        stereo_width=0.72,
        wow_rate_hz=0.18,
        wow_depth=0.0025,
        noise_level_db=-39.0,
    ),
    PresetName.cleaner_lofi: PresetSpec(
        name=PresetName.cleaner_lofi,
        hpf_hz=30.0,
        lpf_hz=12000.0,
        softclip_threshold=0.993,
        comp_ratio=1.45,
        comp_threshold_db=-24.0,
        comp_attack_ms=14.0,
        comp_release_ms=160.0,
        stereo_width=0.94,
        wow_rate_hz=0.0,
        wow_depth=0.0,
        noise_level_db=-120.0,
    ),
    PresetName.night_owl_fm: PresetSpec(
        name=PresetName.night_owl_fm,
        hpf_hz=70.0,
        lpf_hz=6900.0,
        softclip_threshold=0.972,
        comp_ratio=2.7,
        comp_threshold_db=-15.0,
        comp_attack_ms=40.0,
        comp_release_ms=320.0,
        stereo_width=0.68,
        wow_rate_hz=0.12,
        wow_depth=0.0018,
        noise_level_db=-36.0,
    ),
    PresetName.rainy_microcassette: PresetSpec(
        name=PresetName.rainy_microcassette,
        hpf_hz=45.0,
        lpf_hz=8300.0,
        softclip_threshold=0.979,
        comp_ratio=2.2,
        comp_threshold_db=-17.5,
        comp_attack_ms=28.0,
        comp_release_ms=260.0,
        stereo_width=0.76,
        wow_rate_hz=0.35,
        wow_depth=0.0060,
        noise_level_db=-41.0,
    ),
    PresetName.velvet_room: PresetSpec(
        name=PresetName.velvet_room,
        hpf_hz=33.0,
        lpf_hz=10800.0,
        softclip_threshold=0.988,
        comp_ratio=1.75,
        comp_threshold_db=-20.5,
        comp_attack_ms=20.0,
        comp_release_ms=200.0,
        stereo_width=0.88,
        wow_rate_hz=0.10,
        wow_depth=0.0010,
        noise_level_db=-50.0,
    ),
    PresetName.sunrise_clean: PresetSpec(
        name=PresetName.sunrise_clean,
        hpf_hz=26.0,
        lpf_hz=14500.0,
        softclip_threshold=0.995,
        comp_ratio=1.35,
        comp_threshold_db=-25.0,
        comp_attack_ms=12.0,
        comp_release_ms=140.0,
        stereo_width=0.96,
        wow_rate_hz=0.0,
        wow_depth=0.0,
        noise_level_db=-120.0,
    ),
}


def get_preset(preset_name: PresetName) -> PresetSpec:
    return PRESETS[preset_name]
