from nightfall_mix.config import PresetName
from nightfall_mix.effects_presets import PRESETS, get_preset


def test_all_preset_names_have_specs() -> None:
    assert set(PRESETS.keys()) == set(PresetName)


def test_presets_are_not_identical() -> None:
    signatures = {
        (
            preset.hpf_hz,
            preset.lpf_hz,
            preset.softclip_threshold,
            preset.comp_ratio,
            preset.comp_threshold_db,
            preset.comp_attack_ms,
            preset.comp_release_ms,
            preset.stereo_width,
            preset.wow_rate_hz,
            preset.wow_depth,
            preset.noise_level_db,
        )
        for preset in (get_preset(name) for name in PresetName)
    }
    assert len(signatures) == len(PresetName)
