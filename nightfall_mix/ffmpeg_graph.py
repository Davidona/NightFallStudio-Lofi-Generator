from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from nightfall_mix.analysis import AdaptiveProcessing, TrackAnalysis
from nightfall_mix.config import OutputFormat, QualityMode, RunConfig
from nightfall_mix.effects_presets import PresetSpec
from nightfall_mix.mixer import MixPlan


def _db_to_linear(db: float) -> float:
    return 10 ** (db / 20.0)


def _stereo_pan(width: float) -> str:
    width = max(0.0, min(1.0, width))
    a = (1.0 + width) / 2.0
    b = (1.0 - width) / 2.0
    return f"pan=stereo|c0={a:.5f}*c0+{b:.5f}*c1|c1={b:.5f}*c0+{a:.5f}*c1"


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


def _per_track_chain(
    config: RunConfig,
    analysis: TrackAnalysis | None,
    lpf_dip: bool,
) -> str:
    chain: list[str] = [
        "aformat=sample_rates=48000:channel_layouts=stereo",
        "aresample=48000",
        "volume=-3dB",
    ]

    if config.quality_mode == QualityMode.fast:
        gain_db = analysis.loudness.recommended_gain_db if analysis else None
        if gain_db is not None and math.isfinite(gain_db):
            chain.append(f"volume={max(-12.0, min(12.0, gain_db)):.2f}dB")
    elif config.quality_mode == QualityMode.balanced:
        chain.append(f"loudnorm=I={config.lufs}:TP=-1.5:LRA=11")
    else:
        measured = analysis.loudness.measured if analysis else {}
        chain.append(_best_loudnorm_filter(target_lufs=config.lufs, measured=measured or {}))

    if lpf_dip:
        chain.append("lowpass=f=7800")
    return ",".join(chain)


def _adaptive_track_chain(
    config: RunConfig,
    analysis: TrackAnalysis | None,
    preset: PresetSpec,
    lpf_dip: bool,
) -> tuple[str, Optional[float]]:
    chain: list[str] = [
        "aformat=sample_rates=48000:channel_layouts=stereo",
        "aresample=48000",
        "volume=-3dB",
    ]

    if config.quality_mode == QualityMode.fast:
        gain_db = analysis.loudness.recommended_gain_db if analysis else None
        if gain_db is not None and math.isfinite(gain_db):
            chain.append(f"volume={max(-12.0, min(12.0, gain_db)):.2f}dB")
    elif config.quality_mode == QualityMode.balanced:
        chain.append(f"loudnorm=I={config.lufs}:TP=-1.5:LRA=11")
    else:
        measured = analysis.loudness.measured if analysis else {}
        chain.append(_best_loudnorm_filter(target_lufs=config.lufs, measured=measured or {}))

    proc: Optional[AdaptiveProcessing] = analysis.adaptive_processing if analysis else None
    if proc is not None:
        if proc.lpf_cutoff_hz is not None:
            chain.append(f"lowpass=f={proc.lpf_cutoff_hz:.1f}")
        sat_strength = max(0.2, min(1.2, proc.saturation_strength))
        threshold = 1.0 - (1.0 - preset.softclip_threshold) * sat_strength
        threshold = max(0.90, min(0.999, threshold))
        chain.append(f"asoftclip=type=tanh:threshold={threshold:.3f}")

        comp_strength = max(0.45, min(1.2, proc.compression_strength))
        ratio = 1.0 + (preset.comp_ratio - 1.0) * comp_strength
        threshold_db = preset.comp_threshold_db + (1.0 - comp_strength) * 3.0
        attack = max(5.0, preset.comp_attack_ms * (1.0 + 0.15 * (1.0 - comp_strength)))
        release = max(80.0, preset.comp_release_ms * (1.0 + 0.20 * (1.0 - comp_strength)))
        chain.append(
            f"acompressor=threshold={threshold_db:.1f}dB:ratio={ratio:.2f}:"
            f"attack={attack:.1f}:release={release:.1f}"
        )
        chain.append(_stereo_pan(proc.stereo_width_target))
        noise_db = proc.noise_added_db
    else:
        chain.append(f"asoftclip=type=tanh:threshold={preset.softclip_threshold:.3f}")
        chain.append(
            f"acompressor=threshold={preset.comp_threshold_db:.1f}dB:ratio={preset.comp_ratio:.2f}:"
            f"attack={preset.comp_attack_ms:.1f}:release={preset.comp_release_ms:.1f}"
        )
        chain.append(_stereo_pan(max(config.adaptive_stereo_min_width, preset.stereo_width)))
        noise_db = preset.noise_level_db if preset.noise_level_db > -90 else None

    if lpf_dip:
        chain.append("lowpass=f=7800")

    return ",".join(chain), noise_db


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
                chain, noise_db = _adaptive_track_chain(
                    config=config,
                    analysis=analysis,
                    preset=preset,
                    lpf_dip=lpf_dip,
                )
                if noise_db is not None and noise_db > -90:
                    amp = _db_to_linear(noise_db)
                    dur_sec = max(0.1, instance.track.duration_ms / 1000.0)
                    lines.append(f"[{idx}:a]{chain}[tp{idx}]")
                    lines.append(
                        f"anoisesrc=color=pink:amplitude={amp:.8f}:sample_rate=48000:seed={10_000 + idx},"
                        f"lowpass=f=9000,highpass=f=220,atrim=duration={dur_sec:.3f}[tn{idx}]"
                    )
                    lines.append(f"[tp{idx}][tn{idx}]amix=inputs=2:duration=first:dropout_transition=0{out_label}")
                else:
                    lines.append(f"[{idx}:a]{chain}{out_label}")
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
            lines.append(
                f"{current}[t{idx}]acrossfade=d={duration:.3f}:c1=qsin:c2=qsin{next_label}"
            )
            current = next_label
        mix_label = current

    if include_rain and config.rain is not None:
        rain_input_idx = len(mix_plan.instances)
        duration_sec = mix_plan.estimated_duration_ms / 1000.0
        lines.append(
            f"[{rain_input_idx}:a]"
            "aformat=sample_rates=48000:channel_layouts=stereo,"
            "aresample=48000,"
            "highpass=f=200,"
            "lowpass=f=11000,"
            f"volume={config.rain_level_db}dB,"
            "asetpts=PTS-STARTPTS,"
            f"atrim=duration={duration_sec:.3f}[rain]"
        )
        lines.append(f"{mix_label}[rain]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixr]")
        mix_label = "[mixr]"

    if not include_master:
        if preview_duration_sec is not None:
            start_sec = max(0.0, preview_start_sec or 0.0)
            lines.append(
                f"{mix_label}atrim=start={start_sec:.3f}:duration={preview_duration_sec:.3f},"
                "asetpts=PTS-STARTPTS[outa]"
            )
        else:
            lines.append(f"{mix_label}anull[outa]")
        return ";\n".join(lines)

    master_label = "[master0]"
    if config.adaptive_lofi:
        lines.append(
            f"{mix_label}"
            "highpass=f=40,"
            "acompressor=threshold=-22dB:ratio=1.35:attack=30:release=240,"
            f"{_stereo_pan(0.95)}"
            f"{master_label}"
        )
    else:
        lines.append(
            f"{mix_label}"
            f"highpass=f={preset.hpf_hz:.1f},"
            f"lowpass=f={preset.lpf_hz:.1f},"
            f"asoftclip=type=tanh:threshold={preset.softclip_threshold:.3f},"
            f"acompressor=threshold={preset.comp_threshold_db:.1f}dB:ratio={preset.comp_ratio:.2f}:"
            f"attack={preset.comp_attack_ms:.1f}:release={preset.comp_release_ms:.1f},"
            f"{_stereo_pan(preset.stereo_width)}"
            f"{master_label}"
        )

    current_label = master_label
    wow_depth = preset.wow_depth * (0.5 if config.adaptive_lofi else 1.0)
    if wow_depth > 0 and preset.wow_rate_hz > 0:
        lines.append(
            f"{current_label}vibrato=f={preset.wow_rate_hz:.3f}:d={wow_depth:.4f}[master1]"
        )
        current_label = "[master1]"

    if (not config.adaptive_lofi) and preset.noise_level_db > -90:
        amp = _db_to_linear(preset.noise_level_db)
        lines.append(
            f"anoisesrc=color=pink:amplitude={amp:.8f}:sample_rate=48000:seed=424242,"
            "lowpass=f=9000,highpass=f=220[crackle]"
        )
        lines.append(f"{current_label}[crackle]amix=inputs=2:duration=first:dropout_transition=0[master2]")
        current_label = "[master2]"

    if preview_duration_sec is not None:
        start_sec = max(0.0, preview_start_sec or 0.0)
        lines.append(
            f"{current_label}atrim=start={start_sec:.3f}:duration={preview_duration_sec:.3f},"
            "asetpts=PTS-STARTPTS[pre_master]"
        )
        current_label = "[pre_master]"

    if safe_mastering:
        lines.append(f"{current_label}alimiter=limit=0.891[outa]")
    else:
        lines.append(
            f"{current_label}"
            f"loudnorm=I={config.lufs}:TP=-1.0:LRA=11,"
            "alimiter=limit=0.891[outa]"
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
