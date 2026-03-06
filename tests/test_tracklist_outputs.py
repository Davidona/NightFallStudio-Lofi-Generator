from __future__ import annotations

import csv
from pathlib import Path

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.mixer import TrackInstance, TrackSource, build_mix_plan
from nightfall_mix.tracklists import write_tracklist_artifacts


def _source(idx: int, duration_ms: int) -> TrackSource:
    return TrackSource(id=f"t{idx}", path=Path(f"track_{idx}.mp3"), duration_ms=duration_ms)


def test_write_tracklist_artifacts_includes_timestamp_files(tmp_path: Path) -> None:
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

    artifacts = write_tracklist_artifacts(plan=plan, analyses=analyses, output_path=tmp_path / "mix.mp3")

    assert artifacts.tracklist_txt_path.exists()
    assert artifacts.tracklist_json_path.exists()
    assert artifacts.timestamps_txt_path.exists()
    assert artifacts.timestamps_csv_path.exists()

    txt = artifacts.timestamps_txt_path.read_text(encoding="utf-8")
    assert "Start | End | Duration" in txt
    assert "track_0.mp3 | 00:00:00 | 00:00:30 | 00:00:30 | 0 | 6000" in txt
    assert "track_2.mp3 | 00:00:48 | 00:01:18 | 00:00:30 | 6000 | 0" in txt

    with artifacts.timestamps_csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    assert rows[0]["Filename"] == "track_0.mp3"
    assert rows[0]["Start"] == "00:00:00"
    assert rows[0]["End"] == "00:00:30"
    assert rows[0]["InCrossfadeMs"] == "0"
    assert rows[0]["OutCrossfadeMs"] == "6000"
    assert rows[1]["InReason"] == "fixed"
    assert rows[2]["OutReason"] == ""
