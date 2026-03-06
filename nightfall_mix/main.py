from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import replace
from enum import IntEnum
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from nightfall_mix.analysis import (
    AdaptiveMetrics,
    AdaptiveProcessing,
    TrackAnalysis,
    analyze_adaptive_metrics,
    analyze_track,
    derive_adaptive_processing,
    fallback_adaptive_processing,
)
from nightfall_mix.config import (
    OrderMode,
    OutputFormat,
    PresetName,
    QualityMode,
    RunConfig,
    SmartOrderingMode,
)
from nightfall_mix.effects_presets import get_preset
from nightfall_mix.ffmpeg_graph import build_ffmpeg_command, build_filtergraph, write_filtergraph
from nightfall_mix.logging_setup import configure_logging
from nightfall_mix.mixer import (
    MixPlan,
    TrackInstance,
    TrackSource,
    TimelineEntry,
    build_instances,
    build_mix_plan,
    discover_audio_files,
    order_sources_by_transition_fit,
    order_track_paths,
)
from nightfall_mix.tracklists import write_tracklist_artifacts
from nightfall_mix.utils import CommandError, ensure_dependencies, ffprobe_duration_ms, format_hms, write_json

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich")


class ExitCode(IntEnum):
    SUCCESS = 0
    CLI_ERROR = 2
    DEPENDENCY_ERROR = 3
    NO_VALID_INPUT = 4
    ORDER_ERROR = 5
    ANALYSIS_ERROR = 6
    RENDER_ERROR = 7
    OUTPUT_ERROR = 8


def _metrics_payload(analysis: TrackAnalysis) -> dict:
    metrics: AdaptiveMetrics | None = analysis.adaptive_metrics
    return {
        "lufs": metrics.lufs if metrics else analysis.loudness.input_i,
        "crest_factor_db": metrics.crest_factor_db if metrics else None,
        "spectral_centroid_hz": metrics.spectral_centroid_hz if metrics else None,
        "rolloff_hz": metrics.rolloff_hz if metrics else None,
        "stereo_width": metrics.stereo_width if metrics else None,
        "noise_floor_dbfs": metrics.noise_floor_dbfs if metrics else None,
    }


def _processing_payload(analysis: TrackAnalysis) -> dict:
    proc: AdaptiveProcessing | None = analysis.adaptive_processing
    return {
        "lpf_cutoff_hz": proc.lpf_cutoff_hz if proc else None,
        "saturation_strength": proc.saturation_strength if proc else None,
        "compression_strength": proc.compression_strength if proc else None,
        "stereo_width_target": proc.stereo_width_target if proc else None,
        "noise_added_db": proc.noise_added_db if proc else None,
    }


def _write_adaptive_artifacts(
    track_sources: list[TrackSource],
    plan: MixPlan,
    analyses: dict[str, TrackAnalysis],
    adaptive_report_path: Path,
    output_path: Path,
) -> tuple[Path, Path]:
    adaptive_report_path.parent.mkdir(parents=True, exist_ok=True)
    report_items: list[dict] = []
    for src in track_sources:
        analysis = analyses.get(src.id)
        if analysis is None:
            continue
        report_items.append(
            {
                "track": src.path.name,
                "metrics": _metrics_payload(analysis),
                "applied_processing": _processing_payload(analysis),
                "rationale": analysis.adaptive_processing.rationale
                if analysis.adaptive_processing
                else "Adaptive processing unavailable; defaults used.",
            }
        )
    write_json(adaptive_report_path, report_items)

    processing_tracklist_path = output_path.with_name("tracklist_with_processing.txt")
    with processing_tracklist_path.open("w", encoding="utf-8") as f:
        for i, entry in enumerate(plan.timeline, start=1):
            analysis = analyses.get(entry.track_id)
            rationale = (
                analysis.adaptive_processing.rationale
                if analysis and analysis.adaptive_processing
                else "adaptive unavailable; preset fallback"
            )
            f.write(f"{i}. {entry.filename} -- {rationale}\n")
    return adaptive_report_path, processing_tracklist_path


def _run_render(
    plan: MixPlan,
    analyses: dict[str, TrackAnalysis],
    config: RunConfig,
    output_path: Path,
    include_master: bool,
    include_rain: bool,
    per_track_processing: bool,
    logger,
) -> None:
    preset = get_preset(config.preset)
    with tempfile.TemporaryDirectory(prefix="nightfall_graph_") as td:
        script_path = Path(td) / "filtergraph.txt"

        def _run_with_preset(preset_spec) -> None:
            graph = build_filtergraph(
                mix_plan=plan,
                analyses=analyses,
                config=config,
                preset=preset_spec,
                include_master=include_master,
                include_rain=include_rain,
                per_track_processing=per_track_processing,
            )
            write_filtergraph(script_path, graph)
            command = build_ffmpeg_command(
                mix_plan=plan,
                config=config,
                filter_script_path=script_path,
                output_path=output_path,
                include_rain=include_rain,
            )

            expected_ms = max(1, plan.estimated_duration_ms)
            progress = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
            )
            with progress:
                task = progress.add_task("Rendering mix", total=expected_ms)

                def on_stdout(line: str) -> None:
                    if line.startswith("out_time_ms="):
                        val = line.split("=", 1)[1].strip()
                        if val.isdigit():
                            current_ms = int(int(val) / 1000)
                            progress.update(task, completed=min(expected_ms, current_ms))
                    elif line == "progress=end":
                        progress.update(task, completed=expected_ms)

                def on_stderr(line: str) -> None:
                    if line:
                        logger.debug(line)

                from nightfall_mix.utils import run_command_stream

                run_command_stream(
                    command,
                    logger=logger,
                    on_stdout_line=on_stdout,
                    on_stderr_line=on_stderr,
                )

        try:
            _run_with_preset(preset)
        except CommandError as exc:
            err = (exc.stderr or "").lower()
            if "no such filter" in err and "vibrato" in err and preset.wow_depth > 0:
                logger.warning("ffmpeg does not support vibrato; retrying render without wow/flutter")
                fallback = replace(preset, wow_depth=0.0, wow_rate_hz=0.0)
                _run_with_preset(fallback)
            else:
                raise


def _build_chunk_plan(instances: list[TrackInstance], durations_only_crossfade_sec: float) -> MixPlan:
    transitions = []
    timeline = []
    cursor = 0
    fade_ms = int(durations_only_crossfade_sec * 1000)
    min_ms = 500
    for i, inst in enumerate(instances):
        if i > 0:
            max_allowed = max(min_ms, min(instances[i - 1].track.duration_ms, inst.track.duration_ms) - 500)
            cursor -= min(fade_ms, max_allowed)
        start = max(0, cursor)
        end = start + inst.track.duration_ms
        timeline.append(
            {
                "instance_index": inst.instance_index,
                "track_id": inst.track.id,
                "filename": inst.track.path.name,
                "start_time_ms": start,
                "end_time_ms": end,
                "cycle_index": inst.cycle_index,
            }
        )
        cursor = end
    for i in range(len(instances) - 1):
        transitions.append(
            {
                "from_track_id": instances[i].track.id,
                "to_track_id": instances[i + 1].track.id,
                "crossfade_ms": min(
                    fade_ms,
                    max(min_ms, min(instances[i].track.duration_ms, instances[i + 1].track.duration_ms) - 500),
                ),
                "smart_used": False,
                "reason": "chunk-fixed",
                "key_distance": None,
                "lpf_duck_ms": None,
            }
        )

    from nightfall_mix.mixer import TimelineEntry, TransitionPlan

    return MixPlan(
        instances=instances,
        timeline=[TimelineEntry(**item) for item in timeline],
        transitions=[TransitionPlan(**item) for item in transitions],
        estimated_duration_ms=timeline[-1]["end_time_ms"] if timeline else 0,
        target_reached=True,
    )


def _render_staged(
    full_plan: MixPlan,
    analyses: dict[str, TrackAnalysis],
    config: RunConfig,
    output_path: Path,
    logger,
) -> None:
    temp_root = output_path.parent / f".nightfall_tmp_{int(time.time())}"
    temp_root.mkdir(parents=True, exist_ok=True)
    logger.warning(
        "Large mix detected (%s tracks); using staged fallback in %s",
        len(full_plan.instances),
        temp_root,
    )
    chunk_size = config.chunk_threshold
    chunk_files: list[Path] = []
    wav_cfg = config.model_copy(update={"output_format": OutputFormat.wav})

    try:
        for chunk_idx in range(0, len(full_plan.instances), chunk_size):
            chunk_instances = full_plan.instances[chunk_idx : chunk_idx + chunk_size]
            chunk_plan = _build_chunk_plan(chunk_instances, config.crossfade_sec)
            chunk_out = temp_root / f"chunk_{chunk_idx // chunk_size:03d}.wav"
            _run_render(
                plan=chunk_plan,
                analyses=analyses,
                config=wav_cfg,
                output_path=chunk_out,
                include_master=False,
                include_rain=False,
                per_track_processing=True,
                logger=logger,
            )
            chunk_files.append(chunk_out)

        merged_sources: list[TrackSource] = []
        merged_instances: list[TrackInstance] = []
        for idx, chunk_file in enumerate(chunk_files):
            duration = ffprobe_duration_ms(chunk_file, logger=logger)
            src = TrackSource(id=f"chunk{idx}", path=chunk_file, duration_ms=duration)
            merged_sources.append(src)
            merged_instances.append(TrackInstance(instance_index=idx, track=src, cycle_index=0))
        merged_plan = _build_chunk_plan(merged_instances, config.crossfade_sec)
        merged_out = temp_root / "merged.wav"
        _run_render(
            plan=merged_plan,
            analyses={},
            config=wav_cfg,
            output_path=merged_out,
            include_master=False,
            include_rain=False,
            per_track_processing=False,
            logger=logger,
        )

        final_duration = ffprobe_duration_ms(merged_out, logger=logger)
        final_src = TrackSource(id="merged", path=merged_out, duration_ms=final_duration)
        final_plan = MixPlan(
            instances=[TrackInstance(instance_index=0, track=final_src, cycle_index=0)],
            timeline=[
                TimelineEntry(
                    instance_index=0,
                    track_id="merged",
                    filename=merged_out.name,
                    start_time_ms=0,
                    end_time_ms=final_duration,
                    cycle_index=0,
                    analysis_snapshot={},
                )
            ],
            transitions=[],
            estimated_duration_ms=final_duration,
            target_reached=True,
        )
        _run_render(
            plan=final_plan,
            analyses={},
            config=config,
            output_path=output_path,
            include_master=True,
            include_rain=config.rain is not None,
            per_track_processing=False,
            logger=logger,
        )
    except Exception:
        logger.error("Staged render failed; temporary files kept at %s", temp_root)
        raise
    else:
        shutil.rmtree(temp_root, ignore_errors=True)


def _collect_metadata(
    config: RunConfig,
    plan: MixPlan,
    analyses: dict[str, TrackAnalysis],
    output_path: Path,
    warnings: list[str],
) -> dict:
    return {
        "output_path": str(output_path),
        "estimated_duration_ms": plan.estimated_duration_ms,
        "target_reached": plan.target_reached,
        "track_count": len(plan.instances),
        "warnings": warnings,
        "config": config.model_dump(mode="json"),
        "transitions": [
            {
                "from": t.from_track_id,
                "to": t.to_track_id,
                "crossfade_ms": t.crossfade_ms,
                "reason": t.reason,
                "smart_used": t.smart_used,
                "key_distance": t.key_distance,
                "lpf_duck_ms": t.lpf_duck_ms,
            }
            for t in plan.transitions
        ],
        "analysis": {
            track_id: {
                "bpm": a.bpm,
                "bpm_confidence": a.bpm_confidence,
                "key": a.key,
                "key_confidence": a.key_confidence,
                "warnings": a.warnings,
                "loudness": {
                    "input_i": a.loudness.input_i,
                    "input_tp": a.loudness.input_tp,
                    "input_lra": a.loudness.input_lra,
                    "input_thresh": a.loudness.input_thresh,
                },
                "adaptive_metrics": _metrics_payload(a) if a.adaptive_metrics else None,
                "adaptive_processing": _processing_payload(a) if a.adaptive_processing else None,
            }
            for track_id, a in analyses.items()
        },
    }


@app.command("nightfall-mix")
def run_nightfall_mix(
    songs_folder: Path = typer.Option(..., "--songs-folder"),
    output: Path = typer.Option(..., "--output"),
    rain: Optional[Path] = typer.Option(None, "--rain"),
    order: OrderMode = typer.Option(OrderMode.alpha, "--order"),
    seed: Optional[int] = typer.Option(None, "--seed"),
    target_duration_min: Optional[int] = typer.Option(None, "--target-duration-min"),
    crossfade_sec: float = typer.Option(6.0, "--crossfade-sec"),
    smart_crossfade: bool = typer.Option(False, "--smart-crossfade"),
    smart_ordering: bool = typer.Option(False, "--smart-ordering"),
    smart_ordering_mode: SmartOrderingMode = typer.Option(
        SmartOrderingMode.bpm_key_balanced, "--smart-ordering-mode"
    ),
    lufs: float = typer.Option(-14.0, "--lufs"),
    preset: PresetName = typer.Option(PresetName.tokyo_cassette, "--preset"),
    metadata_json: Optional[Path] = typer.Option(None, "--metadata-json"),
    quality_mode: QualityMode = typer.Option(QualityMode.best, "--quality-mode"),
    rain_level_db: float = typer.Option(-28.0, "--rain-level-db"),
    mix_log: Optional[Path] = typer.Option(None, "--mix-log"),
    enable_warp: bool = typer.Option(False, "--enable-warp"),
    output_format: OutputFormat = typer.Option(OutputFormat.auto, "--output-format"),
    bitrate: str = typer.Option("192k", "--bitrate"),
    strict_analysis: bool = typer.Option(False, "--strict-analysis"),
    adaptive_lofi: bool = typer.Option(False, "--adaptive-lofi"),
    adaptive_report: Path = typer.Option(Path("adaptive_report.json"), "--adaptive-report"),
    adaptive_lpf_max_cut_hz: float = typer.Option(2000.0, "--adaptive-lpf-max-cut-hz"),
    adaptive_noise_max_db: float = typer.Option(-30.0, "--adaptive-noise-max-db"),
    adaptive_stereo_min_width: float = typer.Option(0.75, "--adaptive-stereo-min-width"),
    adaptive_centroid_threshold: float = typer.Option(3200.0, "--adaptive-centroid-threshold"),
    adaptive_rolloff_threshold: float = typer.Option(10000.0, "--adaptive-rolloff-threshold"),
    adaptive_crest_threshold_low: float = typer.Option(8.0, "--adaptive-crest-threshold-low"),
    adaptive_crest_threshold_high: float = typer.Option(14.0, "--adaptive-crest-threshold-high"),
    chunk_threshold: int = typer.Option(180, "--chunk-threshold", hidden=True),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    try:
        config = RunConfig(
            songs_folder=songs_folder,
            output=output,
            rain=rain,
            order=order,
            seed=seed,
            target_duration_min=target_duration_min,
            crossfade_sec=crossfade_sec,
            smart_crossfade=smart_crossfade,
            smart_ordering=smart_ordering,
            smart_ordering_mode=smart_ordering_mode,
            lufs=lufs,
            preset=preset,
            metadata_json=metadata_json,
            quality_mode=quality_mode,
            rain_level_db=rain_level_db,
            mix_log=mix_log,
            enable_warp=enable_warp,
            output_format=output_format,
            bitrate=bitrate,
            strict_analysis=strict_analysis,
            adaptive_lofi=adaptive_lofi,
            adaptive_report=adaptive_report,
            adaptive_lpf_max_cut_hz=adaptive_lpf_max_cut_hz,
            adaptive_noise_max_db=adaptive_noise_max_db,
            adaptive_stereo_min_width=adaptive_stereo_min_width,
            adaptive_centroid_threshold=adaptive_centroid_threshold,
            adaptive_rolloff_threshold=adaptive_rolloff_threshold,
            adaptive_crest_threshold_low=adaptive_crest_threshold_low,
            adaptive_crest_threshold_high=adaptive_crest_threshold_high,
            chunk_threshold=chunk_threshold,
        )
    except ValidationError as exc:
        typer.echo(f"Invalid arguments:\n{exc}")
        raise typer.Exit(code=ExitCode.CLI_ERROR)

    logger = configure_logging(log_file=config.mix_log, verbose=verbose)
    output_path = config.resolved_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        ensure_dependencies(logger=logger)
    except Exception as exc:
        typer.echo(f"Dependency error: {exc}")
        raise typer.Exit(code=ExitCode.DEPENDENCY_ERROR)

    discovered = discover_audio_files(config.songs_folder)
    if not discovered:
        typer.echo(
            "No supported audio files found. Supported extensions: "
            ".mp3,.wav,.flac,.m4a,.ogg,.opus,.aac,.wma"
        )
        raise typer.Exit(code=ExitCode.NO_VALID_INPUT)

    try:
        ordered_paths = order_track_paths(
            discovered,
            order=config.order,
            songs_folder=config.songs_folder,
            seed=config.seed,
        )
    except Exception as exc:
        typer.echo(f"Ordering error: {exc}")
        raise typer.Exit(code=ExitCode.ORDER_ERROR)

    track_sources: list[TrackSource] = []
    warnings: list[str] = []
    for idx, path in enumerate(ordered_paths):
        try:
            duration_ms = ffprobe_duration_ms(path, logger=logger)
            track_sources.append(TrackSource(id=f"t{idx}", path=path, duration_ms=duration_ms))
        except Exception as exc:
            msg = f"Skipping unreadable track {path.name}: {exc}"
            warnings.append(msg)
            logger.warning(msg)
    if not track_sources:
        typer.echo("No valid audio tracks remained after probing.")
        raise typer.Exit(code=ExitCode.NO_VALID_INPUT)

    analyses: dict[str, TrackAnalysis] = {}
    preset = get_preset(config.preset)
    for src in track_sources:
        result = analyze_track(
            track_id=src.id,
            path=src.path,
            duration_ms=src.duration_ms,
            target_lufs=config.lufs,
            smart_crossfade=config.smart_crossfade,
            smart_ordering=config.smart_ordering,
            logger=logger,
        )
        if config.adaptive_lofi:
            try:
                metrics = analyze_adaptive_metrics(
                    path=src.path,
                    duration_ms=src.duration_ms,
                    loudness=result.loudness,
                    logger=logger,
                )
                result.adaptive_metrics = metrics
                result.adaptive_processing = derive_adaptive_processing(
                    metrics=metrics,
                    preset=preset,
                    config=config,
                )
            except Exception as exc:
                warn = f"adaptive analysis failed for {src.path.name}: {exc}"
                result.warnings.append(warn)
                result.adaptive_processing = fallback_adaptive_processing(
                    preset=preset,
                    config=config,
                    warning=str(exc),
                )
        analyses[src.id] = result
        warnings.extend(result.warnings)

    if config.strict_analysis and warnings:
        typer.echo("Analysis produced warnings and strict mode is enabled.")
        raise typer.Exit(code=ExitCode.ANALYSIS_ERROR)

    if config.smart_ordering and config.smart_crossfade:
        reordered = order_sources_by_transition_fit(
            track_sources,
            analyses,
            mode=config.smart_ordering_mode,
        )
        if [src.id for src in reordered] != [src.id for src in track_sources]:
            track_sources = reordered
            logger.info("Smart ordering applied (%s).", config.smart_ordering_mode.value)

    instances = build_instances(
        base_tracks=track_sources,
        order=config.order,
        seed=config.seed,
        target_duration_min=config.target_duration_min,
        crossfade_sec=config.crossfade_sec,
    )
    mix_plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=config.crossfade_sec,
        smart_crossfade=config.smart_crossfade,
        target_duration_min=config.target_duration_min,
    )

    try:
        if len(mix_plan.instances) > config.chunk_threshold:
            _render_staged(
                full_plan=mix_plan,
                analyses=analyses,
                config=config,
                output_path=output_path,
                logger=logger,
            )
        else:
            _run_render(
                plan=mix_plan,
                analyses=analyses,
                config=config,
                output_path=output_path,
                include_master=True,
                include_rain=config.rain is not None,
                per_track_processing=True,
                logger=logger,
            )
    except CommandError as exc:
        logger.error("Render failed: %s", exc.stderr)
        typer.echo("Render failed. See logs for details.")
        raise typer.Exit(code=ExitCode.RENDER_ERROR)
    except Exception as exc:
        logger.error("Render failed: %s", exc)
        typer.echo(f"Render failed: {exc}")
        raise typer.Exit(code=ExitCode.RENDER_ERROR)

    adaptive_report_path: Optional[Path] = None
    processing_tracklist_path: Optional[Path] = None
    try:
        tracklists = write_tracklist_artifacts(mix_plan, analyses, output_path)
        if config.metadata_json:
            metadata = _collect_metadata(config, mix_plan, analyses, output_path, warnings)
            write_json(config.metadata_json, metadata)
        if config.adaptive_lofi:
            adaptive_report_path, processing_tracklist_path = _write_adaptive_artifacts(
                track_sources=track_sources,
                plan=mix_plan,
                analyses=analyses,
                adaptive_report_path=config.adaptive_report,
                output_path=output_path,
            )
    except Exception as exc:
        typer.echo(f"Failed to write output metadata files: {exc}")
        raise typer.Exit(code=ExitCode.OUTPUT_ERROR)

    typer.echo("Nightfall mix complete")
    typer.echo(f"Tracks in mix: {len(mix_plan.instances)}")
    typer.echo(f"Estimated duration: {format_hms(mix_plan.estimated_duration_ms)}")
    typer.echo(f"Output: {output_path}")
    typer.echo(f"Tracklist TXT: {tracklists.tracklist_txt_path}")
    typer.echo(f"Tracklist JSON: {tracklists.tracklist_json_path}")
    typer.echo(f"Timestamps TXT: {tracklists.timestamps_txt_path}")
    typer.echo(f"Timestamps CSV: {tracklists.timestamps_csv_path}")
    if config.adaptive_lofi:
        typer.echo(f"Adaptive Report: {adaptive_report_path}")
        typer.echo(f"Tracklist With Processing: {processing_tracklist_path}")
    if warnings:
        typer.echo(f"Warnings: {len(warnings)}")


def app_main() -> None:
    app()


if __name__ == "__main__":
    app()
