from nightfall_mix.config import PresetName
from nightfall_mix.effects_presets import PRESETS, get_preset


def test_all_preset_names_have_specs() -> None:
    assert set(PRESETS.keys()) == set(PresetName)


def test_presets_are_not_identical() -> None:
    signatures = {
        (
            preset.hpf_hz,
            preset.lpf_hz,
            preset.lpf_q,
            preset.saturation_scale,
            preset.tape_drive,
            preset.tape_bias,
            preset.softclip_threshold,
            preset.compression_scale,
            preset.comp_ratio,
            preset.comp_threshold_db,
            preset.comp_attack_ms,
            preset.comp_release_ms,
            preset.bit_depth,
            preset.sample_rate_reduction_hz,
            preset.stereo_width,
            preset.wow_rate_hz,
            preset.wow_depth,
            preset.flutter_rate_hz,
            preset.flutter_depth,
            preset.vinyl_noise_level_db,
            preset.tape_hiss_level_db,
            preset.atmosphere_volume_db,
            preset.atmosphere_stereo_width,
            preset.atmosphere_lpf_hz,
        )
        for preset in (get_preset(name) for name in PresetName)
    }
    assert len(signatures) == len(PresetName)
