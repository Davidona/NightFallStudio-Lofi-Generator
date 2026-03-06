import json
from pathlib import Path

import pytest

from nightfall_mix.config import PresetName, SmartOrderingMode
from nightfall_desktop.models.session_models import GuiSettings, PresetOverrides, WorkspaceMode
from nightfall_desktop.services.project_service import load_project_file, save_project_file


def test_project_save_load_roundtrip(tmp_path: Path) -> None:
    songs = tmp_path / "songs"
    songs.mkdir()
    settings = GuiSettings(
        songs_folder=songs,
        output_path=tmp_path / "mix.mp3",
        cache_folder=tmp_path / "cache",
        preset=PresetName.vinyl_room,
        adaptive_lofi=True,
        smart_ordering=True,
        smart_ordering_mode=SmartOrderingMode.bpm_first,
        output_chunks_enabled=True,
        output_chunk_minutes=15,
        target_duration_min=120,
        workspace_mode=WorkspaceMode.simple,
        metadata_tags={"title": "Night Session", "artist": "Nightfall"},
        preset_overrides=PresetOverrides(lpf_hz=9800.0, saturation_scale=0.8, compression_scale=1.1),
    )
    ordered = [songs / "a.mp3", songs / "b.mp3"]
    project = tmp_path / "session.nightfall"
    save_project_file(project, settings=settings, ordered_paths=ordered)

    loaded, loaded_order = load_project_file(project)
    assert loaded.songs_folder == settings.songs_folder
    assert loaded.preset == settings.preset
    assert loaded.cache_folder == settings.cache_folder
    assert loaded.adaptive_lofi is True
    assert loaded.smart_ordering is True
    assert loaded.smart_ordering_mode == SmartOrderingMode.bpm_first
    assert loaded.output_chunks_enabled is True
    assert loaded.output_chunk_minutes == 15
    assert loaded.workspace_mode == WorkspaceMode.simple
    assert loaded.metadata_tags == {"title": "Night Session", "artist": "Nightfall"}
    assert loaded.preset_overrides.lpf_hz == 9800.0
    assert loaded.preset_overrides_by_name[PresetName.vinyl_room].lpf_hz == 9800.0
    assert [str(x) for x in loaded_order] == [str(x) for x in ordered]


def test_project_load_clamps_override_values(tmp_path: Path) -> None:
    project = tmp_path / "session.nightfall"
    payload = {
        "version": 1,
        "settings": {
            "songs_folder": str(tmp_path / "songs"),
            "output_path": str(tmp_path / "mix.mp3"),
            "adaptive_lofi": "false",
            "preset_overrides": {
                "lpf_hz": 30000,
                "saturation_scale": 10.0,
                "compression_scale": -2.0,
            },
        },
        "ordered_paths": [],
    }
    project.write_text(json.dumps(payload), encoding="utf-8")

    loaded, _ = load_project_file(project)
    assert loaded.adaptive_lofi is False
    assert loaded.preset_overrides.lpf_hz == 18000.0
    assert loaded.preset_overrides.saturation_scale == 1.5
    assert loaded.preset_overrides.compression_scale == 0.3


def test_project_load_rejects_unsupported_version(tmp_path: Path) -> None:
    project = tmp_path / "session_v2.nightfall"
    payload = {
        "version": 99,
        "settings": {
            "songs_folder": str(tmp_path / "songs"),
            "output_path": str(tmp_path / "mix.mp3"),
        },
    }
    project.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_project_file(project)


def test_project_load_parses_per_preset_overrides(tmp_path: Path) -> None:
    project = tmp_path / "session.nightfall"
    payload = {
        "version": 1,
        "settings": {
            "songs_folder": str(tmp_path / "songs"),
            "output_path": str(tmp_path / "mix.mp3"),
            "preset": "night_owl_fm",
            "preset_overrides_by_name": {
                "night_owl_fm": {
                    "lpf_hz": 6100.0,
                    "saturation_scale": 1.3,
                    "compression_scale": 0.9,
                },
                "sunrise_clean": {
                    "lpf_hz": 16000.0,
                    "saturation_scale": 0.7,
                    "compression_scale": 0.8,
                },
            },
        },
        "ordered_paths": [],
    }
    project.write_text(json.dumps(payload), encoding="utf-8")

    loaded, _ = load_project_file(project)
    assert loaded.preset == PresetName.night_owl_fm
    assert loaded.preset_overrides.lpf_hz == 6100.0
    assert loaded.preset_overrides_by_name[PresetName.sunrise_clean].lpf_hz == 16000.0


def test_project_load_defaults_smart_ordering_mode(tmp_path: Path) -> None:
    project = tmp_path / "legacy.nightfall"
    payload = {
        "version": 1,
        "settings": {
            "songs_folder": str(tmp_path / "songs"),
            "output_path": str(tmp_path / "mix.mp3"),
            "smart_ordering": True,
        },
    }
    project.write_text(json.dumps(payload), encoding="utf-8")
    loaded, _ = load_project_file(project)
    assert loaded.smart_ordering is True
    assert loaded.smart_ordering_mode == SmartOrderingMode.bpm_key_balanced


def test_project_save_is_atomic_and_cleans_tmp(tmp_path: Path) -> None:
    songs = tmp_path / "songs"
    songs.mkdir()
    settings = GuiSettings(
        songs_folder=songs,
        output_path=tmp_path / "mix.mp3",
    )
    project = tmp_path / "atomic.nightfall"
    save_project_file(project, settings=settings, ordered_paths=[])
    assert project.exists()
    assert not (tmp_path / "atomic.nightfall.tmp").exists()


def test_project_load_defaults_workspace_mode(tmp_path: Path) -> None:
    project = tmp_path / "legacy_workspace.nightfall"
    payload = {
        "version": 1,
        "settings": {
            "songs_folder": str(tmp_path / "songs"),
            "output_path": str(tmp_path / "mix.mp3"),
        },
        "ordered_paths": [],
    }
    project.write_text(json.dumps(payload), encoding="utf-8")

    loaded, _ = load_project_file(project)
    assert loaded.workspace_mode == WorkspaceMode.advanced
