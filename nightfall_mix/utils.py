from __future__ import annotations

import json
import logging
import math
import queue
import random
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional


class CommandError(RuntimeError):
    def __init__(self, message: str, returncode: int, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def seeded_random(seed: Optional[int]) -> random.Random:
    return random.Random(seed if seed is not None else 0)


def run_command(
    args: list[str],
    logger: logging.Logger,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    logger.debug("Running command: %s", " ".join(args))
    completed = subprocess.run(
        args,
        text=True,
        capture_output=capture_output,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise CommandError(
            f"Command failed: {' '.join(args)}",
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
    return completed


def run_command_binary(
    args: list[str],
    logger: logging.Logger,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    logger.debug("Running binary command: %s", " ".join(args))
    completed = subprocess.run(
        args,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise CommandError(
            f"Command failed: {' '.join(args)}",
            returncode=completed.returncode,
            stdout=(completed.stdout or b"").decode("utf-8", errors="replace"),
            stderr=(completed.stderr or b"").decode("utf-8", errors="replace"),
        )
    return completed


def run_command_stream(
    args: list[str],
    logger: logging.Logger,
    on_stdout_line: Optional[Callable[[str], None]] = None,
    on_stderr_line: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> int:
    logger.debug("Running stream command: %s", " ".join(args))
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    stream_queue: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()

    def _pump(stream_name: str, stream) -> None:
        try:
            for raw in iter(stream.readline, ""):
                stream_queue.put((stream_name, raw))
        finally:
            stream_queue.put((stream_name, None))

    threads = [
        threading.Thread(target=_pump, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=_pump, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    stderr_buffer: list[str] = []
    closed = {"stdout": False, "stderr": False}
    while True:
        if should_cancel and should_cancel():
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise CommandError(
                f"Command cancelled: {' '.join(args)}",
                returncode=-9,
                stderr="\n".join(stderr_buffer),
            )
        try:
            stream_name, raw = stream_queue.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None and all(closed.values()):
                break
            continue

        if raw is None:
            closed[stream_name] = True
            if proc.poll() is not None and all(closed.values()):
                break
            continue

        line = raw.strip()
        if stream_name == "stdout":
            if on_stdout_line:
                on_stdout_line(line)
        else:
            stderr_buffer.append(line)
            if on_stderr_line:
                on_stderr_line(line)

        if proc.poll() is not None and all(closed.values()):
            break

    rc = proc.wait()
    if rc != 0:
        raise CommandError(
            f"Command failed: {' '.join(args)}",
            returncode=rc,
            stderr="\n".join(stderr_buffer),
        )
    return rc


def require_binary(name: str) -> bool:
    return shutil.which(name) is not None


def ensure_dependencies(logger: logging.Logger) -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not require_binary(name)]
    if missing:
        raise RuntimeError(f"Missing required dependencies: {', '.join(missing)}")
    run_command(["ffmpeg", "-version"], logger=logger)
    run_command(["ffprobe", "-version"], logger=logger)


def ffprobe_json(path: Path, logger: logging.Logger) -> dict[str, Any]:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        logger=logger,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}") from exc


def ffprobe_duration_ms(path: Path, logger: logging.Logger) -> int:
    data = ffprobe_json(path, logger=logger)
    format_data = data.get("format", {})
    duration = format_data.get("duration")
    if duration is None:
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio" and stream.get("duration") is not None:
                duration = stream.get("duration")
                break
    if duration is None:
        raise RuntimeError(f"Could not determine duration for {path}")
    return int(float(duration) * 1000)


def parse_loudnorm_json(stderr_text: str) -> Optional[dict[str, float]]:
    candidates = re.findall(r"\{[\s\S]*?\}", stderr_text)
    for blob in reversed(candidates):
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if "input_i" in payload:
            parsed: dict[str, float] = {}
            for key, val in payload.items():
                try:
                    num = float(val)
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(num):
                    continue
                parsed[key] = num
            return parsed
    return None


def format_hms(ms: int) -> str:
    total_seconds = max(0, int(ms // 1000))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def read_playlist_file(path: Path) -> list[str]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines
