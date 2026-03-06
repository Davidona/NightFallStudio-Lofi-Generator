from pathlib import Path

import pytest

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.config import OrderMode
from nightfall_mix.mixer import TrackInstance, TrackSource, build_instances, build_mix_plan


def _source(idx: int, duration_ms: int) -> TrackSource:
    return TrackSource(id=f"t{idx}", path=Path(f"track_{idx}.mp3"), duration_ms=duration_ms)


def test_timestamp_calculation_fixed_crossfade() -> None:
    tracks = [_source(0, 30_000), _source(1, 30_000), _source(2, 30_000)]
    instances = [TrackInstance(instance_index=i, track=t, cycle_index=0) for i, t in enumerate(tracks)]
    analyses = {t.id: TrackAnalysis(track_id=t.id) for t in tracks}

    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=6.0,
        smart_crossfade=False,
        target_duration_min=None,
    )
    starts = [item.start_time_ms for item in plan.timeline]
    ends = [item.end_time_ms for item in plan.timeline]
    assert starts == [0, 24_000, 48_000]
    assert ends[-1] == 78_000
    assert plan.estimated_duration_ms == 78_000


def test_loop_to_target_builds_multiple_cycles() -> None:
    tracks = [_source(0, 60_000), _source(1, 60_000)]
    instances = build_instances(
        base_tracks=tracks,
        order=OrderMode.alpha,
        seed=123,
        target_duration_min=5,
    )
    assert len(instances) >= 5
    assert instances[0].cycle_index == 0
    assert instances[-1].cycle_index >= 2


@pytest.mark.parametrize("crossfade_sec", [0.5, 1.0, 2.0])
def test_fixed_crossfade_respects_low_values(crossfade_sec: float) -> None:
    tracks = [_source(0, 30_000), _source(1, 30_000)]
    instances = [TrackInstance(instance_index=i, track=t, cycle_index=0) for i, t in enumerate(tracks)]
    analyses = {t.id: TrackAnalysis(track_id=t.id) for t in tracks}

    plan = build_mix_plan(
        instances=instances,
        analyses=analyses,
        crossfade_sec=crossfade_sec,
        smart_crossfade=False,
        target_duration_min=None,
    )
    assert plan.transitions[0].crossfade_ms == int(crossfade_sec * 1000)


@pytest.mark.parametrize("crossfade_sec", [0.5, 1.0, 2.0])
def test_smart_crossfade_without_analysis_respects_low_values(crossfade_sec: float) -> None:
    tracks = [_source(0, 30_000), _source(1, 30_000)]
    instances = [TrackInstance(instance_index=i, track=t, cycle_index=0) for i, t in enumerate(tracks)]

    plan = build_mix_plan(
        instances=instances,
        analyses={},
        crossfade_sec=crossfade_sec,
        smart_crossfade=True,
        target_duration_min=None,
    )
    assert plan.transitions[0].crossfade_ms == int(crossfade_sec * 1000)
    assert plan.transitions[0].reason == "fixed-no-analysis"
