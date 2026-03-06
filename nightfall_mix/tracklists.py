from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.mixer import MixPlan
from nightfall_mix.utils import format_hms, write_json


@dataclass
class TracklistArtifacts:
    tracklist_txt_path: Path
    tracklist_json_path: Path
    timestamps_txt_path: Path
    timestamps_csv_path: Path


def write_tracklist_artifacts(
    plan: MixPlan,
    analyses: dict[str, TrackAnalysis],
    output_path: Path,
) -> TracklistArtifacts:
    txt_path = output_path.with_name("tracklist.txt")
    json_path = output_path.with_name("tracklist.json")
    timestamps_txt_path = output_path.with_name("mix_timestamps.txt")
    timestamps_csv_path = output_path.with_name("mix_timestamps.csv")

    txt_path.parent.mkdir(parents=True, exist_ok=True)
    with txt_path.open("w", encoding="utf-8") as f:
        for entry in plan.timeline:
            f.write(f"{format_hms(entry.start_time_ms)} | {entry.filename}\n")

    payload = []
    for entry in plan.timeline:
        analysis = analyses.get(entry.track_id)
        payload.append(
            {
                "filename": entry.filename,
                "start_time_ms": entry.start_time_ms,
                "end_time_ms": entry.end_time_ms,
                "bpm": analysis.bpm if analysis else None,
                "bpm_confidence": analysis.bpm_confidence if analysis else None,
                "key": analysis.key if analysis else None,
                "key_confidence": analysis.key_confidence if analysis else None,
                "loudness": {
                    "input_i": analysis.loudness.input_i if analysis else None,
                    "input_tp": analysis.loudness.input_tp if analysis else None,
                    "input_lra": analysis.loudness.input_lra if analysis else None,
                },
            }
        )
    write_json(json_path, payload)

    with timestamps_txt_path.open("w", encoding="utf-8") as f:
        header = (
            "# | Filename | Start | End | Duration | InCrossfadeMs | OutCrossfadeMs | "
            "InReason | OutReason"
        )
        f.write(header + "\n")
        for idx, entry in enumerate(plan.timeline, start=1):
            in_transition = plan.transitions[idx - 2] if idx > 1 else None
            out_transition = plan.transitions[idx - 1] if idx <= len(plan.transitions) else None
            duration_ms = max(0, entry.end_time_ms - entry.start_time_ms)
            f.write(
                f"{idx} | {entry.filename} | {format_hms(entry.start_time_ms)} | "
                f"{format_hms(entry.end_time_ms)} | {format_hms(duration_ms)} | "
                f"{in_transition.crossfade_ms if in_transition else 0} | "
                f"{out_transition.crossfade_ms if out_transition else 0} | "
                f"{in_transition.reason if in_transition else ''} | "
                f"{out_transition.reason if out_transition else ''}\n"
            )

    with timestamps_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Index",
                "Filename",
                "Start",
                "End",
                "Duration",
                "InCrossfadeMs",
                "OutCrossfadeMs",
                "InReason",
                "OutReason",
            ],
        )
        writer.writeheader()
        for idx, entry in enumerate(plan.timeline, start=1):
            in_transition = plan.transitions[idx - 2] if idx > 1 else None
            out_transition = plan.transitions[idx - 1] if idx <= len(plan.transitions) else None
            duration_ms = max(0, entry.end_time_ms - entry.start_time_ms)
            writer.writerow(
                {
                    "Index": idx,
                    "Filename": entry.filename,
                    "Start": format_hms(entry.start_time_ms),
                    "End": format_hms(entry.end_time_ms),
                    "Duration": format_hms(duration_ms),
                    "InCrossfadeMs": in_transition.crossfade_ms if in_transition else 0,
                    "OutCrossfadeMs": out_transition.crossfade_ms if out_transition else 0,
                    "InReason": in_transition.reason if in_transition else "",
                    "OutReason": out_transition.reason if out_transition else "",
                }
            )

    return TracklistArtifacts(
        tracklist_txt_path=txt_path,
        tracklist_json_path=json_path,
        timestamps_txt_path=timestamps_txt_path,
        timestamps_csv_path=timestamps_csv_path,
    )
