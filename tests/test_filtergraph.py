from pathlib import Path

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.config import PresetName, QualityMode, RunConfig
from nightfall_mix.effects_presets import get_preset
from nightfall_mix.ffmpeg_graph import build_ffmpeg_command, build_filtergraph
from nightfall_mix.mixer import TrackInstance, TrackSource, build_mix_plan


def _stub_track(tmp_path: Path, name: str, duration_ms: int, track_id: str) -> TrackSource:
    p = tmp_path / name
    p.write_bytes(b"stub")
    return TrackSource(id=track_id, path=p, duration_ms=duration_ms)


def test_filtergraph_contains_expected_blocks(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
    rain = tmp_path / "rain.mp3"
    rain.write_bytes(b"stub")
    output = tmp_path / "mix.mp3"

    t0 = _stub_track(songs_folder, "a.mp3", 30_000, "t0")
    t1 = _stub_track(songs_folder, "b.mp3", 30_000, "t1")
    instances = [
        TrackInstance(instance_index=0, track=t0, cycle_index=0),
        TrackInstance(instance_index=1, track=t1, cycle_index=0),
    ]
    analyses = {
        "t0": TrackAnalysis(track_id="t0"),
        "t1": TrackAnalysis(track_id="t1"),
    }
    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=6.0,
        smart_crossfade=False,
        target_duration_min=None,
    )
    cfg = RunConfig(
        songs_folder=songs_folder,
        output=output,
        rain=rain,
        quality_mode=QualityMode.best,
        preset=PresetName.tokyo_cassette,
    )
    graph = build_filtergraph(
        mix_plan=plan,
        analyses=analyses,
        config=cfg,
        preset=get_preset(cfg.preset),
        include_master=True,
        include_rain=True,
        per_track_processing=True,
    )
    assert "acrossfade" in graph
    assert "highpass=f=200" in graph
    assert "loudnorm" in graph
    assert "[outa]" in graph


def test_filtergraph_preview_adds_trim(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
    output = tmp_path / "mix.mp3"
    t0 = _stub_track(songs_folder, "a.mp3", 40_000, "t0")
    instances = [TrackInstance(instance_index=0, track=t0, cycle_index=0)]
    analyses = {"t0": TrackAnalysis(track_id="t0")}
    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=6.0,
        smart_crossfade=False,
        target_duration_min=None,
    )
    cfg = RunConfig(
        songs_folder=songs_folder,
        output=output,
        quality_mode=QualityMode.best,
        preset=PresetName.tokyo_cassette,
    )
    graph = build_filtergraph(
        mix_plan=plan,
        analyses=analyses,
        config=cfg,
        preset=get_preset(cfg.preset),
        include_master=True,
        include_rain=False,
        per_track_processing=True,
        preview_start_sec=5.0,
        preview_duration_sec=60.0,
    )
    assert "atrim=start=5.000:duration=60.000" in graph


def test_ffmpeg_command_includes_metadata_tags(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
    output = tmp_path / "mix.mp3"
    t0 = _stub_track(songs_folder, "a.mp3", 40_000, "t0")
    instances = [TrackInstance(instance_index=0, track=t0, cycle_index=0)]
    analyses = {"t0": TrackAnalysis(track_id="t0")}
    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=6.0,
        smart_crossfade=False,
        target_duration_min=None,
    )
    cfg = RunConfig(
        songs_folder=songs_folder,
        output=output,
        quality_mode=QualityMode.best,
        preset=PresetName.tokyo_cassette,
        metadata_tags={
            "title": "Night Session",
            "artist": "Nightfall",
            "album": "Tokyo Rain",
        },
    )
    filter_script = tmp_path / "graph.txt"
    filter_script.write_text("[0:a]anull[outa]", encoding="utf-8")
    cmd = build_ffmpeg_command(
        mix_plan=plan,
        config=cfg,
        filter_script_path=filter_script,
        output_path=output,
        include_rain=False,
    )
    assert "-metadata" in cmd
    assert "title=Night Session" in cmd
    assert "artist=Nightfall" in cmd
    assert "album=Tokyo Rain" in cmd
