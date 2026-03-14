from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from nightfall_mix.analysis import AdaptiveProcessing, TrackAnalysis
from nightfall_mix.config import OutputFormat, QualityMode, RainPresence, RunConfig
from nightfall_mix.effects_presets import PresetSpec
from nightfall_mix.mixer import MixPlan

BASE_SAMPLE_RATE = 48_000
MIN_SUB_BASS_CLEANUP_HZ = 30.0
FINAL_TRUE_PEAK_LIMIT = 10 ** (-1.0 / 20.0)


def _db_to_linear(db: float) -> float:
    return 10 ** (db / 20.0)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _stereo_pan(width: float) -> str:
    width = _clamp(width, 0.0, 1.2)
    a = (1.0 + width) / 2.0
    b = (1.0 - width) / 2.0
    return f"pan=stereo|c0={a:.5f}*c0+{b:.5f}*c1|c1={b:.5f}*c0+{a:.5f}*c1"


def _hq_resample_filter(sample_rate: int = BASE_SAMPLE_RATE) -> str:
    return f"aresample={sample_rate}:resampler=soxr:precision=28"


def _best_loudnorm_filter(target_lufs: float, measured: dict[str, float]) -> str:
    required = ["input_i", "input_lra", "input_tp", "input_thresh", "target_offset"]
    if not all(key in measured for key in required):
        return f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11"
    return (
        f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11:"
        f"measured_I={measured['input_i']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:"
        "linear=true"
    )


def _track_loudness_steps(config: RunConfig, analysis: TrackAnalysis | None) -> list[str]:
    gain_db = analysis.loudness.recommended_gain_db if analysis else None
    if gain_db is None or not math.isfinite(gain_db):
        return []

    # Per-track stage should only do rough balancing.
    # Final export loudness is handled once at the master stage.
    clamp_db = 6.0 if config.quality_mode != QualityMode.fast else 12.0
    gain_db = max(-clamp_db, min(clamp_db, gain_db))
    return [f"volume={gain_db:.2f}dB"]

def _compressor_filter(
    threshold_db: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    mix: float = 1.0,
) -> str:
    threshold_linear = _clamp(_db_to_linear(threshold_db), 0.000976563, 1.0)
    return (
        f"acompressor=threshold={threshold_linear:.6f}:ratio={_clamp(ratio, 1.0, 20.0):.2f}:"
        f"attack={_clamp(attack_ms, 0.01, 2000.0):.1f}:"
        f"release={_clamp(release_ms, 0.01, 9000.0):.1f}:mix={_clamp(mix, 0.0, 1.0):.2f}"
    )


def _music_processing_steps(
    preset: PresetSpec,
    *,
    lpf_hz: Optional[float] = None,
    apply_lpf: bool = True,
    saturation_scale: Optional[float] = None,
    compression_scale: Optional[float] = None,
    stereo_width: Optional[float] = None,
    wow_depth_scale: float = 1.0,
) -> list[str]:
    effective_hpf = max(MIN_SUB_BASS_CLEANUP_HZ, preset.hpf_hz)
    effective_lpf = _clamp(lpf_hz if lpf_hz is not None else preset.lpf_hz, 3000.0, 18000.0)
    effective_q = _clamp(preset.lpf_q, 0.4, 2.0)
    sat_scale = _clamp(saturation_scale if saturation_scale is not None else preset.saturation_scale, 0.3, 2.2)
    comp_scale = _clamp(compression_scale if compression_scale is not None else preset.compression_scale, 0.3, 2.2)
    width = _clamp(stereo_width if stereo_width is not None else preset.stereo_width, 0.0, 1.2)

    drive_strength = _clamp((sat_scale * 0.7) + (preset.tape_drive * 0.9), 0.5, 3.0)
    pre_gain_db = (drive_strength - 1.0) * 5.0
    bias_gain_db = preset.tape_bias * 4.0
    softclip_threshold = _clamp(
        preset.softclip_threshold - ((drive_strength - 1.0) * 0.04),
        0.82,
        0.995,
    )
    softclip_param = _clamp(1.0 + (preset.tape_bias * 1.2), 0.25, 2.2)
    comp_threshold_db = preset.comp_threshold_db + (1.0 - comp_scale) * 3.5
    comp_mix = _clamp(0.70 + (comp_scale * 0.25), 0.45, 1.0)
    wow_depth = _clamp(preset.wow_depth * wow_depth_scale, 0.0, 0.02)
    flutter_depth = _clamp(preset.flutter_depth * wow_depth_scale, 0.0, 0.01)

    steps: list[str] = [f"highpass=f={effective_hpf:.1f}:t=q:w=0.707"]
    if apply_lpf:
        steps.append(f"lowpass=f={effective_lpf:.1f}:t=q:w={effective_q:.3f}")
    if abs(bias_gain_db) > 0.01:
        steps.append(f"equalizer=f=3400:t=q:w=0.85:g={bias_gain_db:.2f}")
    if abs(pre_gain_db) > 0.01:
        steps.append(f"volume={pre_gain_db:.2f}dB")
    steps.append(
        f"asoftclip=type=tanh:threshold={softclip_threshold:.3f}:output=0.985:param={softclip_param:.3f}:oversample=4"
    )
    if abs(pre_gain_db) > 0.01:
        steps.append(f"volume={(-pre_gain_db * 0.58):.2f}dB")
    steps.append(
        _compressor_filter(
            threshold_db=comp_threshold_db,
            ratio=preset.comp_ratio,
            attack_ms=preset.comp_attack_ms,
            release_ms=preset.comp_release_ms,
            mix=comp_mix,
        )
    )
    if preset.sample_rate_reduction_hz < (BASE_SAMPLE_RATE - 500.0):
        target_sr = int(_clamp(preset.sample_rate_reduction_hz, 8000.0, BASE_SAMPLE_RATE))
        steps.append(f"aresample={target_sr}:resampler=soxr:precision=24")
        steps.append(_hq_resample_filter())
    if preset.bit_depth < 16:
        steps.append(f"acrusher=bits={preset.bit_depth}:mix=1:mode=lin:aa=0.75")
    if wow_depth > 0.0 and preset.wow_rate_hz > 0.0:
        steps.append(f"vibrato=f={preset.wow_rate_hz:.3f}:d={wow_depth:.4f}")
    if flutter_depth > 0.0 and preset.flutter_rate_hz > 0.0:
        steps.append(f"vibrato=f={preset.flutter_rate_hz:.3f}:d={flutter_depth:.4f}")
    steps.append(_stereo_pan(width))
    return steps


def _dominant_noise_levels(
    preset: PresetSpec,
    adaptive_noise_db: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    if adaptive_noise_db is None:
        vinyl = preset.vinyl_noise_level_db if preset.vinyl_noise_level_db > -90.0 else None
        hiss = preset.tape_hiss_level_db if preset.tape_hiss_level_db > -90.0 else None
        return vinyl, hiss

    if preset.vinyl_noise_level_db >= preset.tape_hiss_level_db:
        vinyl = adaptive_noise_db
        hiss = adaptive_noise_db - 6.0 if preset.tape_hiss_level_db > -90.0 else None
    else:
        hiss = adaptive_noise_db
        vinyl = adaptive_noise_db - 6.0 if preset.vinyl_noise_level_db > -90.0 else None
    return vinyl, hiss


def _add_noise_layers(
    *,
    lines: list[str],
    input_label: str,
    output_prefix: str,
    duration_sec: float,
    vinyl_db: Optional[float],
    hiss_db: Optional[float],
    seed_base: int,
) -> str:
    current_label = input_label
    mix_index = 0
    if vinyl_db is not None and vinyl_db > -90.0:
        amp = _clamp(_db_to_linear(vinyl_db), 0.0, 1.0)
        vinyl_label = f"[{output_prefix}vinyl]"
        lines.append(
            f"anoisesrc=color=pink:amplitude={amp:.8f}:sample_rate={BASE_SAMPLE_RATE}:seed={seed_base},"
            f"highpass=f=120,lowpass=f=7800,atrim=duration={duration_sec:.3f}{vinyl_label}"
        )
        out_label = f"[{output_prefix}noise{mix_index}]"
        lines.append(f"{current_label}{vinyl_label}amix=inputs=2:duration=first:dropout_transition=0:normalize=0{out_label}")
        current_label = out_label
        mix_index += 1
    if hiss_db is not None and hiss_db > -90.0:
        amp = _clamp(_db_to_linear(hiss_db), 0.0, 1.0)
        hiss_label = f"[{output_prefix}hiss]"
        lines.append(
            f"anoisesrc=color=white:amplitude={amp:.8f}:sample_rate={BASE_SAMPLE_RATE}:seed={seed_base + 1},"
            f"highpass=f=3500,lowpass=f=14000,atrim=duration={duration_sec:.3f}{hiss_label}"
        )
        out_label = f"[{output_prefix}noise{mix_index}]"
        lines.append(f"{current_label}{hiss_label}amix=inputs=2:duration=first:dropout_transition=0:normalize=0{out_label}")
        current_label = out_label
    return current_label


def _per_track_chain(
    config: RunConfig,
    analysis: TrackAnalysis | None,
    lpf_dip: bool,
) -> str:
    chain: list[str] = [
        "aformat=sample_rates=48000:channel_layouts=stereo",
        _hq_resample_filter(),
    ]
    chain.extend(_track_loudness_steps(config=config, analysis=analysis))
    if lpf_dip:
        chain.append("lowpass=f=7800:t=q:w=0.707")
    return ",".join(chain)


def _adaptive_track_chain(
    config: RunConfig,
    analysis: TrackAnalysis | None,
    preset: PresetSpec,
    lpf_dip: bool,
) -> tuple[str, Optional[float], Optional[float]]:
    chain: list[str] = [
        "aformat=sample_rates=48000:channel_layouts=stereo",
        _hq_resample_filter(),
    ]
    chain.extend(_track_loudness_steps(config=config, analysis=analysis))

    proc: Optional[AdaptiveProcessing] = analysis.adaptive_processing if analysis else None
    if proc is not None:
        chain.extend(
            _music_processing_steps(
                preset,
                lpf_hz=proc.lpf_cutoff_hz,
                apply_lpf=proc.lpf_cutoff_hz is not None,
                saturation_scale=preset.saturation_scale * proc.saturation_strength,
                compression_scale=preset.compression_scale * proc.compression_strength,
                stereo_width=proc.stereo_width_target,
                wow_depth_scale=0.5,
            )
        )
        vinyl_db, hiss_db = _dominant_noise_levels(preset=preset, adaptive_noise_db=proc.noise_added_db)
    else:
        chain.extend(_music_processing_steps(preset, wow_depth_scale=0.5))
        vinyl_db, hiss_db = _dominant_noise_levels(preset=preset, adaptive_noise_db=None)

    if lpf_dip:
        chain.append("lowpass=f=7800:t=q:w=0.707")

    return ",".join(chain), vinyl_db, hiss_db


def _adaptive_master_chain() -> str:
    return ",".join(
        [
            f"highpass=f={MIN_SUB_BASS_CLEANUP_HZ:.1f}:t=q:w=0.707",
            _compressor_filter(threshold_db=-22.0, ratio=1.35, attack_ms=30.0, release_ms=240.0, mix=0.75),
            _stereo_pan(0.95),
        ]
    )


def _rain_presence_profile(config: RunConfig) -> tuple[float, float, float]:
    presence = config.rain_presence
    if presence == RainPresence.behind:
        hpf_hz = 110.0
        lpf_hz = 10_500.0
        width = 0.90
    elif presence == RainPresence.upfront:
        hpf_hz = 45.0
        lpf_hz = 15_500.0
        width = 1.00
    else:
        hpf_hz = 75.0
        lpf_hz = 13_000.0
        width = 0.97

    if config.rain_preserve_low_drops:
        hpf_hz = min(hpf_hz, 55.0)
    return hpf_hz, lpf_hz, width


def _rain_chain(config: RunConfig, preset: PresetSpec, duration_sec: float) -> str:
    combined_rain_db = config.rain_level_db + preset.atmosphere_volume_db
    hpf_hz, presence_lpf_hz, presence_width = _rain_presence_profile(config)
    target_lpf_hz = min(_clamp(preset.atmosphere_lpf_hz, 2000.0, 18000.0), presence_lpf_hz)
    return (
        f"aformat=sample_rates={BASE_SAMPLE_RATE}:channel_layouts=stereo,"
        f"{_hq_resample_filter()},"
        f"highpass=f={hpf_hz:.1f}:t=q:w=0.707,"
        f"lowpass=f={target_lpf_hz:.1f}:t=q:w=0.707,"
        f"{_stereo_pan(_clamp(preset.atmosphere_stereo_width * presence_width, 0.0, 1.2))},"
        f"volume={combined_rain_db:.1f}dB,"
        "asetpts=PTS-STARTPTS,"
        f"atrim=duration={duration_sec:.3f}"
    )


def build_filtergraph(
    mix_plan: MixPlan,
    analyses: dict[str, TrackAnalysis],
    config: RunConfig,
    preset: PresetSpec,
    include_master: bool = True,
    include_rain: bool = True,
    per_track_processing: bool = True,
    preview_start_sec: Optional[float] = None,
    preview_duration_sec: Optional[float] = None,
    safe_mastering: bool = False,
) -> str:
    lines: list[str] = []

    for idx, instance in enumerate(mix_plan.instances):
        transition = mix_plan.transitions[idx - 1] if idx > 0 and idx - 1 < len(mix_plan.transitions) else None
        lpf_dip = bool(transition and transition.lpf_duck_ms)
        analysis = analyses.get(instance.track.id)
        out_label = f"[t{idx}]"
        if per_track_processing:
            if config.adaptive_lofi:
                chain, vinyl_db, hiss_db = _adaptive_track_chain(
                    config=config,
                    analysis=analysis,
                    preset=preset,
                    lpf_dip=lpf_dip,
                )
                lines.append(f"[{idx}:a]{chain}[tp{idx}]")
                processed_label = "[tp{idx}]".format(idx=idx)
                current_label = _add_noise_layers(
                    lines=lines,
                    input_label=processed_label,
                    output_prefix=f"t{idx}_",
                    duration_sec=max(0.1, instance.track.duration_ms / 1000.0),
                    vinyl_db=vinyl_db,
                    hiss_db=hiss_db,
                    seed_base=10_000 + (idx * 10),
                )
                lines.append(f"{current_label}anull{out_label}")
            else:
                chain = _per_track_chain(config=config, analysis=analysis, lpf_dip=lpf_dip)
                lines.append(f"[{idx}:a]{chain}{out_label}")
        else:
            lines.append(f"[{idx}:a]anull{out_label}")

    if len(mix_plan.instances) == 1:
        mix_label = "[mix0]"
        lines.append("[t0]anull[mix0]")
    else:
        current = "[t0]"
        for idx in range(1, len(mix_plan.instances)):
            duration = (
                mix_plan.transitions[idx - 1].crossfade_ms / 1000.0
                if idx - 1 < len(mix_plan.transitions)
                else config.crossfade_sec
            )
            next_label = f"[mix{idx}]"
            lines.append(f"{current}[t{idx}]acrossfade=d={duration:.3f}:c1=qsin:c2=qsin{next_label}")
            current = next_label
        mix_label = current

    if not include_master:
        current_label = mix_label
        if include_rain and config.rain is not None:
            rain_input_idx = len(mix_plan.instances)
            duration_sec = mix_plan.estimated_duration_ms / 1000.0
            lines.append(f"[{rain_input_idx}:a]{_rain_chain(config, preset, duration_sec)}[rain]")
            lines.append(f"{current_label}[rain]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixr]")
            current_label = "[mixr]"
        if preview_duration_sec is not None:
            start_sec = max(0.0, preview_start_sec or 0.0)
            lines.append(
                f"{current_label}atrim=start={start_sec:.3f}:duration={preview_duration_sec:.3f},"
                "asetpts=PTS-STARTPTS[outa]"
            )
        else:
            lines.append(f"{current_label}anull[outa]")
        return ";\n".join(lines)

    music_label = mix_label
    if config.adaptive_lofi:
        lines.append(f"{music_label}{_adaptive_master_chain()}[music0]")
        music_label = "[music0]"
    else:
        music_chain = ",".join(_music_processing_steps(preset))
        lines.append(f"{music_label}{music_chain}[music0]")
        music_label = _add_noise_layers(
            lines=lines,
            input_label="[music0]",
            output_prefix="master_",
            duration_sec=max(0.1, mix_plan.estimated_duration_ms / 1000.0),
            vinyl_db=preset.vinyl_noise_level_db if preset.vinyl_noise_level_db > -90.0 else None,
            hiss_db=preset.tape_hiss_level_db if preset.tape_hiss_level_db > -90.0 else None,
            seed_base=424_242,
        )

    current_label = music_label
    if include_rain and config.rain is not None:
        rain_input_idx = len(mix_plan.instances)
        duration_sec = mix_plan.estimated_duration_ms / 1000.0
        lines.append(f"[{rain_input_idx}:a]{_rain_chain(config, preset, duration_sec)}[rain]")
        lines.append(
            f"{current_label}[rain]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixr]"
        )
        current_label = "[mixr]"

    if preview_duration_sec is not None:
        start_sec = max(0.0, preview_start_sec or 0.0)
        lines.append(
            f"{current_label}atrim=start={start_sec:.3f}:duration={preview_duration_sec:.3f},"
            "asetpts=PTS-STARTPTS[pre_master]"
        )
        current_label = "[pre_master]"

    final_softclip = "asoftclip=type=tanh:threshold=0.985:output=0.985:oversample=4"
    final_limiter = f"alimiter=limit={FINAL_TRUE_PEAK_LIMIT:.3f}:attack=5:release=50:level=false"
    if safe_mastering:
        lines.append(f"{current_label}{final_softclip},{final_limiter}[outa]")
    else:
        lines.append(
            f"{current_label}{final_softclip},"
            f"loudnorm=I={config.lufs}:TP=-1.0:LRA=11,"
            f"{final_limiter}[outa]"
        )
    return ";\n".join(lines)


def build_ffmpeg_command(
    mix_plan: MixPlan,
    config: RunConfig,
    filter_script_path: Path,
    output_path: Path,
    include_rain: bool,
) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for instance in mix_plan.instances:
        cmd.extend(["-i", str(instance.track.path)])
    if include_rain and config.rain is not None:
        cmd.extend(["-stream_loop", "-1", "-i", str(config.rain)])

    cmd.extend(
        [
            "-filter_complex_script",
            str(filter_script_path),
            "-map",
            "[outa]",
            "-vn",
            "-sn",
            "-progress",
            "pipe:1",
            "-nostats",
        ]
    )
    output_format = config.resolve_output_format()
    if output_format == OutputFormat.wav:
        cmd.extend(["-c:a", "pcm_s16le"])
    else:
        cmd.extend(["-c:a", "libmp3lame", "-b:a", config.bitrate])
    for key in sorted(config.metadata_tags.keys()):
        raw_value = config.metadata_tags.get(key)
        clean_key = str(key).strip()
        clean_value = str(raw_value).strip() if raw_value is not None else ""
        if not clean_key or not clean_value:
            continue
        clean_value = clean_value.replace("\r", " ").replace("\n", " ")
        cmd.extend(["-metadata", f"{clean_key}={clean_value}"])
    cmd.append(str(output_path))
    return cmd


def write_filtergraph(path: Path, graph: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(graph, encoding="utf-8")
