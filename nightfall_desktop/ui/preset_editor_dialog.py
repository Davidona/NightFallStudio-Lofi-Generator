from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from nightfall_desktop.models.session_models import PresetOverrides


class PresetEditorDialog(QDialog):
    def __init__(
        self,
        overrides: PresetOverrides,
        base_lpf_hz: float,
        preset_name: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Preset Editor - {preset_name}")
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._lpf = QDoubleSpinBox()
        self._lpf.setRange(4000.0, 18000.0)
        self._lpf.setSingleStep(100.0)
        self._lpf.setValue(overrides.lpf_hz if overrides.lpf_hz is not None else base_lpf_hz)
        self._use_preset_lpf = QCheckBox("Use preset LPF cutoff")
        self._use_preset_lpf.setChecked(overrides.lpf_hz is None)
        self._use_preset_lpf.toggled.connect(lambda checked: self._lpf.setEnabled(not checked))
        self._lpf.setEnabled(not self._use_preset_lpf.isChecked())
        form.addRow("LPF Cutoff (Hz)", self._lpf)
        form.addRow("", self._use_preset_lpf)

        self._sat = QDoubleSpinBox()
        self._sat.setRange(0.3, 1.5)
        self._sat.setSingleStep(0.05)
        self._sat.setValue(overrides.saturation_scale)
        form.addRow("Saturation Scale", self._sat)

        self._comp = QDoubleSpinBox()
        self._comp.setRange(0.3, 1.5)
        self._comp.setSingleStep(0.05)
        self._comp.setValue(overrides.compression_scale)
        form.addRow("Compression Scale", self._comp)

        hint = QLabel(
            "LPF uses this preset's base value when override is disabled. "
            "Saturation/Compression are relative scales (1.0 = preset default)."
        )
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def value(self) -> PresetOverrides:
        lpf_override: Optional[float] = None if self._use_preset_lpf.isChecked() else self._lpf.value()
        return PresetOverrides(
            lpf_hz=lpf_override,
            saturation_scale=self._sat.value(),
            compression_scale=self._comp.value(),
        )
