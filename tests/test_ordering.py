from pathlib import Path

import pytest

from nightfall_mix.analysis import TrackAnalysis
from nightfall_mix.config import OrderMode, SmartOrderingMode
from nightfall_mix.mixer import (
    TrackSource,
    discover_audio_files,
    order_sources_by_transition_fit,
    order_track_paths,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stub")


def test_alpha_ordering(tmp_path: Path) -> None:
    files = [tmp_path / "z.mp3", tmp_path / "A.mp3", tmp_path / "m.wav"]
    for file_path in files:
        _touch(file_path)
    ordered = order_track_paths(files, order=OrderMode.alpha, songs_folder=tmp_path, seed=123)
    assert [p.name for p in ordered] == ["A.mp3", "m.wav", "z.mp3"]


def test_random_ordering_is_seeded(tmp_path: Path) -> None:
    files = [tmp_path / f"{i}.mp3" for i in range(6)]
    for file_path in files:
        _touch(file_path)
    first = order_track_paths(files, order=OrderMode.random, songs_folder=tmp_path, seed=77)
    second = order_track_paths(files, order=OrderMode.random, songs_folder=tmp_path, seed=77)
    assert [p.name for p in first] == [p.name for p in second]


def test_alpha_ordering_uses_natural_numeric_sort(tmp_path: Path) -> None:
    files = [tmp_path / "10.mp3", tmp_path / "2.mp3", tmp_path / "1.mp3", tmp_path / "11.mp3"]
    for file_path in files:
        _touch(file_path)
    ordered = order_track_paths(files, order=OrderMode.alpha, songs_folder=tmp_path, seed=123)
    assert [p.name for p in ordered] == ["1.mp3", "2.mp3", "10.mp3", "11.mp3"]


def test_discover_audio_files_uses_natural_numeric_sort(tmp_path: Path) -> None:
    for name in ("1.mp3", "10.mp3", "2.mp3"):
        _touch(tmp_path / name)
    discovered = discover_audio_files(tmp_path)
    assert [p.name for p in discovered] == ["1.mp3", "2.mp3", "10.mp3"]


def test_m3u_ordering(tmp_path: Path) -> None:
    song1 = tmp_path / "one.mp3"
    song2 = tmp_path / "two.mp3"
    _touch(song1)
    _touch(song2)
    (tmp_path / "list.m3u").write_text("two.mp3\none.mp3\n", encoding="utf-8")

    ordered = order_track_paths([song1, song2], order=OrderMode.m3u, songs_folder=tmp_path, seed=None)
    assert [p.name for p in ordered] == ["two.mp3", "one.mp3"]


def test_m3u_missing_entry_raises(tmp_path: Path) -> None:
    song1 = tmp_path / "one.mp3"
    _touch(song1)
    (tmp_path / "list.m3u").write_text("missing.mp3\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        order_track_paths([song1], order=OrderMode.m3u, songs_folder=tmp_path, seed=None)


def test_m3u_partial_missing_entries_warn_and_continue(tmp_path: Path) -> None:
    song1 = tmp_path / "one.mp3"
    song2 = tmp_path / "two.mp3"
    _touch(song1)
    _touch(song2)
    (tmp_path / "list.m3u").write_text("two.mp3\nmissing.mp3\none.mp3\n", encoding="utf-8")

    with pytest.warns(UserWarning):
        ordered = order_track_paths([song1, song2], order=OrderMode.m3u, songs_folder=tmp_path, seed=None)
    assert [p.name for p in ordered] == ["two.mp3", "one.mp3"]


def test_smart_ordering_prefers_bpm_proximity() -> None:
    sources = [
        TrackSource(id="a", path=Path("a.mp3"), duration_ms=60_000),
        TrackSource(id="b", path=Path("b.mp3"), duration_ms=60_000),
        TrackSource(id="c", path=Path("c.mp3"), duration_ms=60_000),
    ]
    analyses = {
        "a": TrackAnalysis(track_id="a", bpm=70.0, bpm_confidence=0.9, key="A:min", key_confidence=0.8),
        "b": TrackAnalysis(track_id="b", bpm=72.0, bpm_confidence=0.9, key="C:min", key_confidence=0.8),
        "c": TrackAnalysis(track_id="c", bpm=128.0, bpm_confidence=0.9, key="F#:maj", key_confidence=0.8),
    }
    ordered = order_sources_by_transition_fit(sources, analyses, mode=SmartOrderingMode.bpm_key_balanced)
    names = [s.id for s in ordered]
    assert names.index("a") < names.index("c")
    assert names.index("b") < names.index("c")


def test_smart_ordering_mode_changes_priority() -> None:
    sources = [
        TrackSource(id="seed", path=Path("seed.mp3"), duration_ms=60_000),
        TrackSource(id="bpm_close_key_far", path=Path("bpm_close_key_far.mp3"), duration_ms=60_000),
        TrackSource(id="bpm_far_key_close", path=Path("bpm_far_key_close.mp3"), duration_ms=60_000),
    ]
    analyses = {
        "seed": TrackAnalysis(track_id="seed", bpm=100.0, bpm_confidence=0.9, key="A:min", key_confidence=0.9),
        "bpm_close_key_far": TrackAnalysis(
            track_id="bpm_close_key_far",
            bpm=102.0,
            bpm_confidence=0.9,
            key="E:maj",
            key_confidence=0.9,
        ),
        "bpm_far_key_close": TrackAnalysis(
            track_id="bpm_far_key_close",
            bpm=112.0,
            bpm_confidence=0.9,
            key="A:min",
            key_confidence=0.9,
        ),
    }

    bpm_first = order_sources_by_transition_fit(sources, analyses, mode=SmartOrderingMode.bpm_first)
    balanced = order_sources_by_transition_fit(sources, analyses, mode=SmartOrderingMode.bpm_key_balanced)

    bpm_first_ids = [s.id for s in bpm_first]
    balanced_ids = [s.id for s in balanced]
    assert bpm_first_ids.index("bpm_close_key_far") < bpm_first_ids.index("bpm_far_key_close")
    assert balanced_ids.index("bpm_far_key_close") < balanced_ids.index("bpm_close_key_far")
