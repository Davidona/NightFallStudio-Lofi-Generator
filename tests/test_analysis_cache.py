from __future__ import annotations

import json
from pathlib import Path

from nightfall_mix import analysis as analysis_mod


def _write_sidecar(track_path: Path, payload: dict) -> Path:
    sidecar = track_path.with_name(f"{track_path.name}.nightfall_analysis.json")
    sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sidecar


def test_read_analysis_cache_summary_returns_cached_bpm_key(tmp_path: Path) -> None:
    track = tmp_path / "song.mp3"
    track.write_bytes(b"dummy")
    stat = track.stat()
    payload = {
        "version": 1,
        "source": {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)},
        "computed": {"loudness": True, "bpm_key": True, "rms_edges": True},
        "analysis": {
            "bpm": 74.2,
            "bpm_confidence": 0.81,
            "key": "A:min",
            "key_confidence": 0.73,
            "head_rms_curve": [0.1, 0.2],
            "tail_rms_curve": [0.2, 0.1],
            "loudness": {"input_i": -13.6, "measured": {"input_i": -13.6}},
            "warnings": [],
        },
    }
    _write_sidecar(track, payload)

    summary = analysis_mod.read_analysis_cache_summary(track)
    assert summary is not None
    assert summary["bpm"] == 74.2
    assert summary["key"] == "A:min"
    assert summary["has_bpm_key"] is True


def test_read_analysis_cache_summary_invalidated_when_file_changes(tmp_path: Path) -> None:
    track = tmp_path / "song.mp3"
    track.write_bytes(b"dummy")
    stat = track.stat()
    payload = {
        "version": 1,
        "source": {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)},
        "computed": {"loudness": True, "bpm_key": False, "rms_edges": False},
        "analysis": {"loudness": {"input_i": -14.0, "measured": {"input_i": -14.0}}},
    }
    _write_sidecar(track, payload)

    # Mutate source to force signature mismatch.
    track.write_bytes(b"dummy + changed")
    assert analysis_mod.read_analysis_cache_summary(track) is None


def test_analyze_track_reuses_cached_sidecar_without_remeasuring_loudness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    track = tmp_path / "song.mp3"
    track.write_bytes(b"dummy")
    stat = track.stat()
    payload = {
        "version": 1,
        "source": {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)},
        "computed": {"loudness": True, "bpm_key": False, "rms_edges": False},
        "analysis": {
            "bpm": None,
            "bpm_confidence": None,
            "key": None,
            "key_confidence": None,
            "head_rms_curve": [],
            "tail_rms_curve": [],
            "loudness": {
                "input_i": -12.5,
                "input_tp": -1.5,
                "input_lra": 8.4,
                "input_thresh": -22.2,
                "target_offset": -1.5,
                "measured": {"input_i": -12.5},
            },
            "warnings": [],
        },
    }
    _write_sidecar(track, payload)

    def fail_measure(*_args, **_kwargs):
        raise AssertionError("measure_loudness should not be called when cache is valid")

    monkeypatch.setattr(analysis_mod, "measure_loudness", fail_measure)
    monkeypatch.setattr(analysis_mod, "LIBROSA_AVAILABLE", False)

    analyzed = analysis_mod.analyze_track(
        track_id="t0",
        path=track,
        duration_ms=120_000,
        target_lufs=-14.0,
        smart_crossfade=False,
        smart_ordering=False,
        logger=analysis_mod.logging.getLogger("test"),
    )
    assert analyzed.loudness.input_i == -12.5
    assert analyzed.loudness.recommended_gain_db == -1.5


def test_read_analysis_cache_summary_reports_partial_when_only_adaptive_cache_exists(
    tmp_path: Path,
) -> None:
    track = tmp_path / "song.mp3"
    track.write_bytes(b"dummy")
    stat = track.stat()
    adaptive_sidecar = track.with_name(f"{track.name}.nightfall_adaptive.json")
    adaptive_sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "source": {
                    "path": str(track),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "size": int(stat.st_size),
                },
                "metrics": {
                    "lufs": -13.5,
                    "rms_dbfs": -19.2,
                    "crest_factor_db": 8.8,
                    "spectral_centroid_hz": 2800.0,
                    "rolloff_hz": 9300.0,
                    "stereo_width": 0.82,
                    "noise_floor_dbfs": -47.0,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = analysis_mod.read_analysis_cache_summary(track)
    assert summary is not None
    assert summary["has_loudness"] is False
    assert summary["has_bpm_key"] is False
    assert summary["has_rms_edges"] is False
    assert summary["has_adaptive_metrics"] is True
