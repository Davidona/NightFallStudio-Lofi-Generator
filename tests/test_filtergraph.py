from pathlib import Path

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.config import CrossfadeCurve, PresetName, QualityMode, RenderStyle, RunConfig
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
    assert "highpass=f=55.0" in graph
    assert "lowpass=f=11000.0:t=q:w=0.707" in graph
    assert "acrusher=bits=14" in graph
    assert "loudnorm" in graph
    assert "[outa]" in graph


def test_filtergraph_rain_presence_upfront_preserves_more_body(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
    rain = tmp_path / "rain.mp3"
    rain.write_bytes(b"stub")
    output = tmp_path / "mix.mp3"

    t0 = _stub_track(songs_folder, "a.mp3", 30_000, "t0")
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
        rain=rain,
        quality_mode=QualityMode.best,
        preset=PresetName.rainy_study,
        rain_presence="upfront",
        rain_preserve_low_drops=True,
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
    assert "highpass=f=45.0" in graph
    assert "lowpass=f=9200.0:t=q:w=0.707" in graph


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


def test_filtergraph_playlist_mode_skips_lofi_coloration(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
    output = tmp_path / "playlist.mp3"

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
        quality_mode=QualityMode.best,
        preset=PresetName.tokyo_cassette,
        render_style=RenderStyle.clean_playlist,
    )
    graph = build_filtergraph(
        mix_plan=plan,
        analyses=analyses,
        config=cfg,
        preset=get_preset(cfg.preset),
        include_master=True,
        include_rain=False,
        per_track_processing=True,
    )
    assert "acrossfade" in graph
    assert "loudnorm" in graph
    assert "acrusher" not in graph
    assert "anoisesrc" not in graph
    assert "vibrato" not in graph


def test_filtergraph_playlist_mode_keeps_smart_crossfade_transition(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
    output = tmp_path / "playlist.mp3"

    t0 = _stub_track(songs_folder, "a.mp3", 30_000, "t0")
    t1 = _stub_track(songs_folder, "b.mp3", 30_000, "t1")
    instances = [
        TrackInstance(instance_index=0, track=t0, cycle_index=0),
        TrackInstance(instance_index=1, track=t1, cycle_index=0),
    ]
    analyses = {
        "t0": TrackAnalysis(
            track_id="t0",
            bpm=90.0,
            bpm_confidence=0.9,
            tail_rms_curve=[-20.0] * 300,
        ),
        "t1": TrackAnalysis(
            track_id="t1",
            bpm=92.0,
            bpm_confidence=0.9,
            head_rms_curve=[-20.0] * 300,
        ),
    }
    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=6.0,
        smart_crossfade=True,
        target_duration_min=None,
    )
    assert plan.transitions[0].smart_used is True
    assert plan.transitions[0].reason.startswith("smart")

    cfg = RunConfig(
        songs_folder=songs_folder,
        output=output,
        quality_mode=QualityMode.best,
        preset=PresetName.tokyo_cassette,
        render_style=RenderStyle.clean_playlist,
        smart_crossfade=True,
    )
    graph = build_filtergraph(
        mix_plan=plan,
        analyses=analyses,
        config=cfg,
        preset=get_preset(cfg.preset),
        include_master=True,
        include_rain=False,
        per_track_processing=True,
    )
    assert f"acrossfade=d={plan.transitions[0].crossfade_ms / 1000.0:.3f}" in graph


def test_filtergraph_uses_configured_crossfade_curve(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
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
        quality_mode=QualityMode.best,
        preset=PresetName.tokyo_cassette,
        crossfade_curve=CrossfadeCurve.exponential,
    )
    graph = build_filtergraph(
        mix_plan=plan,
        analyses=analyses,
        config=cfg,
        preset=get_preset(cfg.preset),
        include_master=True,
        include_rain=False,
        per_track_processing=True,
    )
    assert "c1=exp:c2=exp" in graph
    assert "c1=qsin:c2=qsin" not in graph


def test_filtergraph_key_mask_duck_is_confined_to_transition(tmp_path: Path) -> None:
    songs_folder = tmp_path / "songs"
    songs_folder.mkdir()
    output = tmp_path / "mix.mp3"

    t0 = _stub_track(songs_folder, "a.mp3", 30_000, "t0")
    t1 = _stub_track(songs_folder, "b.mp3", 30_000, "t1")
    instances = [
        TrackInstance(instance_index=0, track=t0, cycle_index=0),
        TrackInstance(instance_index=1, track=t1, cycle_index=0),
    ]
    # Distant keys (C vs F#, distance 6) with confident detection trigger the
    # key-mask LPF duck across this transition.
    analyses = {
        "t0": TrackAnalysis(track_id="t0", key="C", key_confidence=0.9),
        "t1": TrackAnalysis(track_id="t1", key="F#", key_confidence=0.9),
    }
    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=6.0,
        smart_crossfade=True,
        target_duration_min=None,
    )
    assert plan.transitions[0].lpf_duck_ms is not None

    cfg = RunConfig(
        songs_folder=songs_folder,
        output=output,
        quality_mode=QualityMode.best,
        preset=PresetName.tokyo_cassette,
        smart_crossfade=True,
    )
    graph = build_filtergraph(
        mix_plan=plan,
        analyses=analyses,
        config=cfg,
        preset=get_preset(cfg.preset),
        include_master=True,
        include_rain=False,
        per_track_processing=True,
    )
    # The duck lowpass must be time-gated (enable=...) rather than applied to
    # the whole track, and must not affect the non-masked regions.
    assert "lowpass=f=7800.0:t=q:w=0.707:enable='gt(t," in graph  # outgoing tail
    assert "lowpass=f=7800.0:t=q:w=0.707:enable='lt(t," in graph  # incoming head
    assert "lowpass=f=7800:t=q:w=0.707," not in graph  # never ungated

