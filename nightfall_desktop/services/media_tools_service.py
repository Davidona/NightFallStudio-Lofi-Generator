from __future__ import annotations

import logging
import math
import os
import shutil
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
    run_command_stream,
)

LogCallback = Optional[Callable[[str], None]]
ProgressCallback = Optional[Callable[[int, int], None]]
CancelCallback = Optional[Callable[[], bool]]

SUPPORTED_MP4_INPUT_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv"}

# Render at most this many source files per ffmpeg pass. Every file in a pass
# is decoded concurrently, so this bounds peak memory/CPU regardless of how
# many files are being stitched.
MP4_STITCH_CHUNK_SIZE = 6
# Rough upper bound on re-encode size vs. total source size, used for the
# free-space preflight warning.
STITCH_OUTPUT_SIZE_MULTIPLIER = 2.5
# Warn when the pagefile drive (C: on Windows) has less than this free.
STITCH_MIN_PAGEFILE_FREE_BYTES = 25 * 1024 ** 3
# Per-input decode threads and output encode threads for the chunk renders.
STITCH_DECODE_THREADS = "2"
STITCH_ENCODE_THREADS = "6"


def _build_chunk_graph_lines(
    start: int,
    end: int,
    carry_fade: Optional[float],
    next_fade: Optional[float],
    durations_s: list[float],
    fades: dict[int, float],
) -> list[str]:
    """Build the filter_complex script for one stitch chunk.

    Inputs are ordered [carry-in file (audio only), files[start:end]]. The
    carry-in contributes only its crossfade tail (``carry_fade`` seconds) and
    never any video, so chunk outputs join without duplicating video or audio.
    The last file's audio tail is trimmed by ``next_fade`` so the following
    chunk owns that boundary fade. ``fades[i]`` is the crossfade duration
    between files[i - 1] and files[i].
    """
    lines: list[str] = []
    in_idx = 0
    if carry_fade is not None:
        trim_start = max(0.0, durations_s[start - 1] - carry_fade)
        lines.append(
            f"[{in_idx}:a]atrim=start={trim_start:.3f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates=48000:channel_layouts=stereo,aresample=48000[a{in_idx}]"
        )
        in_idx += 1

    video_inputs: list[str] = []
    for idx in range(start, end):
        if idx == end - 1 and next_fade is not None:
            trim_end = max(0.0, durations_s[idx] - next_fade)
            lines.append(
                f"[{in_idx}:a]atrim=end={trim_end:.3f},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=48000:channel_layouts=stereo,aresample=48000[a{in_idx}]"
            )
        else:
            lines.append(
                f"[{in_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,aresample=48000[a{in_idx}]"
            )
        lines.append(f"[{in_idx}:v]setpts=PTS-STARTPTS,format=yuv420p[v{in_idx}]")
        video_inputs.append(f"[v{in_idx}]")
        in_idx += 1

    concat_video = "".join(video_inputs)
    lines.append(f"{concat_video}concat=n={len(video_inputs)}:v=1:a=0[vout]")

    if in_idx == 1:
        lines.append("[a0]anull[aout]")
    else:
        current = "[a0]"
        for i in range(in_idx - 1):
            # Render pair (i, i+1) maps to original files:
            #  with carry-in: files[start - 1 + i] & files[start + i] -> fades[start + i]
            #  without:      files[start + i]     & files[start + i + 1] -> fades[start + i + 1]
            d = fades[start + i + (0 if carry_fade is not None else 1)]
            next_label = "[aout]" if i == in_idx - 2 else f"[af{i + 1}]"
            lines.append(
                f"{current}[a{i + 1}]acrossfade=d={d:.3f}:c1=qsin:c2=qsin{next_label}"
            )
            current = next_label

    return lines


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
            # Stream-copy the segment without re-encoding.
            # Re-encoding with libmp3lame introduces encoder delay (~13–26 ms of
            # silence) at the start of every chunk.  Those silent samples survive
            # through video creation and produce an audible gap at every stitch
            # point.  Stream copy avoids that entirely; the split is lossless and
            # frame-accurate (within one MP3 frame ≈ 26 ms, which is inaudible).
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
                "copy",
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
        max_allowed = max(0.5, min(base_crossfade_sec, min(left_duration_s, right_duration_s) - 0.5))
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
            return max(0.5, min(max_allowed, candidate))
        if (
            left_analysis is not None
            and right_analysis is not None
            and left_analysis.tail_rms_curve
            and right_analysis.head_rms_curve
        ):
            base_ms = int(base_crossfade_sec * 1000)
            max_ms = int(max_allowed * 1000)
            min_ms = 500
            candidates = list(range(min_ms, max_ms + 1, 250))
            if base_ms not in candidates and min_ms <= base_ms <= max_ms:
                candidates.append(base_ms)
            best_ms = base_ms
            best_score = float("inf")
            for ms in sorted(set(candidates)):
                frames = max(1, int(ms / 50))
                tail = left_analysis.tail_rms_curve
                head = right_analysis.head_rms_curve
                tail_window = tail[max(0, len(tail) - frames):]
                head_window = head[:min(len(head), frames)]
                tail_energy = sum(tail_window) / max(1, len(tail_window))
                head_energy = sum(head_window) / max(1, len(head_window))
                score = tail_energy + head_energy
                score += 0.15 * abs(ms - base_ms) / max(base_ms, 1)
                if score < best_score:
                    best_score = score
                    best_ms = ms
            return max(0.5, min(max_allowed, best_ms / 1000.0))
        relative = min(left_duration_s, right_duration_s) * 0.06
        return max(0.5, min(max_allowed, max(base_crossfade_sec, relative)))

    def _stitch_with_audio_crossfade(
        self,
        files: list[Path],
        output_path: Path,
        base_crossfade_sec: float,
        on_log: LogCallback,
        on_progress: ProgressCallback,
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

        fades: dict[int, float] = {}
        for idx in range(1, len(files)):
            fades[idx] = self._audio_crossfade_seconds(
                left_analysis=analyses.get(idx - 1),
                right_analysis=analyses.get(idx),
                left_duration_s=durations_s[idx - 1],
                right_duration_s=durations_s[idx],
                base_crossfade_sec=base_crossfade_sec,
            )

        chunk_size = MP4_STITCH_CHUNK_SIZE
        chunk_starts = list(range(0, len(files), chunk_size))
        if len(chunk_starts) > 1:
            self._emit_log(
                on_log,
                f"Rendering in {len(chunk_starts)} chunks of up to {chunk_size} files "
                f"to bound memory and CPU.",
            )

        # Intermediates are rendered next to the output (same drive) and joined
        # with a stream copy, so the disk-free preflight applies to this too.
        with tempfile.TemporaryDirectory(
            prefix="nightfall_mp4_stitch_", dir=str(output_path.parent)
        ) as td:
            td_path = Path(td)
            intermediate_paths: list[Path] = []
            for chunk_idx, start in enumerate(chunk_starts):
                if should_cancel and should_cancel():
                    raise RuntimeError("MP4 stitch cancelled")
                end = min(start + chunk_size, len(files))
                carry_fade = fades[start] if start > 0 else None
                next_fade = fades[end] if end < len(files) else None
                intermediate = td_path / f"chunk_{chunk_idx:04d}.mkv"
                self._emit_log(
                    on_log,
                    f"Rendering chunk {chunk_idx + 1}/{len(chunk_starts)} "
                    f"({files[start].name} ... {files[end - 1].name})",
                )
                self._render_crossfade_chunk(
                    files=files,
                    start=start,
                    end=end,
                    fades=fades,
                    durations_s=durations_s,
                    carry_fade=carry_fade,
                    next_fade=next_fade,
                    output_path=intermediate,
                    on_log=on_log,
                    on_progress=on_progress,
                    files_done=start,
                    files_total=len(files),
                    should_cancel=should_cancel,
                )
                intermediate_paths.append(intermediate)
                if on_progress:
                    on_progress(end, len(files))

            self._emit_log(on_log, "Joining chunks and encoding final audio track...")
            self._mux_final_output(
                intermediate_paths=intermediate_paths,
                output_path=output_path,
                on_log=on_log,
                should_cancel=should_cancel,
            )

        self._emit_log(
            on_log,
            "Smart audio fade render complete (video is stitched with hard cuts, audio transitions are crossfaded).",
        )
        return output_path

    def _render_crossfade_chunk(
        self,
        files: list[Path],
        start: int,
        end: int,
        fades: dict[int, float],
        durations_s: list[float],
        carry_fade: Optional[float],
        next_fade: Optional[float],
        output_path: Path,
        on_log: LogCallback,
        on_progress: ProgressCallback,
        files_done: int,
        files_total: int,
        should_cancel: CancelCallback,
    ) -> None:
        """Render one chunk: files[start:end] plus (optionally) the previous
        chunk's last file as an audio-only carry-in that owns the boundary
        crossfade. The last file's audio tail is trimmed so the next chunk can
        own its boundary fade without duplicating audio."""
        lines = _build_chunk_graph_lines(
            start=start,
            end=end,
            carry_fade=carry_fade,
            next_fade=next_fade,
            durations_s=durations_s,
            fades=fades,
        )
        # The crossfades shorten the chunk's audio below its video length.
        # Pad the audio (inside the graph) to the full chunk video duration so
        # every intermediate has audio spanning the exact video length; the
        # segments then join sample-exactly and the final file's audio ends
        # exactly with its video.
        chunk_video_sec = float(sum(durations_s[start:end]))
        audio_map = "[aout]"
        if chunk_video_sec > 0.05:
            lines.append(f"[aout]apad=whole_dur={chunk_video_sec:.3f}[apadout]")
            audio_map = "[apadout]"

        graph_path = output_path.with_suffix(".graph.txt")
        graph_path.write_text(";\n".join(lines), encoding="utf-8")

        input_start = start - 1 if carry_fade is not None else start
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-progress",
            "pipe:1",
        ]
        for path in files[input_start:end]:
            cmd.extend(["-threads", STITCH_DECODE_THREADS, "-i", str(path)])
        cmd.extend(
            [
                "-filter_complex_script",
                str(graph_path),
                "-map",
                "[vout]",
                "-map",
                audio_map,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-threads",
                STITCH_ENCODE_THREADS,
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
        )

        # PCM intermediates are joined losslessly, so the final AAC encode
        # happens exactly once and never accumulates encoder delay at seams.
        chunk_video_us = max(1, int(chunk_video_sec * 1_000_000))
        chunk_file_count = end - start

        def _on_progress_line(line: str) -> None:
            if not line.startswith("out_time_us=") or not on_progress:
                return
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                return
            frac = max(0.0, min(1.0, us / chunk_video_us))
            on_progress(files_done + int(frac * chunk_file_count), files_total)

        try:
            run_command_stream(
                cmd,
                logger=self.logger,
                on_stdout_line=_on_progress_line,
                should_cancel=should_cancel,
                low_priority=True,
            )
        except CommandError as exc:
            if exc.returncode == -9:
                raise RuntimeError("MP4 stitch cancelled")
            raise

    def _mux_final_output(
        self,
        intermediate_paths: list[Path],
        output_path: Path,
        on_log: LogCallback,
        should_cancel: CancelCallback,
    ) -> None:
        list_path = output_path.with_suffix(".concat_list.txt")
        lines = []
        for path in intermediate_paths:
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
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

        try:
            run_command_stream(
                cmd,
                logger=self.logger,
                should_cancel=should_cancel,
                low_priority=True,
            )
        except CommandError as exc:
            if exc.returncode == -9:
                raise RuntimeError("MP4 stitch cancelled")
            raise

    def _preflight_stitch_warnings(
        self,
        files: list[Path],
        output_path: Path,
        on_log: LogCallback,
    ) -> None:
        """Warn about conditions that can turn a long render into a frozen PC:
        not enough free disk on the output drive, a nearly full pagefile drive
        (C: by default on Windows), and output written into the input folder."""
        try:
            total_input_bytes = sum(p.stat().st_size for p in files)
            free_bytes = shutil.disk_usage(output_path.parent).free
            estimated_bytes = total_input_bytes * STITCH_OUTPUT_SIZE_MULTIPLIER
            if free_bytes < estimated_bytes:
                self._emit_log(
                    on_log,
                    f"WARNING: output drive has {free_bytes / 1e9:.1f} GB free but the "
                    f"stitched re-encode may need ~{estimated_bytes / 1e9:.1f} GB "
                    f"(sources total {total_input_bytes / 1e9:.1f} GB). Free space first, "
                    "the render can stall or fail otherwise.",
                )
        except OSError:
            pass

        if os.name == "nt":
            try:
                c_free_bytes = shutil.disk_usage("C:\\").free
                if c_free_bytes < STITCH_MIN_PAGEFILE_FREE_BYTES:
                    self._emit_log(
                        on_log,
                        f"WARNING: C: has only {c_free_bytes / 1e9:.1f} GB free. Windows "
                        "uses C: for the pagefile by default; a long render's memory spike "
                        "can hard-freeze the whole PC when the pagefile cannot grow. Free "
                        "up space on C: or move the pagefile to another drive.",
                    )
            except OSError:
                pass

        input_dirs = {p.parent.resolve() for p in files}
        if output_path.parent.resolve() in input_dirs:
            self._emit_log(
                on_log,
                "Note: output is written into the same folder as the source files; "
                "reading and writing the same drive at once slows the render. Prefer "
                "an output location on a different (ideally SSD) drive.",
            )

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
        self._preflight_stitch_warnings(files, output_path, on_log)
        if smart_fade:
            return self._stitch_with_audio_crossfade(
                files=files,
                output_path=output_path,
                base_crossfade_sec=base_crossfade_sec,
                on_log=on_log,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )

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
