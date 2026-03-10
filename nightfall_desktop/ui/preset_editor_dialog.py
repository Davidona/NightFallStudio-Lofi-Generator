from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from nightfall_desktop.models.session_models import PresetOverrides
from nightfall_mix.effects_presets import PresetSpec


class PresetEditorDialog(QDialog):
    def __init__(
        self,
        overrides: PresetOverrides,
        base_preset: PresetSpec,
        preset_name: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._base_preset = base_preset
        self.setWindowTitle(f"Preset Editor - {preset_name}")
        self.setMinimumSize(460, 700)

        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)

        self._hpf = self._float_spin(20.0, 180.0, 1.0, overrides.hpf_hz or base_preset.hpf_hz)
        self._lpf = self._float_spin(4000.0, 18000.0, 100.0, overrides.lpf_hz or base_preset.lpf_hz)
        self._lpf_q = self._float_spin(0.4, 2.0, 0.05, overrides.lpf_q or base_preset.lpf_q, decimals=2)
        body_layout.addWidget(
            self._group(
                "Filters",
                [
                    ("HPF Cutoff (Hz)", self._hpf),
                    ("LPF Cutoff (Hz)", self._lpf),
                    ("LPF Resonance (Q)", self._lpf_q),
                ],
            )
        )

        self._sat = self._float_spin(
            0.3,
            1.8,
            0.05,
            overrides.saturation_scale or base_preset.saturation_scale,
            decimals=2,
        )
        self._tape_drive = self._float_spin(
            0.5,
            2.0,
            0.05,
            overrides.tape_drive or base_preset.tape_drive,
            decimals=2,
        )
        self._tape_bias = self._float_spin(
            -0.5,
            0.5,
            0.01,
            overrides.tape_bias if overrides.tape_bias is not None else base_preset.tape_bias,
            decimals=2,
        )
        body_layout.addWidget(
            self._group(
                "Saturation",
                [
                    ("Saturation Scale", self._sat),
                    ("Tape Drive", self._tape_drive),
                    ("Tape Bias", self._tape_bias),
                ],
            )
        )

        self._comp = self._float_spin(
            0.3,
            1.8,
            0.05,
            overrides.compression_scale or base_preset.compression_scale,
            decimals=2,
        )
        self._attack = self._float_spin(
            1.0,
            200.0,
            1.0,
            overrides.comp_attack_ms or base_preset.comp_attack_ms,
            decimals=1,
        )
        self._release = self._float_spin(
            40.0,
            1200.0,
            5.0,
            overrides.comp_release_ms or base_preset.comp_release_ms,
            decimals=1,
        )
        self._ratio = self._float_spin(
            1.0,
            8.0,
            0.1,
            overrides.comp_ratio or base_preset.comp_ratio,
            decimals=2,
        )
        body_layout.addWidget(
            self._group(
                "Compression",
                [
                    ("Compression Scale", self._comp),
                    ("Compressor Attack (ms)", self._attack),
                    ("Compressor Release (ms)", self._release),
                    ("Compression Ratio", self._ratio),
                ],
            )
        )

        self._bit_depth = self._int_spin(8, 16, overrides.bit_depth or base_preset.bit_depth)
        self._sample_rate = self._float_spin(
            8000.0,
            44100.0,
            500.0,
            overrides.sample_rate_reduction_hz or base_preset.sample_rate_reduction_hz,
            decimals=0,
        )
        body_layout.addWidget(
            self._group(
                "Sampler",
                [
                    ("Bit Depth", self._bit_depth),
                    ("Sample Rate Reduction", self._sample_rate),
                ],
            )
        )

        self._wow_depth = self._float_spin(
            0.0,
            0.02,
            0.0005,
            overrides.wow_depth if overrides.wow_depth is not None else base_preset.wow_depth,
            decimals=4,
        )
        self._wow_rate = self._float_spin(
            0.0,
            2.0,
            0.01,
            overrides.wow_rate_hz if overrides.wow_rate_hz is not None else base_preset.wow_rate_hz,
            decimals=3,
        )
        self._flutter_depth = self._float_spin(
            0.0,
            0.01,
            0.0002,
            overrides.flutter_depth if overrides.flutter_depth is not None else base_preset.flutter_depth,
            decimals=4,
        )
        self._flutter_rate = self._float_spin(
            0.0,
            12.0,
            0.1,
            overrides.flutter_rate_hz if overrides.flutter_rate_hz is not None else base_preset.flutter_rate_hz,
            decimals=2,
        )
        body_layout.addWidget(
            self._group(
                "Tape Instability",
                [
                    ("Wow Depth", self._wow_depth),
                    ("Wow Rate", self._wow_rate),
                    ("Flutter Depth", self._flutter_depth),
                    ("Flutter Rate", self._flutter_rate),
                ],
            )
        )

        self._stereo_width = self._float_spin(
            0.4,
            1.2,
            0.01,
            overrides.stereo_width or base_preset.stereo_width,
            decimals=2,
        )
        body_layout.addWidget(
            self._group(
                "Stereo",
                [("Stereo Width", self._stereo_width)],
            )
        )

        self._vinyl_noise = self._float_spin(
            -120.0,
            -20.0,
            1.0,
            overrides.vinyl_noise_level_db
            if overrides.vinyl_noise_level_db is not None
            else base_preset.vinyl_noise_level_db,
            decimals=1,
        )
        self._tape_hiss = self._float_spin(
            -120.0,
            -20.0,
            1.0,
            overrides.tape_hiss_level_db
            if overrides.tape_hiss_level_db is not None
            else base_preset.tape_hiss_level_db,
            decimals=1,
        )
        body_layout.addWidget(
            self._group(
                "Noise Layer",
                [
                    ("Vinyl Noise Level", self._vinyl_noise),
                    ("Tape Hiss Level", self._tape_hiss),
                ],
            )
        )

        self._atmo_volume = self._float_spin(
            -18.0,
            18.0,
            1.0,
            overrides.atmosphere_volume_db
            if overrides.atmosphere_volume_db is not None
            else base_preset.atmosphere_volume_db,
            decimals=1,
        )
        self._atmo_width = self._float_spin(
            0.0,
            1.2,
            0.01,
            overrides.atmosphere_stereo_width
            if overrides.atmosphere_stereo_width is not None
            else base_preset.atmosphere_stereo_width,
            decimals=2,
        )
        self._atmo_lpf = self._float_spin(
            2000.0,
            18000.0,
            100.0,
            overrides.atmosphere_lpf_hz or base_preset.atmosphere_lpf_hz,
            decimals=0,
        )
        body_layout.addWidget(
            self._group(
                "Atmosphere",
                [
                    ("Atmosphere Volume (dB)", self._atmo_volume),
                    ("Atmosphere Stereo Width", self._atmo_width),
                    ("Atmosphere LPF", self._atmo_lpf),
                ],
            )
        )

        hint = QLabel(
            "Values are preset-specific baselines. Resetting a preset returns every field to the preset default. "
            "Atmosphere volume stacks with the main rain level control."
        )
        hint.setWordWrap(True)
        body_layout.addWidget(hint)
        body_layout.addStretch(1)

        scroll.setWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addWidget(scroll)
        layout.addWidget(buttons)

    def _group(self, title: str, rows: list[tuple[str, QWidget]]) -> QGroupBox:
        box = QGroupBox(title)
        form = QFormLayout(box)
        for label, widget in rows:
            form.addRow(label, widget)
        return box

    def _float_spin(
        self,
        minimum: float,
        maximum: float,
        step: float,
        value: float,
        decimals: int = 2,
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        return spin

    def _int_spin(self, minimum: int, maximum: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    @staticmethod
    def _maybe_float_override(value: float, base: float, tolerance: float = 1e-6) -> Optional[float]:
        return None if abs(value - base) <= tolerance else value

    @staticmethod
    def _maybe_int_override(value: int, base: int) -> Optional[int]:
        return None if value == base else value

    def value(self) -> PresetOverrides:
        base = self._base_preset
        return PresetOverrides(
            lpf_hz=self._maybe_float_override(self._lpf.value(), base.lpf_hz),
            hpf_hz=self._maybe_float_override(self._hpf.value(), base.hpf_hz),
            lpf_q=self._maybe_float_override(self._lpf_q.value(), base.lpf_q),
            saturation_scale=self._maybe_float_override(self._sat.value(), base.saturation_scale),
            tape_drive=self._maybe_float_override(self._tape_drive.value(), base.tape_drive),
            tape_bias=self._maybe_float_override(self._tape_bias.value(), base.tape_bias),
            compression_scale=self._maybe_float_override(self._comp.value(), base.compression_scale),
            comp_attack_ms=self._maybe_float_override(self._attack.value(), base.comp_attack_ms),
            comp_release_ms=self._maybe_float_override(self._release.value(), base.comp_release_ms),
            comp_ratio=self._maybe_float_override(self._ratio.value(), base.comp_ratio),
            bit_depth=self._maybe_int_override(self._bit_depth.value(), base.bit_depth),
            sample_rate_reduction_hz=self._maybe_float_override(
                self._sample_rate.value(),
                base.sample_rate_reduction_hz,
            ),
            wow_depth=self._maybe_float_override(self._wow_depth.value(), base.wow_depth),
            wow_rate_hz=self._maybe_float_override(self._wow_rate.value(), base.wow_rate_hz),
            flutter_depth=self._maybe_float_override(self._flutter_depth.value(), base.flutter_depth),
            flutter_rate_hz=self._maybe_float_override(self._flutter_rate.value(), base.flutter_rate_hz),
            stereo_width=self._maybe_float_override(self._stereo_width.value(), base.stereo_width),
            vinyl_noise_level_db=self._maybe_float_override(
                self._vinyl_noise.value(),
                base.vinyl_noise_level_db,
            ),
            tape_hiss_level_db=self._maybe_float_override(
                self._tape_hiss.value(),
                base.tape_hiss_level_db,
            ),
            atmosphere_volume_db=self._maybe_float_override(
                self._atmo_volume.value(),
                base.atmosphere_volume_db,
            ),
            atmosphere_stereo_width=self._maybe_float_override(
                self._atmo_width.value(),
                base.atmosphere_stereo_width,
            ),
            atmosphere_lpf_hz=self._maybe_float_override(
                self._atmo_lpf.value(),
                base.atmosphere_lpf_hz,
            ),
        )
