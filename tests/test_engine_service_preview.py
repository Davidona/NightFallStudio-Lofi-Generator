from __future__ import annotations

from pathlib import Path

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.mixer import TrackInstance, TrackSource, build_mix_plan
from nightfall_desktop.models.session_models import EngineSessionModel, GuiSettings
from nightfall_desktop.services.engine_service import GuiEngineService


def _build_session(track_durations_ms: list[int]) -> EngineSessionModel:
    tracks: list[TrackSource] = []
    analyses: dict[str, TrackAnalysis] = {}
    instances: list[TrackInstance] = []
    for idx, duration_ms in enumerate(track_durations_ms):
        source = TrackSource(id=f"t{idx}", path=Path(f"track_{idx}.mp3"), duration_ms=duration_ms)
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
        songs_folder=Path("."),
        output_path=Path("mix.mp3"),
    )
    return EngineSessionModel(
        settings=settings,
        ordered_paths=[s.path for s in tracks],
        track_sources=tracks,
        analyses=analyses,
        mix_plan=plan,
        warnings=[],
    )


def test_preview_optimization_keeps_two_tracks_for_typical_60s_window() -> None:
    service = GuiEngineService()
    session = _build_session([180_000, 180_000, 180_000, 180_000])
    ordered = service._preview_ordered_paths(session, preview_duration_sec=60.0)
    assert [p.name for p in ordered] == ["track_0.mp3", "track_1.mp3"]


def test_preview_optimization_expands_when_preview_window_longer() -> None:
    service = GuiEngineService()
    session = _build_session([60_000, 60_000, 60_000, 60_000, 60_000])
    ordered = service._preview_ordered_paths(session, preview_duration_sec=180.0)
    assert [p.name for p in ordered] == ["track_0.mp3", "track_1.mp3", "track_2.mp3", "track_3.mp3"]
