from __future__ import annotations

import pytest

from nightfall_desktop.services.media_tools_service import _build_chunk_graph_lines


def _fades(count: int) -> dict[int, float]:
    return {i: float(i) for i in range(1, count)}


class TestChunkGraph:
    def test_single_file_no_fades(self) -> None:
        lines = _build_chunk_graph_lines(
            start=0,
            end=1,
            carry_fade=None,
            next_fade=None,
            durations_s=[10.0],
            fades={},
        )
        assert "[0:v]setpts=PTS-STARTPTS,format=yuv420p[v0]" in lines
        assert "[v0]concat=n=1:v=1:a=0[vout]" in lines
        assert "[a0]anull[aout]" in lines
        assert not any("acrossfade" in line for line in lines)

    def test_first_chunk_has_no_carry_in_and_keeps_full_last_file(self) -> None:
        lines = _build_chunk_graph_lines(
            start=0,
            end=4,
            carry_fade=None,
            next_fade=None,
            durations_s=[10.0, 20.0, 30.0, 40.0],
            fades=_fades(4),
        )
        assert not any("atrim" in line for line in lines)
        assert "[v0][v1][v2][v3]concat=n=4:v=1:a=0[vout]" in lines
        # No carry-in: pair (i, i+1) uses fades[start + i + 1].
        assert any("[a0][a1]acrossfade=d=1.000" in line for line in lines)
        assert any("[af1][a2]acrossfade=d=2.000" in line for line in lines)
        assert any("[af2][a3]acrossfade=d=3.000" in line for line in lines)

    def test_middle_chunk_carry_in_audio_only_and_last_trim(self) -> None:
        lines = _build_chunk_graph_lines(
            start=4,
            end=10,
            carry_fade=2.0,
            next_fade=3.0,
            durations_s=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0],
            fades=_fades(10),
        )
        # Carry-in: audio-only, trimmed to its fade tail (40 - 2 = 38s).
        assert any("atrim=start=38.000" in line for line in lines)
        assert any("atrim=start" in line and "[0:a]" in line for line in lines)
        # Last file: audio trimmed by next fade (100 - 3 = 97s), video kept.
        assert any("atrim=end=97.000" in line for line in lines)
        # Video excludes the carry-in: 6 new files, indices 1..6.
        assert "[v1][v2][v3][v4][v5][v6]concat=n=6:v=1:a=0[vout]" in lines
        # With carry-in: pair (i, i+1) uses fades[start + i].
        assert any("[a0][a1]acrossfade=d=4.000" in line for line in lines)
        assert any("[af1][a2]acrossfade=d=5.000" in line for line in lines)

    def test_last_chunk_keeps_full_last_file(self) -> None:
        lines = _build_chunk_graph_lines(
            start=6,
            end=10,
            carry_fade=1.5,
            next_fade=None,
            durations_s=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0],
            fades=_fades(10),
        )
        assert any("atrim=end" in line for line in lines) is False
        assert any("atrim=start=58.500" in line for line in lines)
        # Last pair of the chunk: fades[start + 3] = fades[9].
        assert any("acrossfade=d=9.000" in line and line.endswith("[aout]") for line in lines)

    def test_carry_in_with_single_new_file(self) -> None:
        lines = _build_chunk_graph_lines(
            start=6,
            end=7,
            carry_fade=2.0,
            next_fade=None,
            durations_s=[10.0] * 7,
            fades=_fades(7),
        )
        assert "[v1]concat=n=1:v=1:a=0[vout]" in lines
        assert any("[a0][a1]acrossfade=d=6.000:c1=qsin:c2=qsin[aout]" in line for line in lines)

    def test_chunk_of_one_after_first(self) -> None:
        lines = _build_chunk_graph_lines(
            start=1,
            end=2,
            carry_fade=2.0,
            next_fade=None,
            durations_s=[10.0, 20.0],
            fades=_fades(2),
        )
        assert "[v1]concat=n=1:v=1:a=0[vout]" in lines
        assert any("[a0][a1]acrossfade=d=1.000" in line for line in lines)
