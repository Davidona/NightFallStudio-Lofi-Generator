from __future__ import annotations

import copy
import math
import logging
import os
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from nightfall_mix.analysis import (
    TrackAnalysis,
    analyze_adaptive_metrics,
    analyze_track,
    derive_adaptive_processing,
    fallback_adaptive_processing,
    read_analysis_cache_summary,
)
from nightfall_mix.config import OrderMode, OutputFormat, RunConfig
from nightfall_mix.effects_presets import PresetSpec, get_preset
from nightfall_mix.ffmpeg_graph import build_ffmpeg_command, build_filtergraph, write_filtergraph
from nightfall_mix.mixer import (
    MixPlan,
    TimelineEntry,
    TrackInstance,
    TrackSource,
    TransitionPlan,
    build_instances,
    build_mix_plan,
    discover_audio_files,
    order_sources_by_transition_fit,
)
from nightfall_mix.tracklists import write_tracklist_artifacts
from nightfall_mix.utils import (
    CommandError,
    ensure_dependencies,
    ffprobe_json,
    ffprobe_duration_ms,
    run_command_stream,
    run_command,
    seeded_random,
    write_json,
)
from nightfall_desktop.models.session_models import (
    EngineSessionModel,
    GuiSettings,
    PresetOverrides,
    RenderArtifactsModel,
)

LogCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[int, int], None]]
CancelCallback = Optional[Callable[[], bool]]


class GuiEngineService:
    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger("nightfall_desktop.engine")
        self.logger.setLevel(logging.DEBUG)
        self._active_cache_root: Optional[Path] = None

    def _emit_log(self, callback: LogCallback, message: str) -> None:
        self.logger.info(message)
        if callback:
            callback(message)

    def _build_run_config(self, settings: GuiSettings) -> RunConfig:
        return RunConfig(
            songs_folder=settings.songs_folder,
            output=settings.output_path,
            rain=settings.rain_path,
            order=OrderMode.random if settings.shuffle else OrderMode.alpha,
            seed=settings.seed,
            target_duration_min=settings.target_duration_min,
            crossfade_sec=settings.crossfade_sec,
            smart_crossfade=settings.smart_crossfade,
            smart_ordering=settings.smart_ordering,
            smart_ordering_mode=settings.smart_ordering_mode,
            lufs=settings.lufs,
            preset=settings.preset,
            metadata_tags=settings.metadata_tags,
            metadata_json=settings.metadata_json,
            quality_mode=settings.quality_mode,
            rain_level_db=settings.rain_level_db,
            mix_log=settings.mix_log,
            output_format=settings.output_format,
            bitrate=settings.bitrate,
            strict_analysis=settings.strict_analysis,
            adaptive_lofi=settings.adaptive_lofi,
            adaptive_report=settings.adaptive_report,
            adaptive_lpf_max_cut_hz=settings.adaptive_lpf_max_cut_hz,
            adaptive_noise_max_db=settings.adaptive_noise_max_db,
            adaptive_stereo_min_width=settings.adaptive_stereo_min_width,
            adaptive_centroid_threshold=settings.adaptive_centroid_threshold,
            adaptive_rolloff_threshold=settings.adaptive_rolloff_threshold,
            adaptive_crest_threshold_low=settings.adaptive_crest_threshold_low,
            adaptive_crest_threshold_high=settings.adaptive_crest_threshold_high,
        )

    def _resolve_cache_root(self, settings: GuiSettings) -> Path:
        cache_root = settings.cache_folder if settings.cache_folder is not None else Path(tempfile.gettempdir())
        cache_root = Path(cache_root)
        cache_root.mkdir(parents=True, exist_ok=True)
        return cache_root

    def _validate_rain_input(self, rain_path: Optional[Path]) -> tuple[bool, str]:
        if rain_path is None:
            return True, ""
        try:
            payload = ffprobe_json(rain_path, logger=self.logger)
        except Exception as exc:
            return False, f"rain input probe failed ({rain_path}): {exc}"
        streams = payload.get("streams", [])
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if not audio_streams:
            return False, f"rain input has no audio stream: {rain_path}"
        return True, ""

    def _preflight_rain_mix(self, session: EngineSessionModel, config: RunConfig) -> None:
        if config.rain is None or not session.mix_plan.instances:
            return

        td_kwargs: dict[str, str] = {"prefix": "nightfall_rain_probe_"}
        if self._active_cache_root is not None:
            td_kwargs["dir"] = str(self._active_cache_root)
        with tempfile.TemporaryDirectory(**td_kwargs) as td:
            probe_out = Path(td) / "rain_probe.w64"
            probe_src = session.mix_plan.instances[0].track.path
            probe_sec = max(4.0, min(12.0, session.mix_plan.instances[0].track.duration_ms / 1000.0))
            filtergraph = (
                f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"aresample=48000,atrim=duration={probe_sec:.3f}[main];"
                f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"aresample=48000,highpass=f=200,lowpass=f=11000,"
                f"volume={config.rain_level_db}dB,asetpts=PTS-STARTPTS,"
                f"atrim=duration={probe_sec:.3f}[rain];"
                "[main][rain]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[outa]"
            )
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(probe_src),
                "-stream_loop",
                "-1",
                "-i",
                str(config.rain),
                "-filter_complex",
                filtergraph,
                "-map",
                "[outa]",
                "-vn",
                "-sn",
                "-c:a",
                "pcm_s16le",
                str(probe_out),
            ]
            try:
                run_command(cmd, logger=self.logger)
            except CommandError as exc:
                detail = (exc.stderr or str(exc)).strip()
                raise RuntimeError(
                    "Rain preflight failed. The selected rain file is not compatible with the current mix chain. "
                    f"Details: {detail}"
                ) from exc
            state = self._output_audibility_state(
                output_path=probe_out,
                sample_duration_sec=2.0,
                deep_probe=False,
            )
            if state != "audible":
                raise RuntimeError(
                    "Rain preflight produced non-audible output. "
                    "Choose a different rain file or adjust rain settings, then render again."
                )

    @staticmethod
    def _pcm_bytes_per_second() -> float:
        # 48kHz stereo 16-bit PCM
        return float(48_000 * 2 * 2)

    @staticmethod
    def _bitrate_to_bps(raw_bitrate: str) -> int:
        text = (raw_bitrate or "").strip().lower()
        match = re.fullmatch(r"(\d+)\s*k", text)
        if match:
            return int(match.group(1)) * 1000
        return 192_000

    def estimate_render_storage(self, session: EngineSessionModel, settings: GuiSettings) -> dict[str, object]:
        config = self._build_run_config(settings)
        cache_root = self._resolve_cache_root(settings)
        output_path = config.resolved_output_path()
        output_parent = output_path.parent if output_path.parent != Path("") else Path(".")
        output_parent.mkdir(parents=True, exist_ok=True)

        timeline_sec = max(1.0, session.mix_plan.estimated_duration_ms / 1000.0)
        if settings.preview_mode:
            render_sec = max(10.0, float(settings.preview_duration_sec))
        else:
            render_sec = timeline_sec

        max_track_sec = 1.0
        if session.mix_plan.instances:
            max_track_sec = max(1.0, max(inst.track.duration_ms for inst in session.mix_plan.instances) / 1000.0)

        pcm_bps = self._pcm_bytes_per_second()
        # Rolling merge keeps at most ~2 full-size intermediates + one track chunk.
        estimated_intermediate = (timeline_sec * pcm_bps * 2.2) + (max_track_sec * pcm_bps * 1.1)

        if config.resolve_output_format() == OutputFormat.wav:
            estimated_output = render_sec * pcm_bps
        else:
            estimated_output = render_sec * (self._bitrate_to_bps(config.bitrate) / 8.0)

        # Safety margin for ffmpeg temp/log overhead and filesystem metadata.
        safety_margin = 512 * 1024 * 1024
        required_cache_bytes = int(max(1.0, estimated_intermediate + estimated_output + safety_margin))

        cache_free = shutil.disk_usage(cache_root).free
        output_free = shutil.disk_usage(output_parent).free
        return {
            "cache_root": cache_root,
            "required_cache_bytes": required_cache_bytes,
            "available_cache_bytes": int(cache_free),
            "output_root": output_parent,
            "available_output_bytes": int(output_free),
            "estimated_output_bytes": int(max(1.0, estimated_output)),
            "timeline_seconds": float(timeline_sec),
            "render_seconds": float(render_sec),
        }

    def _resolve_preset(self, settings: GuiSettings) -> PresetSpec:
        base = get_preset(settings.preset)
        overrides: PresetOverrides = settings.preset_overrides
        if overrides.lpf_hz is None:
            lpf_hz = base.lpf_hz
        else:
            lpf_hz = max(4000.0, min(18000.0, overrides.lpf_hz))
        sat_scale = max(0.3, min(1.5, overrides.saturation_scale))
        comp_scale = max(0.3, min(1.5, overrides.compression_scale))
        softclip_threshold = 1.0 - (1.0 - base.softclip_threshold) * sat_scale
        softclip_threshold = max(0.9, min(0.999, softclip_threshold))
        comp_ratio = 1.0 + (base.comp_ratio - 1.0) * comp_scale
        comp_threshold = base.comp_threshold_db + (1.0 - comp_scale) * 3.0
        return replace(
            base,
            lpf_hz=lpf_hz,
            softclip_threshold=softclip_threshold,
            comp_ratio=comp_ratio,
            comp_threshold_db=comp_threshold,
        )

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(path.resolve()).casefold()

    def _resolve_initial_paths(self, config: RunConfig, ordered_paths: Optional[list[Path]]) -> list[Path]:
        discovered = discover_audio_files(config.songs_folder)
        excluded_keys: set[str] = set()
        output_candidates: list[Path] = []
        try:
            output_candidates.append(config.output)
        except Exception:
            pass
        try:
            output_candidates.append(config.resolved_output_path())
        except Exception:
            pass
        for path in list(output_candidates):
            try:
                output_candidates.append(path.with_suffix(".mp3"))
                output_candidates.append(path.with_suffix(".wav"))
            except Exception:
                continue
        for candidate in output_candidates:
            try:
                excluded_keys.add(self._path_key(candidate))
            except Exception:
                continue
        if config.rain is not None:
            try:
                excluded_keys.add(self._path_key(config.rain))
            except Exception:
                pass
        if excluded_keys:
            before = len(discovered)
            discovered = [p for p in discovered if self._path_key(p) not in excluded_keys]
            removed = before - len(discovered)
            if removed > 0:
                self.logger.info("Excluded %d non-source audio file(s) from analysis input set.", removed)
        if not discovered:
            raise RuntimeError("No supported audio files found in selected folder")

        if ordered_paths:
            discovered_by_key = {str(p.resolve()).casefold(): p for p in discovered}
            ordered: list[Path] = []
            for path in ordered_paths:
                key = str(path.resolve()).casefold()
                if key in discovered_by_key:
                    ordered.append(discovered_by_key[key])
            if ordered:
                return ordered

        if config.order == OrderMode.random:
            shuffled = list(discovered)
            rng = seeded_random(config.seed)
            rng.shuffle(shuffled)
            return shuffled
        return sorted(discovered, key=lambda p: p.name.casefold())

    def _build_plan_from_sources(
        self,
        sources: list[TrackSource],
        analyses: dict[str, TrackAnalysis],
        config: RunConfig,
    ) -> MixPlan:
        instances = build_instances(
            base_tracks=sources,
            order=config.order,
            seed=config.seed,
            target_duration_min=config.target_duration_min,
            crossfade_sec=config.crossfade_sec,
        )
        return build_mix_plan(
            instances=instances,
            analyses=analyses,
            crossfade_sec=config.crossfade_sec,
            smart_crossfade=config.smart_crossfade,
            target_duration_min=config.target_duration_min,
        )

    def analyze_folder(
        self,
        settings: GuiSettings,
        ordered_paths: Optional[list[Path]] = None,
        on_log: LogCallback = None,
        on_progress: ProgressCallback = None,
        should_cancel: CancelCallback = None,
    ) -> EngineSessionModel:
        config = self._build_run_config(settings)
        ensure_dependencies(self.logger)
        ordered = self._resolve_initial_paths(config, ordered_paths)
        self._emit_log(on_log, f"Analyzing {len(ordered)} tracks...")

        track_sources: list[TrackSource] = []
        warnings: list[str] = []
        for idx, path in enumerate(ordered):
            if should_cancel and should_cancel():
                raise RuntimeError("Analysis cancelled")
            try:
                duration_ms = ffprobe_duration_ms(path, logger=self.logger)
            except Exception as exc:
                warnings.append(f"Skipping {path.name}: {exc}")
                continue
            track_sources.append(TrackSource(id=f"t{idx}", path=path, duration_ms=duration_ms))

        if not track_sources:
            raise RuntimeError("No valid audio tracks remained after probing")

        analyses: dict[str, TrackAnalysis] = {}
        preset = self._resolve_preset(settings)
        total = len(track_sources)
        cached_track_count = 0
        for idx, source in enumerate(track_sources, start=1):
            if should_cancel and should_cancel():
                raise RuntimeError("Analysis cancelled")
            if read_analysis_cache_summary(source.path) is not None:
                cached_track_count += 1
            analysis = analyze_track(
                track_id=source.id,
                path=source.path,
                duration_ms=source.duration_ms,
                target_lufs=config.lufs,
                smart_crossfade=config.smart_crossfade,
                smart_ordering=config.smart_ordering,
                logger=self.logger,
            )
            if config.adaptive_lofi:
                try:
                    metrics = analyze_adaptive_metrics(
                        path=source.path,
                        duration_ms=source.duration_ms,
                        loudness=analysis.loudness,
                        logger=self.logger,
                    )
                    analysis.adaptive_metrics = metrics
                    analysis.adaptive_processing = derive_adaptive_processing(
                        metrics=metrics,
                        preset=preset,
                        config=config,
                    )
                except Exception as exc:
                    warning = f"adaptive analysis failed for {source.path.name}: {exc}"
                    analysis.warnings.append(warning)
                    analysis.adaptive_processing = fallback_adaptive_processing(
                        preset=preset,
                        config=config,
                        warning=str(exc),
                    )
            analyses[source.id] = analysis
            warnings.extend(analysis.warnings)
            if on_progress:
                on_progress(idx, total)

        if cached_track_count > 0:
            self._emit_log(
                on_log,
                f"Analysis cache detected for {cached_track_count}/{total} tracks; reused where valid.",
            )

        if settings.smart_ordering and settings.smart_crossfade:
            reordered = order_sources_by_transition_fit(
                track_sources,
                analyses,
                mode=settings.smart_ordering_mode,
            )
            if [src.id for src in reordered] != [src.id for src in track_sources]:
                track_sources = reordered
                self._emit_log(
                    on_log,
                    f"Smart ordering applied ({settings.smart_ordering_mode.value}).",
                )

        mix_plan = self._build_plan_from_sources(track_sources, analyses, config=config)
        self._emit_log(on_log, f"Analysis complete ({len(track_sources)} tracks).")

        return EngineSessionModel(
            settings=settings,
            ordered_paths=[s.path for s in track_sources],
            track_sources=track_sources,
            analyses=analyses,
            mix_plan=mix_plan,
            warnings=warnings,
        )

    def rebuild_plan(
        self,
        session: EngineSessionModel,
        settings: GuiSettings,
        ordered_paths: list[Path],
    ) -> EngineSessionModel:
        config = self._build_run_config(settings)
        source_by_path = {str(src.path.resolve()).casefold(): src for src in session.track_sources}
        analysis_by_path = {
            str(src.path.resolve()).casefold(): session.analyses.get(src.id)
            for src in session.track_sources
        }

        rebuilt_sources: list[TrackSource] = []
        rebuilt_analyses: dict[str, TrackAnalysis] = {}
        for idx, path in enumerate(ordered_paths):
            key = str(path.resolve()).casefold()
            source = source_by_path.get(key)
            if source is None:
                continue
            new_id = f"t{idx}"
            rebuilt_sources.append(
                TrackSource(
                    id=new_id,
                    path=source.path,
                    duration_ms=source.duration_ms,
                    sample_rate=source.sample_rate,
                    channels=source.channels,
                )
            )
            old_analysis = analysis_by_path.get(key)
            if old_analysis is not None:
                copied = copy.deepcopy(old_analysis)
                copied.track_id = new_id
                rebuilt_analyses[new_id] = copied

        if not rebuilt_sources:
            raise RuntimeError("No tracks available after reorder")

        mix_plan = self._build_plan_from_sources(rebuilt_sources, rebuilt_analyses, config=config)
        return EngineSessionModel(
            settings=settings,
            ordered_paths=[s.path for s in rebuilt_sources],
            track_sources=rebuilt_sources,
            analyses=rebuilt_analyses,
            mix_plan=mix_plan,
            warnings=list(session.warnings),
        )

    def _adaptive_metrics_payload(self, analysis: TrackAnalysis) -> dict:
        metrics = analysis.adaptive_metrics
        return {
            "lufs": metrics.lufs if metrics else analysis.loudness.input_i,
            "crest_factor_db": metrics.crest_factor_db if metrics else None,
            "spectral_centroid_hz": metrics.spectral_centroid_hz if metrics else None,
            "rolloff_hz": metrics.rolloff_hz if metrics else None,
            "stereo_width": metrics.stereo_width if metrics else None,
            "noise_floor_dbfs": metrics.noise_floor_dbfs if metrics else None,
        }

    def _adaptive_processing_payload(self, analysis: TrackAnalysis) -> dict:
        proc = analysis.adaptive_processing
        return {
            "lpf_cutoff_hz": proc.lpf_cutoff_hz if proc else None,
            "saturation_strength": proc.saturation_strength if proc else None,
            "compression_strength": proc.compression_strength if proc else None,
            "stereo_width_target": proc.stereo_width_target if proc else None,
            "noise_added_db": proc.noise_added_db if proc else None,
            "lofi_needed_score": proc.lofi_needed_score if proc else None,
        }

    def _write_adaptive_outputs(
        self,
        session: EngineSessionModel,
        adaptive_report_path: Path,
        output_path: Path,
    ) -> tuple[Path, Path]:
        report = []
        source_by_id = {src.id: src for src in session.track_sources}
        ordered_track_ids: list[str] = []
        seen: set[str] = set()
        for entry in session.mix_plan.timeline:
            if entry.track_id in seen:
                continue
            seen.add(entry.track_id)
            ordered_track_ids.append(entry.track_id)

        for track_id in ordered_track_ids:
            analysis = session.analyses.get(track_id)
            if analysis is None:
                continue
            src = source_by_id.get(track_id)
            if src is None:
                continue
            report.append(
                {
                    "track": src.path.name,
                    "metrics": self._adaptive_metrics_payload(analysis),
                    "applied_processing": self._adaptive_processing_payload(analysis),
                    "rationale": (
                        analysis.adaptive_processing.rationale
                        if analysis.adaptive_processing
                        else "adaptive processing unavailable"
                    ),
                }
            )
        write_json(adaptive_report_path, report)

        processing_path = output_path.with_name("tracklist_with_processing.txt")
        with processing_path.open("w", encoding="utf-8") as f:
            for idx, entry in enumerate(session.mix_plan.timeline, start=1):
                analysis = session.analyses.get(entry.track_id)
                rationale = (
                    analysis.adaptive_processing.rationale
                    if analysis and analysis.adaptive_processing
                    else "adaptive fallback"
                )
                f.write(f"{idx}. {entry.filename} -- {rationale}\n")
        return adaptive_report_path, processing_path

    def export_adaptive_report(self, session: EngineSessionModel, settings: GuiSettings) -> tuple[Path, Path]:
        config = self._build_run_config(settings)
        output_path = config.resolved_output_path()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return self._write_adaptive_outputs(
            session=session,
            adaptive_report_path=config.adaptive_report,
            output_path=output_path,
        )

    def _write_metadata(self, session: EngineSessionModel, config: RunConfig) -> Optional[Path]:
        if config.metadata_json is None:
            return None
        payload = {
            "track_count": len(session.mix_plan.instances),
            "estimated_duration_ms": session.mix_plan.estimated_duration_ms,
            "config": config.model_dump(mode="json"),
            "warnings": session.warnings,
            "analysis": {
                track_id: {
                    "bpm": a.bpm,
                    "bpm_confidence": a.bpm_confidence,
                    "key": a.key,
                    "key_confidence": a.key_confidence,
                    "adaptive_metrics": self._adaptive_metrics_payload(a) if a.adaptive_metrics else None,
                    "adaptive_processing": self._adaptive_processing_payload(a)
                    if a.adaptive_processing
                    else None,
                }
                for track_id, a in session.analyses.items()
            },
        }
        write_json(config.metadata_json, payload)
        return config.metadata_json

    def _preview_ordered_paths(
        self,
        session: EngineSessionModel,
        preview_duration_sec: float,
    ) -> list[Path]:
        if len(session.mix_plan.timeline) <= 2:
            return list(session.ordered_paths)

        first_transition_ms = session.mix_plan.timeline[1].start_time_ms
        preview_ms = int(max(10.0, preview_duration_sec) * 1000)
        window_start_ms = max(0, first_transition_ms - (preview_ms // 2))
        window_end_ms = window_start_ms + preview_ms

        last_needed_idx = 1
        for idx, entry in enumerate(session.mix_plan.timeline):
            if entry.end_time_ms >= window_end_ms:
                last_needed_idx = max(1, idx)
                break
            last_needed_idx = idx

        keep_count = min(len(session.ordered_paths), max(2, last_needed_idx + 1))
        return session.ordered_paths[:keep_count]

    @staticmethod
    def _is_memory_allocation_failure(error_text: str) -> bool:
        normalized = (error_text or "").casefold()
        return any(
            marker in normalized
            for marker in (
                "cannot allocate memory",
                "error code: -12",
                "error while filtering: cannot allocate memory",
                "return code -12",
            )
        )

    def _run_render_plan(
        self,
        mix_plan: MixPlan,
        analyses: dict[str, TrackAnalysis],
        config: RunConfig,
        preset: PresetSpec,
        output_path: Path,
        include_master: bool,
        include_rain: bool,
        per_track_processing: bool,
        preview_start_sec: Optional[float],
        preview_duration_sec: Optional[float],
        on_log: LogCallback,
        on_progress: ProgressCallback,
        should_cancel: CancelCallback,
        stage_label: str = "render",
        safe_mastering: bool = False,
    ) -> None:
        td_kwargs: dict[str, str] = {"prefix": "nightfall_gui_graph_"}
        if self._active_cache_root is not None:
            td_kwargs["dir"] = str(self._active_cache_root)
        with tempfile.TemporaryDirectory(**td_kwargs) as td:
            graph_path = Path(td) / "filtergraph.txt"
            graph = build_filtergraph(
                mix_plan=mix_plan,
                analyses=analyses,
                config=config,
                preset=preset,
                include_master=include_master,
                include_rain=include_rain,
                per_track_processing=per_track_processing,
                preview_start_sec=preview_start_sec,
                preview_duration_sec=preview_duration_sec,
                safe_mastering=safe_mastering,
            )
            write_filtergraph(graph_path, graph)
            command = build_ffmpeg_command(
                mix_plan=mix_plan,
                config=config,
                filter_script_path=graph_path,
                output_path=output_path,
                include_rain=include_rain,
            )
            total_ms = int((preview_duration_sec or (mix_plan.estimated_duration_ms / 1000.0)) * 1000)
            total_ms = max(1, total_ms)

            def on_stdout(line: str) -> None:
                if line.startswith("out_time_ms="):
                    val = line.split("=", 1)[1].strip()
                    if val.isdigit() and on_progress:
                        current = min(total_ms, int(int(val) / 1000))
                        on_progress(current, total_ms)
                elif line == "progress=end" and on_progress:
                    on_progress(total_ms, total_ms)

            def on_stderr(line: str) -> None:
                if line:
                    self._emit_log(on_log, line)

            try:
                run_command_stream(
                    command,
                    logger=self.logger,
                    on_stdout_line=on_stdout,
                    on_stderr_line=on_stderr,
                    should_cancel=should_cancel,
                )
            except CommandError as exc:
                debug_dir = self._persist_render_debug_artifacts(
                    stage_label=stage_label,
                    command=command,
                    graph=graph,
                    stderr=exc.stderr or str(exc),
                )
                self._emit_log(on_log, f"{stage_label} failed. Debug artifacts: {debug_dir}")
                raise

    def _render_staged_fallback(
        self,
        session: EngineSessionModel,
        config: RunConfig,
        preset: PresetSpec,
        output_path: Path,
        preview_start_sec: Optional[float],
        preview_duration_sec: Optional[float],
        chunk_per_track_processing: bool,
        on_log: LogCallback,
        on_progress: ProgressCallback,
        should_cancel: CancelCallback,
    ) -> set[int]:
        instances = list(session.mix_plan.instances)
        if not instances:
            raise RuntimeError("Cannot render staged fallback with no track instances.")
        transitions = list(session.mix_plan.transitions)
        total_steps = max(1, len(instances) + max(0, len(instances) - 1) + 1)
        step = 0
        degraded_transition_indices: set[int] = set()

        td_kwargs: dict[str, str] = {"prefix": "nightfall_gui_staged_"}
        if self._active_cache_root is not None:
            td_kwargs["dir"] = str(self._active_cache_root)
        with tempfile.TemporaryDirectory(**td_kwargs) as td:
            temp_root = Path(td)
            wav_cfg = config.model_copy(update={"output_format": OutputFormat.wav, "rain": None})

            self._emit_log(on_log, "Bounded render: processing tracks individually")
            current_path: Optional[Path] = None
            current_duration_ms: Optional[int] = None
            current_is_temp = False

            for idx, instance in enumerate(instances):
                if should_cancel and should_cancel():
                    raise RuntimeError("Render cancelled")

                stage_prefix = f"track {idx + 1}/{len(instances)}"
                src = TrackSource(
                    id=instance.track.id,
                    path=instance.track.path,
                    duration_ms=instance.track.duration_ms,
                )
                proc_plan = build_mix_plan(
                    instances=[TrackInstance(instance_index=0, track=src, cycle_index=0)],
                    analyses=session.analyses,
                    crossfade_sec=config.crossfade_sec,
                    smart_crossfade=False,
                    target_duration_min=None,
                )
                proc_out = temp_root / f"proc_{idx:03d}.w64"
                self._emit_log(on_log, f"Bounded render: {stage_prefix}")
                try:
                    self._run_render_plan(
                        mix_plan=proc_plan,
                        analyses=session.analyses,
                        config=wav_cfg,
                        preset=preset,
                        output_path=proc_out,
                        include_master=False,
                        include_rain=False,
                        per_track_processing=chunk_per_track_processing,
                        preview_start_sec=None,
                        preview_duration_sec=None,
                        on_log=on_log,
                        on_progress=None,
                        should_cancel=should_cancel,
                        stage_label=f"bounded process {stage_prefix}",
                    )
                except CommandError as exc:
                    if chunk_per_track_processing:
                        if self._is_memory_allocation_failure(exc.stderr or ""):
                            self._emit_log(
                                on_log,
                                f"Bounded render: {stage_prefix} hit ffmpeg resource limits; retrying decode-only.",
                            )
                        else:
                            self._emit_log(
                                on_log,
                                f"Bounded render: {stage_prefix} DSP pass failed; retrying decode-only.",
                            )
                        try:
                            self._run_render_plan(
                                mix_plan=proc_plan,
                                analyses={},
                                config=wav_cfg,
                                preset=preset,
                                output_path=proc_out,
                                include_master=False,
                                include_rain=False,
                                per_track_processing=False,
                                preview_start_sec=None,
                                preview_duration_sec=None,
                                on_log=on_log,
                                on_progress=None,
                                should_cancel=should_cancel,
                                stage_label=f"bounded process safe {stage_prefix}",
                            )
                        except CommandError as safe_exc:
                            raise RuntimeError(self._render_stage_error(stage_prefix, safe_exc)) from safe_exc
                    else:
                        raise RuntimeError(self._render_stage_error(stage_prefix, exc)) from exc

                if chunk_per_track_processing:
                    proc_state = self._output_audibility_state(
                        output_path=proc_out,
                        sample_duration_sec=1.5,
                        deep_probe=False,
                    )
                    if proc_state != "audible":
                        self._emit_log(
                            on_log,
                            f"Bounded render: {stage_prefix} output is {proc_state}; retrying decode-only.",
                        )
                        try:
                            self._run_render_plan(
                                mix_plan=proc_plan,
                                analyses={},
                                config=wav_cfg,
                                preset=preset,
                                output_path=proc_out,
                                include_master=False,
                                include_rain=False,
                                per_track_processing=False,
                                preview_start_sec=None,
                                preview_duration_sec=None,
                                on_log=on_log,
                                on_progress=None,
                                should_cancel=should_cancel,
                                stage_label=f"bounded process safe-audibility {stage_prefix}",
                            )
                        except CommandError as safe_exc:
                            raise RuntimeError(self._render_stage_error(stage_prefix, safe_exc)) from safe_exc

                        proc_state = self._output_audibility_state(
                            output_path=proc_out,
                            sample_duration_sec=1.5,
                            deep_probe=False,
                        )
                        if proc_state != "audible":
                            raise RuntimeError(
                                f"{stage_prefix} produced {proc_state} output after safe retry."
                            )

                try:
                    proc_duration_ms = ffprobe_duration_ms(proc_out, logger=self.logger)
                except Exception as exc:
                    raise RuntimeError(
                        f"{stage_prefix} output is unreadable by ffprobe: {exc}"
                    ) from exc

                step += 1
                if on_progress:
                    on_progress(step * 1000, total_steps * 1000)

                if current_path is None:
                    current_path = proc_out
                    current_duration_ms = proc_duration_ms
                    current_is_temp = True
                    continue

                transition_idx = idx - 1
                transition = transitions[transition_idx] if transition_idx < len(transitions) else None
                crossfade_sec = (
                    max(0.5, (transition.crossfade_ms / 1000.0))
                    if transition is not None
                    else config.crossfade_sec
                )
                left_src = TrackSource(
                    id=f"fold_left_{transition_idx}",
                    path=current_path,
                    duration_ms=max(1, int(current_duration_ms or 1)),
                )
                right_src = TrackSource(
                    id=f"fold_right_{transition_idx}",
                    path=proc_out,
                    duration_ms=proc_duration_ms,
                )
                fold_plan = build_mix_plan(
                    instances=[
                        TrackInstance(instance_index=0, track=left_src, cycle_index=0),
                        TrackInstance(instance_index=1, track=right_src, cycle_index=0),
                    ],
                    analyses={},
                    crossfade_sec=crossfade_sec,
                    smart_crossfade=False,
                    target_duration_min=None,
                )
                fold_out = temp_root / f"fold_{transition_idx + 1:03d}.w64"
                merge_stage = f"merge {transition_idx + 1}/{len(instances) - 1}"
                self._emit_log(on_log, f"Bounded render: {merge_stage}")
                merge_failed = False
                try:
                    self._run_render_plan(
                        mix_plan=fold_plan,
                        analyses={},
                        config=wav_cfg,
                        preset=preset,
                        output_path=fold_out,
                        include_master=False,
                        include_rain=False,
                        per_track_processing=False,
                        preview_start_sec=None,
                        preview_duration_sec=None,
                        on_log=on_log,
                        on_progress=None,
                        should_cancel=should_cancel,
                        stage_label=f"bounded {merge_stage}",
                    )
                except CommandError as exc:
                    merge_failed = True
                    if self._is_memory_allocation_failure(exc.stderr or ""):
                        self._emit_log(
                            on_log,
                            f"Bounded render: {merge_stage} hit ffmpeg resource limits; "
                            "falling back to no-crossfade join.",
                        )
                    else:
                        self._emit_log(
                            on_log,
                            f"Bounded render: {merge_stage} failed; falling back to no-crossfade join.",
                        )

                merge_state = self._output_audibility_state(
                    output_path=fold_out,
                    sample_duration_sec=1.5,
                    deep_probe=False,
                )
                if merge_failed or merge_state != "audible":
                    if merge_failed and merge_state == "audible":
                        self._emit_log(
                            on_log,
                            f"Bounded render: {merge_stage} command failed but produced a partial file; "
                            "rebuilding this boundary with concat-safe join.",
                        )
                    else:
                        self._emit_log(
                            on_log,
                            f"Bounded render: {merge_stage} produced {merge_state}; "
                            "retrying with concat-safe join (crossfade disabled for this transition).",
                        )
                    try:
                        self._concat_audio_files([current_path, proc_out], fold_out)
                    except Exception as exc:
                        raise RuntimeError(f"{merge_stage} fallback join failed: {exc}") from exc
                    merge_state = self._output_audibility_state(
                        output_path=fold_out,
                        sample_duration_sec=1.5,
                        deep_probe=False,
                    )
                    degraded_transition_indices.add(transition_idx)
                    if merge_state != "audible":
                        raise RuntimeError(
                            f"{merge_stage} fallback join produced {merge_state} output."
                        )

                if current_is_temp:
                    self._safe_unlink(current_path)
                self._safe_unlink(proc_out)

                current_path = fold_out
                current_is_temp = True
                try:
                    current_duration_ms = ffprobe_duration_ms(current_path, logger=self.logger)
                except Exception as exc:
                    raise RuntimeError(f"{merge_stage} output is unreadable by ffprobe: {exc}") from exc
                step += 1
                if on_progress:
                    on_progress(step * 1000, total_steps * 1000)

            assert current_path is not None
            assert current_duration_ms is not None
            merged_out = current_path
            final_src = TrackSource(id="merged", path=merged_out, duration_ms=current_duration_ms)
            final_instances = [TrackInstance(instance_index=0, track=final_src, cycle_index=0)]
            final_plan = build_mix_plan(
                instances=final_instances,
                analyses={},
                crossfade_sec=config.crossfade_sec,
                smart_crossfade=False,
                target_duration_min=None,
            )
            self._emit_log(on_log, "Bounded render: applying final master chain")
            final_sec = max(1.0, final_plan.estimated_duration_ms / 1000.0)
            self._emit_log(
                on_log,
                f"Bounded render: final master pass length ~{final_sec:.0f}s (progress should continue).",
            )
            final_preset = preset
            if on_progress:
                on_progress(step * 1000, total_steps * 1000)

            def _on_final_master_progress(current_ms: int, total_ms: int) -> None:
                if not on_progress:
                    return
                safe_total = max(1, int(total_ms))
                frac = max(0.0, min(1.0, float(current_ms) / float(safe_total)))
                current_units = int((step + frac) * 1000)
                total_units = total_steps * 1000
                on_progress(current_units, total_units)

            def _run_final_master_variant(
                *,
                label: str,
                include_master_variant: bool,
                safe_mastering_variant: bool,
                variant_preset: PresetSpec,
                allow_wow_fallback: bool,
            ) -> tuple[str, str]:
                active_variant_preset = variant_preset
                try:
                    self._run_render_plan(
                        mix_plan=final_plan,
                        analyses={},
                        config=config,
                        preset=active_variant_preset,
                        output_path=output_path,
                        include_master=include_master_variant,
                        include_rain=config.rain is not None,
                        per_track_processing=False,
                        preview_start_sec=preview_start_sec,
                        preview_duration_sec=preview_duration_sec,
                        on_log=on_log,
                        on_progress=_on_final_master_progress,
                        should_cancel=should_cancel,
                        stage_label=f"bounded final master {label}",
                        safe_mastering=safe_mastering_variant,
                    )
                except CommandError as exc:
                    err = (exc.stderr or "").lower()
                    if (
                        allow_wow_fallback
                        and "no such filter" in err
                        and "vibrato" in err
                        and active_variant_preset.wow_depth > 0
                    ):
                        self._emit_log(
                            on_log,
                            "vibrato unsupported by ffmpeg in bounded pass; retrying this variant without wow/flutter",
                        )
                        active_variant_preset = replace(active_variant_preset, wow_depth=0.0, wow_rate_hz=0.0)
                        self._run_render_plan(
                            mix_plan=final_plan,
                            analyses={},
                            config=config,
                            preset=active_variant_preset,
                            output_path=output_path,
                            include_master=include_master_variant,
                            include_rain=config.rain is not None,
                            per_track_processing=False,
                            preview_start_sec=preview_start_sec,
                            preview_duration_sec=preview_duration_sec,
                            on_log=on_log,
                            on_progress=_on_final_master_progress,
                            should_cancel=should_cancel,
                            stage_label=f"bounded final master {label} no-wow",
                            safe_mastering=safe_mastering_variant,
                        )
                    else:
                        raise RuntimeError(self._render_stage_error(f"final master {label}", exc)) from exc
                state, diag = self._final_output_audibility_state(output_path=output_path)
                self._emit_log(on_log, f"Final master variant '{label}' audibility: {state} ({diag})")
                return state, diag

            # Use stable (no final loudnorm) mastering as primary.
            # Track-level chains already apply loudness conditioning; this avoids non-deterministic
            # final loudnorm behavior observed on long/rain mixes.
            safe_preset = replace(final_preset, wow_depth=0.0, wow_rate_hz=0.0)
            state, diag = _run_final_master_variant(
                label="stable-master",
                include_master_variant=True,
                safe_mastering_variant=True,
                variant_preset=safe_preset,
                allow_wow_fallback=False,
            )
            if state != "audible":
                self._emit_log(
                    on_log,
                    "Bounded render: stable final master was non-audible; retrying passthrough finalization.",
                )
                state, diag = _run_final_master_variant(
                    label="passthrough",
                    include_master_variant=False,
                    safe_mastering_variant=False,
                    variant_preset=safe_preset,
                    allow_wow_fallback=False,
                )
            if state != "audible":
                raise RuntimeError(
                    "Final master variants produced non-audible output. "
                    f"Diagnostics: {diag}"
                )
            step += 1
            if on_progress:
                on_progress(step * 1000, total_steps * 1000)
            if current_is_temp:
                self._safe_unlink(current_path)
        return degraded_transition_indices

    def _persist_render_debug_artifacts(
        self,
        stage_label: str,
        command: list[str],
        graph: str,
        stderr: str,
    ) -> Path:
        root = self._active_cache_root if self._active_cache_root is not None else Path(tempfile.gettempdir())
        debug_root = root / "nightfall_render_debug"
        debug_root.mkdir(parents=True, exist_ok=True)
        safe_stage = re.sub(r"[^a-z0-9_-]+", "_", stage_label.casefold()).strip("_") or "render"
        debug_dir = Path(tempfile.mkdtemp(prefix=f"{safe_stage}_", dir=str(debug_root)))
        (debug_dir / "command.txt").write_text("\n".join(command), encoding="utf-8")
        (debug_dir / "filtergraph.txt").write_text(graph, encoding="utf-8")
        (debug_dir / "stderr.txt").write_text(stderr or "", encoding="utf-8")
        return debug_dir

    def _render_stage_error(self, stage_label: str, exc: CommandError) -> str:
        detail = (exc.stderr or str(exc) or "").strip()
        if self._is_memory_allocation_failure(detail):
            return f"{stage_label} failed due to ffmpeg resource exhaustion. {detail}"
        return f"{stage_label} failed: {detail}"

    def _safe_unlink(self, path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            self.logger.debug("Failed to remove temp file %s: %s", path, exc)

    def _output_audibility_state(
        self,
        output_path: Path,
        sample_duration_sec: float = 6.0,
        deep_probe: bool = True,
    ) -> str:
        try:
            duration_sec = max(1.0, ffprobe_duration_ms(output_path, logger=self.logger) / 1000.0)
        except Exception as exc:
            self.logger.warning("Audibility probe failed to read duration for %s: %s", output_path, exc)
            return "unknown"

        offsets = [0.0]
        if duration_sec > max(10.0, sample_duration_sec * 2.0):
            offsets.append(max(0.0, (duration_sec * 0.5) - (sample_duration_sec * 0.5)))
        if deep_probe and duration_sec > 120.0:
            offsets.append(max(0.0, (duration_sec * 0.5) - (sample_duration_sec * 0.5)))
        if deep_probe and duration_sec > 1800.0:
            offsets.append(max(0.0, duration_sec - sample_duration_sec - 5.0))
        # Keep deterministic ordering and avoid duplicate probes.
        offsets = sorted(set(round(x, 3) for x in offsets))

        measured_any = False
        for offset in offsets:
            mean_db, max_db = self._probe_volume_stats(
                output_path=output_path,
                offset_sec=float(offset),
                sample_duration_sec=sample_duration_sec,
            )
            if mean_db is None and max_db is None:
                continue
            measured_any = True
            # Treat as audible only if not near-silent by both peak and mean energy.
            if max_db is not None and max_db > -28.0:
                return "audible"
            if mean_db is not None and mean_db > -58.0 and (max_db is None or max_db > -40.0):
                return "audible"
        if not measured_any:
            return "unknown"
        return "silent"

    def _probe_volume_stats(
        self,
        output_path: Path,
        offset_sec: float,
        sample_duration_sec: float,
    ) -> tuple[Optional[float], Optional[float]]:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "info",
            "-ss",
            f"{offset_sec:.3f}",
            "-t",
            f"{sample_duration_sec:.3f}",
            "-i",
            str(output_path),
            "-vn",
            "-sn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "NUL" if os.name == "nt" else "/dev/null",
        ]
        try:
            result = run_command(cmd, logger=self.logger)
        except Exception:
            return None, None
        text = "\n".join(part for part in ((result.stdout or ""), (result.stderr or "")) if part)
        mean_match = re.search(r"mean_volume:\s*([-\w\.]+)\s*dB", text, flags=re.IGNORECASE)
        max_match = re.search(r"max_volume:\s*([-\w\.]+)\s*dB", text, flags=re.IGNORECASE)

        mean_db: Optional[float] = None
        if mean_match:
            raw_mean = mean_match.group(1).strip().lower()
            if raw_mean != "-inf":
                try:
                    mean_db = float(raw_mean)
                except Exception:
                    mean_db = None

        max_db: Optional[float] = None
        if max_match:
            raw_max = max_match.group(1).strip().lower()
            if raw_max != "-inf":
                try:
                    max_db = float(raw_max)
                except Exception:
                    max_db = None
        return mean_db, max_db

    def _final_output_audibility_state(self, output_path: Path) -> tuple[str, str]:
        try:
            duration_sec = max(1.0, ffprobe_duration_ms(output_path, logger=self.logger) / 1000.0)
        except Exception as exc:
            return "unknown", f"duration probe failed: {exc}"

        sample_duration_sec = 4.0
        candidate_offsets = [
            0.0,
            max(0.0, duration_sec * 0.25 - sample_duration_sec * 0.5),
            max(0.0, duration_sec * 0.50 - sample_duration_sec * 0.5),
            max(0.0, duration_sec * 0.75 - sample_duration_sec * 0.5),
            max(0.0, duration_sec - sample_duration_sec - 3.0),
        ]
        offsets = sorted(set(round(x, 3) for x in candidate_offsets if x <= max(0.0, duration_sec - 0.1)))
        if not offsets:
            offsets = [0.0]

        measured = 0
        audible_windows = 0
        best_mean_db: Optional[float] = None
        best_max_db: Optional[float] = None
        summaries: list[str] = []
        for offset in offsets:
            mean_db, max_db = self._probe_volume_stats(
                output_path=output_path,
                offset_sec=float(offset),
                sample_duration_sec=sample_duration_sec,
            )
            if mean_db is None and max_db is None:
                summaries.append(f"{offset:.1f}s:unmeasured")
                continue
            measured += 1
            if mean_db is not None:
                best_mean_db = mean_db if best_mean_db is None else max(best_mean_db, mean_db)
            if max_db is not None:
                best_max_db = max_db if best_max_db is None else max(best_max_db, max_db)
            summaries.append(
                f"{offset:.1f}s:mean={mean_db if mean_db is not None else 'n/a'} max={max_db if max_db is not None else 'n/a'}"
            )
            # Final output should be clearly audible (not only faint rain/noise floor).
            if max_db is not None and max_db > -18.0:
                audible_windows += 1
                continue
            if mean_db is not None and mean_db > -42.0 and (max_db is None or max_db > -30.0):
                audible_windows += 1

        if measured == 0:
            return "unknown", "no measurable windows"

        if duration_sec >= 90.0:
            required = max(2, measured // 2)
        elif duration_sec >= 30.0:
            required = 2 if measured >= 2 else 1
        else:
            required = 1

        details = (
            f"windows audible={audible_windows}/{measured} required={required} "
            f"best_mean={best_mean_db if best_mean_db is not None else 'n/a'} "
            f"best_max={best_max_db if best_max_db is not None else 'n/a'} "
            f"[{'; '.join(summaries)}]"
        )
        if audible_windows >= required:
            return "audible", details
        return "silent", details

    def _output_has_audible_content(self, output_path: Path) -> bool:
        return self._output_audibility_state(
            output_path=output_path,
            sample_duration_sec=6.0,
            deep_probe=True,
        ) == "audible"

    def _plan_with_degraded_transitions(
        self,
        plan: MixPlan,
        degraded_transition_indices: set[int],
    ) -> MixPlan:
        if not degraded_transition_indices:
            return plan

        transitions: list[TransitionPlan] = []
        for idx, transition in enumerate(plan.transitions):
            if idx in degraded_transition_indices:
                reason = transition.reason or "fixed"
                transitions.append(
                    TransitionPlan(
                        from_track_id=transition.from_track_id,
                        to_track_id=transition.to_track_id,
                        crossfade_ms=0,
                        smart_used=False,
                        reason=f"{reason}+fallback-concat",
                        key_distance=transition.key_distance,
                        lpf_duck_ms=None,
                    )
                )
            else:
                transitions.append(copy.deepcopy(transition))

        timeline: list[TimelineEntry] = []
        cursor_ms = 0
        for idx, instance in enumerate(plan.instances):
            if idx > 0 and idx - 1 < len(transitions):
                cursor_ms -= transitions[idx - 1].crossfade_ms
            start = max(0, cursor_ms)
            end = start + instance.track.duration_ms
            prev_snapshot = (
                copy.deepcopy(plan.timeline[idx].analysis_snapshot)
                if idx < len(plan.timeline)
                else {}
            )
            timeline.append(
                TimelineEntry(
                    instance_index=instance.instance_index,
                    track_id=instance.track.id,
                    filename=instance.track.path.name,
                    start_time_ms=start,
                    end_time_ms=end,
                    cycle_index=instance.cycle_index,
                    analysis_snapshot=prev_snapshot,
                )
            )
            cursor_ms = end

        return MixPlan(
            instances=plan.instances,
            timeline=timeline,
            transitions=transitions,
            estimated_duration_ms=timeline[-1].end_time_ms if timeline else 0,
            target_reached=plan.target_reached,
        )

    def _concat_audio_files(self, sources: list[Path], output_path: Path) -> None:
        if not sources:
            raise RuntimeError("Cannot concat empty audio source list.")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for src in sources:
            cmd.extend(["-i", str(src)])
        concat_inputs = "".join(f"[{idx}:a]" for idx in range(len(sources)))
        cmd.extend(
            [
                "-filter_complex",
                f"{concat_inputs}concat=n={len(sources)}:v=0:a=1[outa]",
                "-map",
                "[outa]",
                "-vn",
                "-sn",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
        )
        try:
            run_command(cmd, logger=self.logger)
        except CommandError as exc:
            raise RuntimeError(exc.stderr or str(exc)) from exc

    def _split_output_into_chunks(
        self,
        output_path: Path,
        chunk_minutes: int,
        bitrate: str,
        on_log: LogCallback,
    ) -> list[Path]:
        if chunk_minutes <= 0:
            return []
        duration_sec = max(1.0, ffprobe_duration_ms(output_path, logger=self.logger) / 1000.0)
        chunk_sec = max(60, int(chunk_minutes * 60))
        total_chunks = max(1, int(math.ceil(duration_sec / float(chunk_sec))))
        chunk_paths: list[Path] = []
        for idx in range(total_chunks):
            start_sec = idx * chunk_sec
            remaining = max(0.0, duration_sec - float(start_sec))
            seg_sec = min(float(chunk_sec), remaining)
            if seg_sec <= 0.0:
                continue
            chunk_path = output_path.with_name(f"{output_path.stem}_part_{idx + 1:03d}.mp3")
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start_sec:.3f}",
                "-t",
                f"{seg_sec:.3f}",
                "-i",
                str(output_path),
                "-vn",
                "-sn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                str(chunk_path),
            ]
            run_command(cmd, logger=self.logger)
            chunk_paths.append(chunk_path)
            self._emit_log(
                on_log,
                f"Chunk {idx + 1}/{total_chunks} written: {chunk_path.name} ({seg_sec / 60.0:.2f} min)",
            )
        return chunk_paths

    def render(
        self,
        session: EngineSessionModel,
        settings: GuiSettings,
        on_log: LogCallback = None,
        on_progress: ProgressCallback = None,
        should_cancel: CancelCallback = None,
    ) -> RenderArtifactsModel:
        config = self._build_run_config(settings)
        session = self.rebuild_plan(session, settings, ordered_paths=session.ordered_paths)
        cache_root = self._resolve_cache_root(settings)
        previous_cache_root = self._active_cache_root
        self._active_cache_root = cache_root
        self._emit_log(on_log, f"Render cache directory: {cache_root}")
        rain_usable, rain_issue = self._validate_rain_input(config.rain)
        if not rain_usable:
            raise RuntimeError(f"Rain input is invalid: {rain_issue}")

        try:
            if settings.preview_mode:
                preview_paths = self._preview_ordered_paths(session, settings.preview_duration_sec)
                if len(preview_paths) < len(session.ordered_paths):
                    session = self.rebuild_plan(session, settings, ordered_paths=preview_paths)
                    self._emit_log(
                        on_log,
                        f"Preview optimization: using first {len(preview_paths)} tracks to reduce memory load.",
                    )

            preset = self._resolve_preset(settings)
            output_path = config.resolved_output_path()
            output_path.parent.mkdir(parents=True, exist_ok=True)

            preview_start_sec: Optional[float] = None
            preview_duration_sec: Optional[float] = None
            if settings.preview_mode:
                preview_duration_sec = max(10.0, settings.preview_duration_sec)
                if len(session.mix_plan.timeline) > 1:
                    first_transition_sec = session.mix_plan.timeline[1].start_time_ms / 1000.0
                    preview_start_sec = max(0.0, first_transition_sec - (preview_duration_sec / 2.0))
                else:
                    preview_start_sec = 0.0
                self._emit_log(
                    on_log,
                        f"Preview mode active: rendering {preview_duration_sec:.0f}s excerpt from {preview_start_sec:.1f}s",
                    )

            if config.rain is not None:
                self._emit_log(on_log, "Running rain compatibility preflight...")
                self._preflight_rain_mix(session=session, config=config)
                self._emit_log(on_log, "Rain compatibility preflight passed.")

            active_preset = preset
            self._emit_log(
                on_log,
                "Using bounded render pipeline (stability-first).",
            )
            degraded_transition_indices = set(
                self._render_staged_fallback(
                    session=session,
                    config=config,
                    preset=active_preset,
                    output_path=output_path,
                    preview_start_sec=preview_start_sec,
                    preview_duration_sec=preview_duration_sec,
                    chunk_per_track_processing=True,
                    on_log=on_log,
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
                or set()
            )

            final_state, final_diag = self._final_output_audibility_state(output_path=output_path)
            self._emit_log(on_log, f"Final audibility check: {final_state} ({final_diag})")
            if final_state != "audible":
                self._emit_log(
                    on_log,
                    f"Primary render variant is {final_state}; running safe decode-only variant.",
                )
                degraded_transition_indices = set(
                    self._render_staged_fallback(
                        session=session,
                        config=config,
                        preset=active_preset,
                        output_path=output_path,
                        preview_start_sec=preview_start_sec,
                        preview_duration_sec=preview_duration_sec,
                        chunk_per_track_processing=False,
                        on_log=on_log,
                        on_progress=on_progress,
                        should_cancel=should_cancel,
                    )
                    or set()
                )
                final_state, final_diag = self._final_output_audibility_state(output_path=output_path)
                self._emit_log(on_log, f"Safe variant audibility check: {final_state} ({final_diag})")
                if final_state != "audible":
                    raise RuntimeError(
                        f"Render completed but output is {final_state}. "
                        "Rain is enabled and remained enabled. "
                        f"Diagnostics: {final_diag}. "
                        "Try a different rain file or adjust rain level/crossfade settings."
                    )

            effective_plan = self._plan_with_degraded_transitions(
                plan=session.mix_plan,
                degraded_transition_indices=degraded_transition_indices,
            )
            if degraded_transition_indices:
                degraded_items = ", ".join(str(idx + 1) for idx in sorted(degraded_transition_indices))
                self._emit_log(
                    on_log,
                    "Transition fallback used (no crossfade) at boundaries: "
                    f"{degraded_items}. Exported timestamps were adjusted.",
                )
                session_for_outputs = replace(session, mix_plan=effective_plan)
            else:
                session_for_outputs = session

            tracklists = write_tracklist_artifacts(
                plan=session_for_outputs.mix_plan,
                analyses=session_for_outputs.analyses,
                output_path=output_path,
            )
            adaptive_report_path: Optional[Path] = None
            processing_tracklist_path: Optional[Path] = None
            if config.adaptive_lofi:
                adaptive_report_path, processing_tracklist_path = self._write_adaptive_outputs(
                    session=session_for_outputs,
                    adaptive_report_path=config.adaptive_report,
                    output_path=output_path,
                )
            metadata_path = self._write_metadata(session_for_outputs, config=config)
            chunk_paths: list[Path] = []
            if (
                settings.output_chunks_enabled
                and not settings.preview_mode
                and config.resolve_output_format() == OutputFormat.mp3
            ):
                self._emit_log(
                    on_log,
                    f"Splitting output into {settings.output_chunk_minutes} min MP3 chunks...",
                )
                chunk_paths = self._split_output_into_chunks(
                    output_path=output_path,
                    chunk_minutes=settings.output_chunk_minutes,
                    bitrate=config.bitrate,
                    on_log=on_log,
                )
            return RenderArtifactsModel(
                output_audio_path=output_path,
                tracklist_txt_path=tracklists.tracklist_txt_path,
                tracklist_json_path=tracklists.tracklist_json_path,
                timestamps_txt_path=tracklists.timestamps_txt_path,
                timestamps_csv_path=tracklists.timestamps_csv_path,
                adaptive_report_path=adaptive_report_path,
                processing_tracklist_path=processing_tracklist_path,
                metadata_json_path=metadata_path,
                chunk_output_paths=chunk_paths,
            )
        finally:
            self._active_cache_root = previous_cache_root
