from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path
from typing import Callable, Optional

from nightfall_mix.analysis import TrackAnalysis, analyze_track
from nightfall_mix.config import SmartOrderingMode
from nightfall_mix.mixer import TrackSource, natural_name_key, order_sources_by_transition_fit
from nightfall_mix.utils import (
    CommandError,
    ensure_dependencies,
    ffprobe_duration_ms,
    run_command,
)

LogCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[int, int], None]]
CancelCallback = Optional[Callable[[], bool]]

SUPPORTED_MP4_INPUT_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv"}


class MediaToolsService:
    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger("nightfall_desktop.media_tools")
        self.logger.setLevel(logging.DEBUG)

    def _emit_log(self, callback: LogCallback, message: str) -> None:
        self.logger.info(message)
        if callback:
            callback(message)

    @staticmethod
    def _discover_mp4_inputs(folder: Path) -> list[Path]:
        files = [
            p
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_MP4_INPUT_EXTENSIONS
        ]
        return sorted(files, key=natural_name_key)

    def discover_mp4_inputs(self, folder: Path) -> list[Path]:
        if not folder.exists() or not folder.is_dir():
            raise RuntimeError(f"Input folder does not exist: {folder}")
        return self._discover_mp4_inputs(folder)

    def split_mp3(
        self,
        input_path: Path,
        output_dir: Path,
        chunk_minutes: int,
        bitrate: str,
        on_log: LogCallback = None,
        on_progress: ProgressCallback = None,
        should_cancel: CancelCallback = None,
    ) -> list[Path]:
        ensure_dependencies(self.logger)
        if not input_path.exists() or not input_path.is_file():
            raise RuntimeError(f"Input file does not exist: {input_path}")
        output_dir.mkdir(parents=True, exist_ok=True)
        duration_sec = max(1.0, ffprobe_duration_ms(input_path, logger=self.logger) / 1000.0)
        chunk_sec = max(60, int(chunk_minutes * 60))
        total_chunks = max(1, int(math.ceil(duration_sec / float(chunk_sec))))

        self._emit_log(
            on_log,
            f"Splitting {input_path.name} into {total_chunks} chunk(s) of {chunk_minutes} min.",
        )
        chunk_paths: list[Path] = []
        for idx in range(total_chunks):
            if should_cancel and should_cancel():
                raise RuntimeError("MP3 split cancelled")
            start_sec = idx * chunk_sec
            remaining = max(0.0, duration_sec - float(start_sec))
            seg_sec = min(float(chunk_sec), remaining)
            if seg_sec <= 0.0:
                continue
            out_path = output_dir / f"{input_path.stem}_part_{idx + 1:03d}.mp3"
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
                str(input_path),
                "-vn",
                "-sn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                str(out_path),
            ]
            run_command(cmd, logger=self.logger)
            chunk_paths.append(out_path)
            self._emit_log(
                on_log,
                f"Chunk {idx + 1}/{total_chunks}: {out_path.name} ({seg_sec / 60.0:.2f} min)",
            )
            if on_progress:
                on_progress(idx + 1, total_chunks)
        return chunk_paths

    def _order_videos_by_audio(
        self,
        files: list[Path],
        mode: SmartOrderingMode,
        on_log: LogCallback,
        should_cancel: CancelCallback,
    ) -> list[Path]:
        sources: list[TrackSource] = []
        analyses: dict[str, TrackAnalysis] = {}
        total = len(files)
        for idx, path in enumerate(files):
            if should_cancel and should_cancel():
                raise RuntimeError("MP4 stitch cancelled")
            duration_ms = ffprobe_duration_ms(path, logger=self.logger)
            track_id = f"v{idx}"
            sources.append(TrackSource(id=track_id, path=path, duration_ms=duration_ms))
            analyses[track_id] = analyze_track(
                track_id=track_id,
                path=path,
                duration_ms=duration_ms,
                target_lufs=-14.0,
                smart_crossfade=True,
                smart_ordering=True,
                logger=self.logger,
            )
            self._emit_log(on_log, f"Smart ordering analysis {idx + 1}/{total}: {path.name}")
        ordered = order_sources_by_transition_fit(sources, analyses, mode=mode)
        return [src.path for src in ordered]

    def _audio_crossfade_seconds(
        self,
        left_analysis: Optional[TrackAnalysis],
        right_analysis: Optional[TrackAnalysis],
        left_duration_s: float,
        right_duration_s: float,
        base_crossfade_sec: float,
    ) -> float:
        if (
            left_analysis is not None
            and right_analysis is not None
            and left_analysis.bpm is not None
            and right_analysis.bpm is not None
            and (left_analysis.bpm_confidence or 0.0) >= 0.35
            and (right_analysis.bpm_confidence or 0.0) >= 0.35
        ):
            avg_bpm = (left_analysis.bpm + right_analysis.bpm) / 2.0
            beat_sec = 60.0 / max(1e-6, avg_bpm)
            candidate = 4.0 * beat_sec
            return max(0.5, min(4.0, candidate))
        relative = min(left_duration_s, right_duration_s) * 0.06
        return max(0.5, min(4.0, max(base_crossfade_sec, relative)))

    def _stitch_with_audio_crossfade(
        self,
        files: list[Path],
        output_path: Path,
        base_crossfade_sec: float,
        on_log: LogCallback,
        should_cancel: CancelCallback,
    ) -> Path:
        analyses: dict[int, TrackAnalysis] = {}
        durations_s: list[float] = []
        for idx, path in enumerate(files):
            if should_cancel and should_cancel():
                raise RuntimeError("MP4 stitch cancelled")
            duration_ms = ffprobe_duration_ms(path, logger=self.logger)
            durations_s.append(max(1.0, duration_ms / 1000.0))
            analyses[idx] = analyze_track(
                track_id=f"v{idx}",
                path=path,
                duration_ms=duration_ms,
                target_lufs=-14.0,
                smart_crossfade=True,
                smart_ordering=True,
                logger=self.logger,
            )

        with tempfile.TemporaryDirectory(prefix="nightfall_mp4_stitch_") as td:
            graph_path = Path(td) / "graph.txt"
            lines: list[str] = []
            for idx in range(len(files)):
                lines.append(f"[{idx}:v]setpts=PTS-STARTPTS,format=yuv420p[v{idx}]")
                lines.append(
                    f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,aresample=48000[a{idx}]"
                )
            concat_inputs = "".join(f"[v{idx}]" for idx in range(len(files)))
            lines.append(f"{concat_inputs}concat=n={len(files)}:v=1:a=0[vout]")

            if len(files) == 1:
                lines.append("[a0]anull[aout]")
            else:
                current = "[a0]"
                for idx in range(1, len(files)):
                    left_analysis = analyses.get(idx - 1)
                    right_analysis = analyses.get(idx)
                    d = self._audio_crossfade_seconds(
                        left_analysis=left_analysis,
                        right_analysis=right_analysis,
                        left_duration_s=durations_s[idx - 1],
                        right_duration_s=durations_s[idx],
                        base_crossfade_sec=base_crossfade_sec,
                    )
                    next_label = "[aout]" if idx == len(files) - 1 else f"[af{idx}]"
                    lines.append(f"{current}[a{idx}]acrossfade=d={d:.3f}:c1=tri:c2=tri{next_label}")
                    current = next_label

            graph_path.write_text(";\n".join(lines), encoding="utf-8")
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            for path in files:
                cmd.extend(["-i", str(path)])
            cmd.extend(
                [
                    "-filter_complex_script",
                    str(graph_path),
                    "-map",
                    "[vout]",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
            run_command(cmd, logger=self.logger)
        self._emit_log(
            on_log,
            "Smart audio fade render complete (video is stitched with hard cuts, audio transitions are crossfaded).",
        )
        return output_path

    def stitch_mp4(
        self,
        folder: Path,
        output_path: Path,
        smart_ordering: bool,
        smart_fade: bool,
        base_crossfade_sec: float,
        input_files: Optional[list[Path]] = None,
        on_log: LogCallback = None,
        on_progress: ProgressCallback = None,
        should_cancel: CancelCallback = None,
    ) -> Path:
        ensure_dependencies(self.logger)
        if input_files is not None:
            files = []
            for path in input_files:
                if (
                    path.exists()
                    and path.is_file()
                    and path.suffix.lower() in SUPPORTED_MP4_INPUT_EXTENSIONS
                ):
                    files.append(path)
        else:
            if not folder.exists() or not folder.is_dir():
                raise RuntimeError(f"Input folder does not exist: {folder}")
            files = self._discover_mp4_inputs(folder)
        if not files:
            raise RuntimeError("No MP4/MOV/MKV files found in folder.")

        self._emit_log(on_log, f"Found {len(files)} video files.")
        if smart_ordering and len(files) > 2:
            self._emit_log(on_log, "Applying smart ordering using audio BPM/key fit...")
            files = self._order_videos_by_audio(
                files=files,
                mode=SmartOrderingMode.bpm_key_balanced,
                on_log=on_log,
                should_cancel=should_cancel,
            )
            self._emit_log(on_log, "Smart ordering applied.")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if smart_fade:
            result = self._stitch_with_audio_crossfade(
                files=files,
                output_path=output_path,
                base_crossfade_sec=base_crossfade_sec,
                on_log=on_log,
                should_cancel=should_cancel,
            )
            if on_progress:
                on_progress(1, 1)
            return result

        with tempfile.TemporaryDirectory(prefix="nightfall_mp4_concat_") as td:
            list_path = Path(td) / "concat_list.txt"
            lines = []
            for path in files:
                escaped = str(path).replace("'", "'\\''")
                lines.append(f"file '{escaped}'")
            list_path.write_text("\n".join(lines), encoding="utf-8")
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(output_path),
            ]
            try:
                run_command(cmd, logger=self.logger)
            except CommandError:
                self._emit_log(on_log, "Copy concat failed; retrying with re-encode.")
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
                run_command(cmd, logger=self.logger)
        if on_progress:
            on_progress(1, 1)
        self._emit_log(on_log, "Stitch complete.")
        return output_path
