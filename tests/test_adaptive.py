import logging
from pathlib import Path

import numpy as np

import nightfall_mix.analysis as analysis_module
from nightfall_mix.analysis import AdaptiveMetrics, AdaptiveProcessing, TrackAnalysis, derive_adaptive_processing
from nightfall_mix.config import PresetName, RunConfig
from nightfall_mix.effects_presets import get_preset
from nightfall_mix.ffmpeg_graph import build_filtergraph
from nightfall_mix.mixer import TrackInstance, TrackSource, build_mix_plan


def test_adaptive_rules_skip_lpf_and_noise_for_already_lofi(tmp_path: Path) -> None:
    songs = tmp_path / "songs"
    songs.mkdir()
    output = tmp_path / "out.mp3"
    cfg = RunConfig(songs_folder=songs, output=output, adaptive_lofi=True)
    preset = get_preset(PresetName.tokyo_cassette)
    metrics = AdaptiveMetrics(
        lufs=-14.5,
        rms_dbfs=-17.0,
        crest_factor_db=7.5,
        spectral_centroid_hz=3000.0,
        rolloff_hz=9200.0,
        stereo_width=0.70,
        noise_floor_dbfs=-38.0,
    )
    proc = derive_adaptive_processing(metrics=metrics, preset=preset, config=cfg)
    assert proc.lpf_cutoff_hz is None
    assert proc.saturation_strength < 0.5
    assert proc.compression_strength < 1.0
    assert abs(proc.stereo_width_target - 0.70) < 1e-6
    assert proc.noise_added_db is None


def test_adaptive_rules_apply_processing_for_clean_bright_track(tmp_path: Path) -> None:
    songs = tmp_path / "songs"
    songs.mkdir()
    output = tmp_path / "out.mp3"
    cfg = RunConfig(
        songs_folder=songs,
        output=output,
        adaptive_lofi=True,
        preset=PresetName.cleaner_lofi,
    )
    preset = get_preset(PresetName.cleaner_lofi)
    metrics = AdaptiveMetrics(
        lufs=-12.0,
        rms_dbfs=-13.0,
        crest_factor_db=16.0,
        spectral_centroid_hz=5000.0,
        rolloff_hz=12500.0,
        stereo_width=1.2,
        noise_floor_dbfs=-58.0,
    )
    proc = derive_adaptive_processing(metrics=metrics, preset=preset, config=cfg)
    assert proc.lpf_cutoff_hz is not None
    assert proc.lpf_cutoff_hz <= 10000.0
    assert proc.saturation_strength >= 1.0
    assert proc.compression_strength >= 1.0
    assert proc.stereo_width_target <= 0.85
    assert proc.noise_added_db is not None


def test_filtergraph_adaptive_mode_uses_glue_master_chain(tmp_path: Path) -> None:
    songs = tmp_path / "songs"
    songs.mkdir()
    output = tmp_path / "out.mp3"
    track_path = songs / "a.mp3"
    track_path.write_bytes(b"stub")

    cfg = RunConfig(songs_folder=songs, output=output, adaptive_lofi=True)
    src = TrackSource(id="t0", path=track_path, duration_ms=30_000)
    instances = [TrackInstance(instance_index=0, track=src, cycle_index=0)]
    analysis = TrackAnalysis(
        track_id="t0",
        adaptive_processing=AdaptiveProcessing(
            lpf_cutoff_hz=None,
            saturation_strength=0.6,
            compression_strength=0.9,
            stereo_width_target=0.85,
            noise_added_db=-35.0,
            lofi_needed_score=68.0,
            rationale="test",
        ),
    )
    plan = build_mix_plan(
        instances=instances,
        analyses={"t0": analysis},
        crossfade_sec=6.0,
        smart_crossfade=False,
        target_duration_min=None,
    )
    graph = build_filtergraph(
        mix_plan=plan,
        analyses={"t0": analysis},
        config=cfg,
        preset=get_preset(cfg.preset),
        include_master=True,
        include_rain=False,
        per_track_processing=True,
    )
    assert "highpass=f=40,acompressor=threshold=-22dB:ratio=1.35" in graph
    assert "anoisesrc" in graph
    assert "alimiter=limit=0.891[outa]" in graph


def test_analyze_track_warns_when_smart_ordering_without_librosa(monkeypatch) -> None:
    monkeypatch.setattr(analysis_module, "LIBROSA_AVAILABLE", False)
    monkeypatch.setattr(analysis_module, "measure_loudness", lambda *args, **kwargs: analysis_module.LoudnessStats())
    result = analysis_module.analyze_track(
        track_id="t0",
        path=Path("track.mp3"),
        duration_ms=30_000,
        target_lufs=-14.0,
        smart_crossfade=False,
        smart_ordering=True,
        logger=logging.getLogger("test"),
    )
    assert any("smart analysis disabled" in warning for warning in result.warnings)


def test_adaptive_metrics_sidecar_write_failure_is_non_fatal(monkeypatch) -> None:
    monkeypatch.setattr(analysis_module, "_load_adaptive_sidecar", lambda path: None)
    monkeypatch.setattr(analysis_module, "_analysis_offsets", lambda duration_sec: [0.0])
    monkeypatch.setattr(
        analysis_module,
        "_decode_pcm_window",
        lambda **kwargs: np.full((22050, 2), 0.01, dtype=np.float32),
    )
    monkeypatch.setattr(
        analysis_module,
        "_save_adaptive_sidecar",
        lambda track_path, metrics: (_ for _ in ()).throw(PermissionError("read only")),
    )
    metrics = analysis_module.analyze_adaptive_metrics(
        path=Path("song.mp3"),
        duration_ms=30_000,
        loudness=analysis_module.LoudnessStats(input_i=-14.0),
        logger=logging.getLogger("test"),
    )
    assert metrics.lufs == -14.0


def test_bpm_normalization_folds_double_and_half_time() -> None:
    assert analysis_module._normalize_bpm_for_lofi(152.0) == 76.0
    assert analysis_module._normalize_bpm_for_lofi(148.0) == 74.0
    assert analysis_module._normalize_bpm_for_lofi(37.0) == 74.0
    assert analysis_module._normalize_bpm_for_lofi(92.0) == 92.0
