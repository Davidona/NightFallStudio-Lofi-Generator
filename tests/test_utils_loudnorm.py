from __future__ import annotations

from nightfall_mix.utils import parse_loudnorm_json


def test_parse_loudnorm_json_drops_non_finite_values() -> None:
    stderr_text = """
    [Parsed_loudnorm_0 @ 000001] {
      "input_i" : "-13.70",
      "input_tp" : "-0.20",
      "input_lra" : "2.10",
      "input_thresh" : "-24.00",
      "target_offset" : "nan",
      "normalization_type" : "dynamic"
    }
    """
    parsed = parse_loudnorm_json(stderr_text)
    assert parsed is not None
    assert parsed["input_i"] == -13.70
    assert parsed["input_tp"] == -0.20
    assert parsed["input_lra"] == 2.10
    assert parsed["input_thresh"] == -24.00
    assert "target_offset" not in parsed
