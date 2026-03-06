from __future__ import annotations

from pathlib import Path

import pytest

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.mixer import TrackInstance, TrackSource, build_mix_plan
from nightfall_desktop.models.session_models import EngineSessionModel, GuiSettings
from nightfall_desktop.services.engine_service import GuiEngineService


def _build_session(root: Path, track_count: int, duration_ms: int = 180_000) -> EngineSessionModel:
    tracks: list[TrackSource] = []
    analyses: dict[str, TrackAnalysis] = {}
    instances: list[TrackInstance] = []
    for idx in range(track_count):
        source = TrackSource(
            id=f"t{idx}",
            path=root / f"track_{idx:02d}.mp3",
            duration_ms=duration_ms,
        )
        tracks.append(source)
        analyses[source.id] = TrackAnalysis(track_id=source.id)
        instances.append(TrackInstance(instance_index=idx, track=source, cycle_index=0))

    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=6.0,
        smart_crossfade=False,
        target_duration_min=None,
    )
    settings = GuiSettings(
        songs_folder=root,
        output_path=root / "mix.mp3",
    )
    return EngineSessionModel(
        settings=settings,
        ordered_paths=[track.path for track in tracks],
        track_sources=tracks,
        analyses=analyses,
        mix_plan=plan,
        warnings=[],
    )


def test_memory_error_classifier_matches_ffmpeg_allocation_failure() -> None:
    message = (
        "[fc#0 @ 000001812e1d6cc0] Error while filtering: Cannot allocate memory\n"
        "[fc#0 @ 000001812e1d6cc0] Task finished with error code: -12 (Cannot allocate memory)\n"
    )
    assert GuiEngineService._is_memory_allocation_failure(message) is True


def test_memory_error_classifier_ignores_non_memory_failures() -> None:
    message = "[libmp3lame @ 000001] Invalid argument"
    assert GuiEngineService._is_memory_allocation_failure(message) is False


def test_render_uses_bounded_pipeline_as_primary_path(tmp_path: Path) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=3)
    settings = GuiSettings(songs_folder=tmp_path, output_path=tmp_path / "mix.mp3")

    staged_calls: list[bool] = []

    def staged_ok(**kwargs):
        staged_calls.append(bool(kwargs.get("chunk_per_track_processing")))

    service._render_staged_fallback = staged_ok  # type: ignore[method-assign]
    service._final_output_audibility_state = lambda **_kwargs: ("audible", "ok")  # type: ignore[method-assign]
    artifacts = service.render(session=session, settings=settings)
    assert staged_calls == [True]
    assert artifacts.output_audio_path == settings.output_path


def test_render_raises_for_non_memory_ffmpeg_errors(tmp_path: Path) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=3)
    settings = GuiSettings(songs_folder=tmp_path, output_path=tmp_path / "mix.mp3")

    def staged_fail(**_kwargs):
        raise RuntimeError("[libmp3lame @ 000001] Invalid argument")

    service._render_staged_fallback = staged_fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Invalid argument"):
        service.render(session=session, settings=settings)


def test_render_retries_staged_with_safe_chunk_path_if_output_is_silent(tmp_path: Path) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=12)
    settings = GuiSettings(songs_folder=tmp_path, output_path=tmp_path / "mix.mp3")

    staged_calls: list[bool] = []
    audible_checks = iter(["silent", "audible"])

    def staged_ok(**kwargs):
        staged_calls.append(bool(kwargs.get("chunk_per_track_processing")))

    def audible_probe(**_kwargs):
        return next(audible_checks), "diag"

    service._render_staged_fallback = staged_ok  # type: ignore[method-assign]
    service._final_output_audibility_state = audible_probe  # type: ignore[method-assign]

    service.render(session=session, settings=settings)
    assert staged_calls == [True, False]


def test_render_fails_if_staged_output_remains_silent_after_retry(tmp_path: Path) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=12)
    settings = GuiSettings(songs_folder=tmp_path, output_path=tmp_path / "mix.mp3")

    def staged_ok(**_kwargs):
        return None

    service._render_staged_fallback = staged_ok  # type: ignore[method-assign]
    service._final_output_audibility_state = lambda **_kwargs: ("silent", "diag")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="output is silent"):
        service.render(session=session, settings=settings)


def test_output_has_audible_content_treats_unknown_as_not_audible() -> None:
    service = GuiEngineService()
    service._output_audibility_state = lambda **_kwargs: "unknown"  # type: ignore[method-assign]
    assert service._output_has_audible_content(Path("dummy.mp3")) is False


def test_final_output_audibility_requires_quorum_for_long_files(monkeypatch) -> None:
    service = GuiEngineService()
    monkeypatch.setattr(
        "nightfall_desktop.services.engine_service.ffprobe_duration_ms",
        lambda _path, logger=None: 300_000,
    )
    values = iter(
        [
            (-70.0, -45.0),
            (-69.0, -44.5),
            (-39.0, -25.0),
            (-38.0, -24.0),
            (-70.0, -45.0),
        ]
    )

    def probe(**_kwargs):
        return next(values)

    service._probe_volume_stats = probe  # type: ignore[method-assign]
    state, _diag = service._final_output_audibility_state(Path("dummy.mp3"))
    assert state == "audible"


def test_final_output_audibility_rejects_near_silent_noise_floor(monkeypatch) -> None:
    service = GuiEngineService()
    monkeypatch.setattr(
        "nightfall_desktop.services.engine_service.ffprobe_duration_ms",
        lambda _path, logger=None: 240_000,
    )
    values = iter(
        [
            (-55.0, -36.0),
            (-54.0, -37.0),
            (-56.5, -35.5),
            (-55.2, -36.2),
            (-57.0, -38.0),
        ]
    )

    def probe(**_kwargs):
        return next(values)

    service._probe_volume_stats = probe  # type: ignore[method-assign]
    state, _diag = service._final_output_audibility_state(Path("dummy.mp3"))
    assert state == "silent"


def test_plan_with_degraded_transitions_recomputes_timeline(tmp_path: Path) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=3, duration_ms=60_000)
    original = session.mix_plan
    adjusted = service._plan_with_degraded_transitions(original, {0})

    assert adjusted.transitions[0].crossfade_ms == 0
    assert adjusted.transitions[0].reason.endswith("fallback-concat")
    assert adjusted.timeline[1].start_time_ms == original.timeline[1].start_time_ms + original.transitions[0].crossfade_ms


def test_staged_render_preserves_track_analysis_ids(tmp_path: Path, monkeypatch) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=2, duration_ms=45_000)
    settings = GuiSettings(songs_folder=tmp_path, output_path=tmp_path / "mix.mp3")
    config = service._build_run_config(settings)
    preset = service._resolve_preset(settings)

    observed_track_ids: list[list[str]] = []

    def fake_run_render_plan(**kwargs):
        mix_plan = kwargs["mix_plan"]
        observed_track_ids.append([inst.track.id for inst in mix_plan.instances])
        out_path = kwargs["output_path"]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"ok")

    monkeypatch.setattr(service, "_run_render_plan", fake_run_render_plan)
    monkeypatch.setattr(
        "nightfall_desktop.services.engine_service.ffprobe_duration_ms",
        lambda _path, logger=None: 45_000,
    )
    monkeypatch.setattr(service, "_output_audibility_state", lambda **_kwargs: "audible")
    monkeypatch.setattr(service, "_final_output_audibility_state", lambda **_kwargs: ("audible", "test"))

    degraded = service._render_staged_fallback(
        session=session,
        config=config,
        preset=preset,
        output_path=tmp_path / "mix.mp3",
        preview_start_sec=None,
        preview_duration_sec=None,
        chunk_per_track_processing=True,
        on_log=None,
        on_progress=None,
        should_cancel=None,
    )

    assert degraded == set()
    assert observed_track_ids[0] == ["t0"]
    assert observed_track_ids[1] == ["t1"]


def test_estimate_render_storage_uses_configured_cache_folder(tmp_path: Path) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=4, duration_ms=120_000)
    cache_dir = tmp_path / "cache_drive"
    cache_dir.mkdir()
    settings = GuiSettings(
        songs_folder=tmp_path,
        output_path=tmp_path / "mix.mp3",
        cache_folder=cache_dir,
    )

    estimate = service.estimate_render_storage(session=session, settings=settings)
    assert estimate["cache_root"] == cache_dir
    assert int(estimate["required_cache_bytes"]) > 0
    assert int(estimate["available_cache_bytes"]) > 0


def test_render_can_export_mp3_chunks(tmp_path: Path) -> None:
    service = GuiEngineService()
    session = _build_session(tmp_path, track_count=2, duration_ms=120_000)
    settings = GuiSettings(
        songs_folder=tmp_path,
        output_path=tmp_path / "mix.mp3",
        output_chunks_enabled=True,
        output_chunk_minutes=10,
    )

    service._render_staged_fallback = lambda **_kwargs: None  # type: ignore[method-assign]
    service._final_output_audibility_state = lambda **_kwargs: ("audible", "ok")  # type: ignore[method-assign]
    service._split_output_into_chunks = lambda **_kwargs: [tmp_path / "mix_part_001.mp3"]  # type: ignore[method-assign]
    artifacts = service.render(session=session, settings=settings)
    assert artifacts.chunk_output_paths == [tmp_path / "mix_part_001.mp3"]


def test_resolve_initial_paths_excludes_output_and_rain_variants(tmp_path: Path, monkeypatch) -> None:
    service = GuiEngineService()
    song_a = tmp_path / "song_a.mp3"
    song_b = tmp_path / "song_b.mp3"
    output_mp3 = tmp_path / "mix.mp3"
    output_wav = tmp_path / "mix.wav"
    rain = tmp_path / "rain.wav"
    for path in (song_a, song_b, output_mp3, output_wav, rain):
        path.write_bytes(b"x")

    settings = GuiSettings(
        songs_folder=tmp_path,
        output_path=tmp_path / "mix",
        rain_path=rain,
    )
    config = service._build_run_config(settings)

    monkeypatch.setattr(
        "nightfall_desktop.services.engine_service.discover_audio_files",
        lambda _folder: [song_a, output_mp3, rain, song_b, output_wav],
    )

    resolved = service._resolve_initial_paths(config=config, ordered_paths=None)
    assert resolved == [song_a, song_b]
