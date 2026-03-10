from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabBar,
    QDoubleSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    MULTIMEDIA_AVAILABLE = True
except Exception:
    QAudioOutput = None  # type: ignore[assignment]
    QMediaPlayer = None  # type: ignore[assignment]
    MULTIMEDIA_AVAILABLE = False

from nightfall_mix.analysis import TrackAnalysis, read_analysis_cache_summary
from nightfall_mix.config import OutputFormat, PresetName, QualityMode, RainPresence, SmartOrderingMode
from nightfall_mix.effects_presets import get_preset
from nightfall_mix.mixer import discover_audio_files
from nightfall_mix.utils import ffprobe_duration_ms, format_hms
from nightfall_desktop.models.session_models import (
    EngineSessionModel,
    GuiSettings,
    PresetOverrides,
    WorkspaceMode,
)
from nightfall_desktop.services.engine_service import GuiEngineService
from nightfall_desktop.services.media_tools_service import MediaToolsService
from nightfall_desktop.services.project_service import load_project_file, save_project_file
from nightfall_desktop.services.workers import (
    AnalysisWorker,
    RenderWorker,
    Mp3SplitWorker,
    Mp4StitchWorker,
)
from nightfall_desktop.ui.metadata_dialog import RenderMetadataDialog
from nightfall_desktop.ui.preset_editor_dialog import PresetEditorDialog
from nightfall_desktop.ui.timeline_widget import TimelineWidget


class ReorderableTrackTree(QTreeWidget):
    order_changed = Signal()

    def _collect_flat(self, item: QTreeWidgetItem, out: list[QTreeWidgetItem]) -> None:
        out.append(item)
        while item.childCount() > 0:
            child = item.takeChild(0)
            self._collect_flat(child, out)

    def _normalize_top_level_items(self) -> None:
        flat: list[QTreeWidgetItem] = []
        while self.topLevelItemCount() > 0:
            item = self.takeTopLevelItem(0)
            self._collect_flat(item, flat)
        for item in flat:
            self.addTopLevelItem(item)

    def dropEvent(self, event) -> None:  # noqa: N802
        super().dropEvent(event)
        # InternalMove may nest dropped rows under another row (OnItem target).
        # Flatten everything back to top-level so rows never "disappear".
        self._normalize_top_level_items()
        self.order_changed.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Nightfall Studio")
        self.resize(1560, 880)
        self._service = GuiEngineService()
        self._media_tools_service = MediaToolsService()
        self._session: Optional[EngineSessionModel] = None
        self._analysis_worker: Optional[AnalysisWorker] = None
        self._render_worker: Optional[RenderWorker] = None
        self._mp3_split_worker: Optional[Mp3SplitWorker] = None
        self._mp4_stitch_worker: Optional[Mp4StitchWorker] = None
        self._render_mode: str = "final"
        self._busy = False
        self._preview_dirty = True
        self._syncing_simple_controls = False
        self._simple_mode_initialized = False
        self._preview_audio_path: Optional[Path] = None
        self._render_metadata_tags: dict[str, str] = {}
        self._audio_output: Optional[QAudioOutput] = None
        self._player: Optional[QMediaPlayer] = None
        if MULTIMEDIA_AVAILABLE:
            self._audio_output = QAudioOutput(self)
            self._audio_output.setVolume(0.80)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.playbackStateChanged.connect(self._on_playback_state_changed)
            self._player.positionChanged.connect(self._on_playback_position_changed)
            self._player.durationChanged.connect(self._on_playback_duration_changed)
            self._player.errorOccurred.connect(self._on_playback_error)
        self._preset_overrides_by_preset: dict[PresetName, PresetOverrides] = {
            preset: PresetOverrides() for preset in PresetName
        }
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        shell = QVBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.workspace_tabs = QTabWidget()
        shell.addWidget(self.workspace_tabs)

        self.lofi_tab = QWidget()
        self.workspace_tabs.addTab(self.lofi_tab, "Lo-Fi Studio")

        root = QVBoxLayout(self.lofi_tab)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        mode_panel = QFrame()
        mode_panel.setObjectName("panel")
        root.addWidget(mode_panel)
        mode_layout = QVBoxLayout(mode_panel)
        mode_layout.setContentsMargins(10, 10, 10, 10)
        mode_layout.setSpacing(8)

        mode_header = QHBoxLayout()
        mode_header.addWidget(QLabel("Workspace Mode"))
        self.mode_tabs = QTabBar()
        self.mode_tabs.currentChanged.connect(self._on_mode_tab_changed)
        self.mode_tabs.setExpanding(False)
        self.mode_tabs.addTab("Simple")
        self.mode_tabs.addTab("Advanced")
        mode_header.addWidget(self.mode_tabs)
        mode_header.addStretch(1)
        self.reset_recommended_btn = QPushButton("Reset To Recommended")
        self.reset_recommended_btn.clicked.connect(self._reset_to_recommended)
        mode_header.addWidget(self.reset_recommended_btn)
        mode_layout.addLayout(mode_header)

        self.simple_mode_frame = QFrame()
        self.simple_mode_frame.setObjectName("panel")
        simple_layout = QGridLayout(self.simple_mode_frame)
        simple_layout.setContentsMargins(8, 8, 8, 8)
        simple_layout.setHorizontalSpacing(10)
        simple_layout.setVerticalSpacing(8)

        self.simple_preset_combo = QComboBox()
        self.simple_preset_combo.addItems([p.value for p in PresetName])
        self.simple_preset_combo.currentTextChanged.connect(self._apply_simple_controls_to_advanced)
        self.simple_output_format_combo = QComboBox()
        self.simple_output_format_combo.addItems([OutputFormat.mp3.value, OutputFormat.wav.value])
        self.simple_output_format_combo.currentTextChanged.connect(self._apply_simple_controls_to_advanced)
        self.simple_target_checkbox = QCheckBox("Set Mix Length")
        self.simple_target_spin = QSpinBox()
        self.simple_target_spin.setRange(10, 600)
        self.simple_target_spin.setValue(60)
        self.simple_target_spin.setSuffix(" min")
        self.simple_target_spin.setToolTip("Target final mix length in minutes when Set Mix Length is enabled.")
        self.simple_target_spin.setEnabled(False)
        self.simple_target_checkbox.toggled.connect(self.simple_target_spin.setEnabled)
        self.simple_target_checkbox.toggled.connect(self._apply_simple_controls_to_advanced)
        self.simple_target_spin.valueChanged.connect(self._apply_simple_controls_to_advanced)
        self.simple_preview_checkbox = QCheckBox("Use Preview While Tuning")
        self.simple_preview_spin = QSpinBox()
        self.simple_preview_spin.setRange(20, 180)
        self.simple_preview_spin.setValue(60)
        self.simple_preview_spin.setSuffix(" sec")
        self.simple_preview_spin.setToolTip("Preview excerpt length in seconds when preview rendering is enabled.")
        self.simple_preview_spin.setEnabled(False)
        self.simple_preview_checkbox.toggled.connect(self.simple_preview_spin.setEnabled)
        self.simple_preview_checkbox.toggled.connect(self._apply_simple_controls_to_advanced)
        self.simple_preview_spin.valueChanged.connect(self._apply_simple_controls_to_advanced)
        self.simple_hint = QLabel(
            "Simple mode uses recommended defaults for advanced DSP and transition settings."
        )
        self.simple_hint.setWordWrap(True)
        self.simple_hint.setStyleSheet("color: #6A7583;")

        simple_layout.addWidget(QLabel("Preset"), 0, 0)
        simple_layout.addWidget(self.simple_preset_combo, 0, 1)
        simple_layout.addWidget(QLabel("Output Format"), 0, 2)
        simple_layout.addWidget(self.simple_output_format_combo, 0, 3)
        simple_layout.addWidget(self.simple_target_checkbox, 1, 0)
        simple_layout.addWidget(self.simple_target_spin, 1, 1)
        simple_layout.addWidget(self.simple_preview_checkbox, 1, 2)
        simple_layout.addWidget(self.simple_preview_spin, 1, 3)
        simple_layout.addWidget(self.simple_hint, 2, 0, 1, 4)
        mode_layout.addWidget(self.simple_mode_frame)

        self.mode_mode_hint = QLabel("Simple mode: only core controls are shown. Advanced uses full control cards below.")
        self.mode_mode_hint.setStyleSheet("color: #6A7583;")
        self.mode_mode_hint.setWordWrap(True)
        mode_layout.addWidget(self.mode_mode_hint)

        self.top_controls_frame = QFrame()
        self.top_controls_frame.setObjectName("panel")
        root.addWidget(self.top_controls_frame)
        top = QGridLayout(self.top_controls_frame)
        top.setContentsMargins(10, 10, 10, 10)
        top.setHorizontalSpacing(12)
        top.setVerticalSpacing(8)

        self.preset_combo = QComboBox()
        self.preset_combo.addItems([p.value for p in PresetName])
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.preset_editor_btn = QPushButton("Preset Editor")
        self.preset_editor_btn.clicked.connect(self._open_preset_editor)
        self.reset_preset_btn = QPushButton("Reset Preset")
        self.reset_preset_btn.clicked.connect(self._reset_current_preset_overrides)
        self.reset_all_presets_btn = QPushButton("Reset All Presets")
        self.reset_all_presets_btn.clicked.connect(self._reset_all_preset_overrides)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems([q.value for q in QualityMode])
        self.quality_combo.setToolTip(
            "fast: quickest encode, balanced: default quality/speed, best: highest quality and slowest."
        )
        self.adaptive_checkbox = QCheckBox("Adaptive Lo-Fi")
        self.adaptive_checkbox.setToolTip("Per-track adaptive processing derived from analysis metrics.")
        self.smart_crossfade_checkbox = QCheckBox("Smart Crossfade")
        self.smart_crossfade_checkbox.setChecked(True)
        self.smart_crossfade_checkbox.setToolTip(
            "Automatically tunes crossfade length per transition using RMS and BPM cues."
        )
        self.smart_ordering_checkbox = QCheckBox("Smart Ordering")
        self.smart_ordering_checkbox.setToolTip("Reorders tracks by BPM/key proximity before planning.")
        self.smart_ordering_checkbox.setEnabled(True)
        self.smart_ordering_checkbox.toggled.connect(self._refresh_smart_ordering_mode_state)
        self.smart_ordering_mode_combo = QComboBox()
        self.smart_ordering_mode_combo.addItem("BPM First", SmartOrderingMode.bpm_first.value)
        self.smart_ordering_mode_combo.addItem("BPM + Key Balanced", SmartOrderingMode.bpm_key_balanced.value)
        self.smart_ordering_mode_combo.setToolTip("Controls how strongly key proximity affects smart ordering.")
        self.smart_crossfade_checkbox.toggled.connect(self._on_smart_crossfade_toggled)
        self.shuffle_checkbox = QCheckBox("Shuffle")
        self.shuffle_checkbox.setToolTip("Randomizes track order before analysis/planning when Smart Ordering is off.")
        self._on_smart_crossfade_toggled(self.smart_crossfade_checkbox.isChecked())

        self.rain_line = QLineEdit()
        self.rain_browse_btn = QPushButton("Rain...")
        self.rain_browse_btn.clicked.connect(self._choose_rain)
        self.rain_slider = QSlider(Qt.Horizontal)
        self.rain_slider.setRange(-50, -10)
        self.rain_slider.setValue(-28)
        self.rain_label = QLabel("-28 dB")
        self.rain_slider.valueChanged.connect(lambda v: self.rain_label.setText(f"{v} dB"))
        self.rain_presence_combo = QComboBox()
        self.rain_presence_combo.addItem("Behind", RainPresence.behind.value)
        self.rain_presence_combo.addItem("Balanced", RainPresence.balanced.value)
        self.rain_presence_combo.addItem("Upfront", RainPresence.upfront.value)
        self.rain_presence_combo.setCurrentIndex(1)
        self.rain_presence_combo.setToolTip("Controls how tucked-in or forward the rain layer sounds.")
        self.rain_low_drops_checkbox = QCheckBox("Preserve Low Drops")
        self.rain_low_drops_checkbox.setChecked(True)
        self.rain_low_drops_checkbox.setToolTip(
            "Keeps more low-frequency body from window hits, heavier drops, and darker ambience."
        )

        self.crossfade_spin = QDoubleSpinBox()
        self.crossfade_spin.setRange(0.5, 20.0)
        self.crossfade_spin.setSingleStep(0.5)
        self.crossfade_spin.setValue(6.0)
        self.crossfade_spin.setSuffix(" sec")
        self.crossfade_spin.setToolTip("Base crossfade duration in seconds used by fixed and smart transitions.")
        self.lufs_spin = QDoubleSpinBox()
        self.lufs_spin.setRange(-30.0, -5.0)
        self.lufs_spin.setSingleStep(0.5)
        self.lufs_spin.setValue(-14.0)
        self.lufs_spin.setSuffix(" LUFS")
        self.lufs_spin.setToolTip("Target output loudness. More negative values are quieter.")

        self.target_checkbox = QCheckBox("Loop To Target")
        self.target_checkbox.setToolTip("Repeats track cycles until the mix reaches the target minutes value.")
        self.target_spin = QSpinBox()
        self.target_spin.setRange(10, 600)
        self.target_spin.setValue(60)
        self.target_spin.setSuffix(" min")
        self.target_spin.setToolTip("Target mix length in minutes. Enabled only when Loop To Target is checked.")
        self.target_spin.setEnabled(False)
        self.target_checkbox.toggled.connect(self.target_spin.setEnabled)
        self.target_checkbox.toggled.connect(self._sync_simple_from_advanced)
        self.target_spin.valueChanged.connect(self._sync_simple_from_advanced)

        self.preview_checkbox = QCheckBox("Preview Mode")
        self.preview_checkbox.setToolTip(
            "When enabled, final Render exports only a short excerpt instead of the full timeline."
        )
        self.preview_spin = QSpinBox()
        self.preview_spin.setRange(20, 180)
        self.preview_spin.setValue(60)
        self.preview_spin.setSuffix(" sec")
        self.preview_spin.setToolTip("Excerpt length in seconds used by Preview Mode renders.")
        self.preview_spin.setEnabled(False)
        self.preview_checkbox.toggled.connect(self.preview_spin.setEnabled)
        self.preview_checkbox.toggled.connect(self._refresh_estimates)
        self.preview_checkbox.toggled.connect(self._sync_simple_from_advanced)
        self.preview_spin.valueChanged.connect(self._refresh_estimates)
        self.preview_spin.valueChanged.connect(self._sync_simple_from_advanced)
        self.preview_checkbox.toggled.connect(self._mark_preview_dirty)
        self.preview_spin.valueChanged.connect(self._mark_preview_dirty)
        self.crossfade_spin.valueChanged.connect(self._on_plan_controls_changed)
        self.target_checkbox.toggled.connect(self._on_plan_controls_changed)
        self.target_spin.valueChanged.connect(self._on_plan_controls_changed)
        self.lufs_spin.valueChanged.connect(self._mark_preview_dirty)
        self.rain_line.textChanged.connect(self._mark_preview_dirty)
        self.rain_slider.valueChanged.connect(self._mark_preview_dirty)
        self.rain_presence_combo.currentIndexChanged.connect(self._mark_preview_dirty)
        self.rain_low_drops_checkbox.toggled.connect(self._mark_preview_dirty)
        self.shuffle_checkbox.toggled.connect(self._mark_preview_dirty)
        self.adaptive_checkbox.toggled.connect(self._mark_preview_dirty)
        self.quality_combo.currentIndexChanged.connect(self._mark_preview_dirty)
        self.smart_ordering_checkbox.toggled.connect(self._mark_preview_dirty)
        self.smart_ordering_mode_combo.currentIndexChanged.connect(self._mark_preview_dirty)

        self.adaptive_report_line = QLineEdit(str(Path("adaptive_report.json")))
        self.adaptive_report_btn = QPushButton("Adaptive Report...")
        self.adaptive_report_btn.clicked.connect(self._choose_adaptive_report)
        self.cache_folder_line = QLineEdit("")
        self.cache_folder_line.setPlaceholderText("System temp folder (default)")
        self.cache_folder_line.setToolTip(
            "Temporary render cache folder for intermediate files. "
            "Use a drive with enough free space for long mixes."
        )
        self.cache_folder_line.textChanged.connect(self._refresh_action_state)
        self.cache_folder_btn = QPushButton("Cache...")
        self.cache_folder_btn.clicked.connect(self._choose_cache_folder)

        profile_box = QGroupBox("Sound Profile")
        profile_layout = QGridLayout(profile_box)
        profile_layout.setHorizontalSpacing(8)
        profile_layout.setVerticalSpacing(6)
        profile_layout.addWidget(QLabel("Preset"), 0, 0)
        profile_layout.addWidget(self.preset_combo, 0, 1)
        profile_layout.addWidget(self.preset_editor_btn, 0, 2)
        profile_layout.addWidget(self.reset_preset_btn, 0, 3)
        profile_layout.addWidget(self.reset_all_presets_btn, 0, 4)
        profile_layout.addWidget(QLabel("Quality"), 1, 0)
        profile_layout.addWidget(self.quality_combo, 1, 1)
        profile_layout.addWidget(self.adaptive_checkbox, 1, 2, 1, 3)

        transition_box = QGroupBox("Transitions And Ordering")
        transition_layout = QGridLayout(transition_box)
        transition_layout.setHorizontalSpacing(8)
        transition_layout.setVerticalSpacing(6)
        transition_layout.addWidget(self.smart_crossfade_checkbox, 0, 0)
        transition_layout.addWidget(self.smart_ordering_checkbox, 0, 1)
        transition_layout.addWidget(self.smart_ordering_mode_combo, 0, 2)
        transition_layout.addWidget(self.shuffle_checkbox, 0, 3)
        transition_layout.addWidget(QLabel("Crossfade"), 1, 0)
        transition_layout.addWidget(self.crossfade_spin, 1, 1)

        length_box = QGroupBox("Length And Scope")
        length_layout = QGridLayout(length_box)
        length_layout.setHorizontalSpacing(8)
        length_layout.setVerticalSpacing(6)
        length_layout.addWidget(self.target_checkbox, 0, 0)
        length_layout.addWidget(self.target_spin, 0, 1)
        length_layout.addWidget(self.preview_checkbox, 1, 0)
        length_layout.addWidget(self.preview_spin, 1, 1)
        length_hint = QLabel(
            "Loop To Target controls full mix duration. Preview Mode only changes final Render scope."
        )
        length_hint.setWordWrap(True)
        length_hint.setStyleSheet("color: #6A7583;")
        length_layout.addWidget(length_hint, 2, 0, 1, 2)

        rain_box = QGroupBox("Rain And Mastering")
        rain_layout = QGridLayout(rain_box)
        rain_layout.setHorizontalSpacing(8)
        rain_layout.setVerticalSpacing(6)
        rain_layout.addWidget(QLabel("Rain File"), 0, 0)
        rain_layout.addWidget(self.rain_line, 0, 1, 1, 3)
        rain_layout.addWidget(self.rain_browse_btn, 0, 4)
        rain_layout.addWidget(QLabel("Rain Volume"), 1, 0)
        rain_layout.addWidget(self.rain_slider, 1, 1, 1, 3)
        rain_layout.addWidget(self.rain_label, 1, 4)
        rain_layout.addWidget(QLabel("Rain Presence"), 2, 0)
        rain_layout.addWidget(self.rain_presence_combo, 2, 1)
        rain_layout.addWidget(self.rain_low_drops_checkbox, 2, 2, 1, 3)
        rain_layout.addWidget(QLabel("Target LUFS"), 3, 0)
        rain_layout.addWidget(self.lufs_spin, 3, 1)

        reports_box = QGroupBox("Reports")
        reports_layout = QGridLayout(reports_box)
        reports_layout.setHorizontalSpacing(8)
        reports_layout.setVerticalSpacing(6)
        reports_layout.addWidget(QLabel("Adaptive Report JSON"), 0, 0)
        reports_layout.addWidget(self.adaptive_report_line, 0, 1)
        reports_layout.addWidget(self.adaptive_report_btn, 0, 2)
        reports_layout.addWidget(QLabel("Render Cache Folder"), 1, 0)
        reports_layout.addWidget(self.cache_folder_line, 1, 1)
        reports_layout.addWidget(self.cache_folder_btn, 1, 2)

        top.addWidget(profile_box, 0, 0)
        top.addWidget(transition_box, 0, 1)
        top.addWidget(length_box, 1, 0)
        top.addWidget(rain_box, 1, 1)
        top.setColumnStretch(0, 1)
        top.setColumnStretch(1, 1)

        body_row = QHBoxLayout()
        body_row.setSpacing(10)
        root.addLayout(body_row, 1)

        left_panel = QFrame()
        left_panel.setObjectName("panel")
        left_panel.setMinimumWidth(400)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)

        folder_row = QHBoxLayout()
        self.folder_line = QLineEdit()
        self.folder_line.textChanged.connect(self._refresh_action_state)
        self.folder_line.editingFinished.connect(self._preload_tracks_from_folder)
        self.folder_browse_btn = QPushButton("Folder...")
        self.folder_browse_btn.clicked.connect(self._choose_folder)
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.clicked.connect(self._start_analysis)
        self.remove_track_btn = QPushButton("Remove")
        self.remove_track_btn.clicked.connect(self._remove_selected_lofi_tracks)
        folder_row.addWidget(self.folder_line, 1)
        folder_row.addWidget(self.folder_browse_btn)
        folder_row.addWidget(self.analyze_btn)
        folder_row.addWidget(self.remove_track_btn)
        left_layout.addLayout(folder_row)
        self.validation_label = QLabel("")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #D65A5A;")
        left_layout.addWidget(self.validation_label)

        self.track_tree = ReorderableTrackTree()
        self.track_tree.setColumnCount(7)
        self.track_tree.setHeaderLabels(["#", "Track", "Duration", "BPM", "Key", "Cache", "Lo-Fi"])
        self.track_tree.setRootIsDecorated(False)
        self.track_tree.setSelectionBehavior(QTreeWidget.SelectRows)
        self.track_tree.setDragDropMode(QTreeWidget.InternalMove)
        self.track_tree.setDefaultDropAction(Qt.MoveAction)
        self.track_tree.itemSelectionChanged.connect(self._on_track_selected)
        self.track_tree.order_changed.connect(self._on_track_order_changed)
        left_layout.addWidget(self.track_tree, 1)
        body_row.addWidget(left_panel, 0)

        center_panel = QFrame()
        center_panel.setObjectName("panel")
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(10, 10, 10, 10)
        center_layout.setSpacing(8)
        center_layout.addWidget(reports_box)
        center_layout.addWidget(QLabel("Timeline"))
        self.timeline = TimelineWidget()
        center_layout.addWidget(self.timeline, 1)
        body_row.addWidget(center_panel, 1)

        right_panel = QFrame()
        right_panel.setObjectName("panel")
        right_panel.setMinimumWidth(360)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(8)

        metrics_box = QGroupBox("Adaptive Analysis")
        metrics_form = QFormLayout(metrics_box)
        self.metric_labels: dict[str, QLabel] = {}
        for key in (
            "lufs",
            "crest_factor_db",
            "spectral_centroid_hz",
            "rolloff_hz",
            "stereo_width",
            "noise_floor_dbfs",
        ):
            lbl = QLabel("--")
            self.metric_labels[key] = lbl
            metrics_form.addRow(key.replace("_", " ").title(), lbl)

        process_box = QGroupBox("Applied Processing")
        process_form = QFormLayout(process_box)
        self.proc_labels: dict[str, QLabel] = {}
        for key in (
            "lpf_cutoff_hz",
            "saturation_strength",
            "compression_strength",
            "stereo_width_target",
            "noise_added_db",
        ):
            lbl = QLabel("--")
            self.proc_labels[key] = lbl
            process_form.addRow(key.replace("_", " ").title(), lbl)

        self.rationale_console = QPlainTextEdit()
        self.rationale_console.setReadOnly(True)
        self.rationale_console.setPlaceholderText("Track rationale appears here.")
        self.rationale_console.setFixedHeight(140)

        right_layout.addWidget(metrics_box)
        right_layout.addWidget(process_box)
        right_layout.addWidget(QLabel("Explanation"))
        right_layout.addWidget(self.rationale_console)
        body_row.addWidget(right_panel, 0)

        bottom_panel = QFrame()
        bottom_panel.setObjectName("panel")
        bottom_layout = QVBoxLayout(bottom_panel)
        bottom_layout.setContentsMargins(10, 10, 10, 10)
        bottom_layout.setSpacing(8)

        output_row = QHBoxLayout()
        self.output_line = QLineEdit()
        self.output_line.textChanged.connect(self._refresh_action_state)
        self.output_browse_btn = QPushButton("Output...")
        self.output_browse_btn.clicked.connect(self._choose_output)
        self.format_combo = QComboBox()
        self.format_combo.addItems([OutputFormat.mp3.value, OutputFormat.wav.value])
        self.format_combo.currentTextChanged.connect(self._refresh_estimates)
        self.format_combo.currentTextChanged.connect(self._sync_simple_from_advanced)
        self.format_combo.currentTextChanged.connect(self._refresh_bitrate_state)
        self.format_combo.currentTextChanged.connect(self._refresh_chunk_output_state)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["128k", "160k", "192k", "256k", "320k"])
        self.bitrate_combo.setCurrentText("192k")
        self.bitrate_combo.setToolTip("MP3 encoding bitrate. Higher bitrate means larger file and better quality.")
        self.bitrate_combo.currentTextChanged.connect(self._refresh_estimates)
        self.bitrate_combo.currentTextChanged.connect(self._mark_preview_dirty)
        self.chunk_output_checkbox = QCheckBox("Split MP3")
        self.chunk_output_checkbox.setToolTip(
            "Export additional MP3 chunk files after render. "
            "Example: 35 min with 10 min chunks -> 10/10/10/5."
        )
        self.chunk_output_checkbox.toggled.connect(self._refresh_chunk_output_state)
        self.chunk_output_checkbox.toggled.connect(self._mark_preview_dirty)
        self.chunk_minutes_spin = QSpinBox()
        self.chunk_minutes_spin.setRange(1, 240)
        self.chunk_minutes_spin.setValue(10)
        self.chunk_minutes_spin.setSuffix(" min")
        self.chunk_minutes_spin.setToolTip("Chunk length in minutes for split MP3 export.")
        self.chunk_minutes_spin.valueChanged.connect(self._mark_preview_dirty)
        output_row.addWidget(QLabel("Output"))
        output_row.addWidget(self.output_line, 1)
        output_row.addWidget(self.format_combo)
        output_row.addWidget(QLabel("Bitrate"))
        output_row.addWidget(self.bitrate_combo)
        output_row.addWidget(self.chunk_output_checkbox)
        output_row.addWidget(self.chunk_minutes_spin)
        output_row.addWidget(self.output_browse_btn)
        bottom_layout.addLayout(output_row)

        estimate_row = QHBoxLayout()
        self.timeline_duration_label = QLabel("Timeline: --")
        self.render_duration_label = QLabel("Render Scope: --")
        self.estimated_size_label = QLabel("Estimated Size: --")
        estimate_row.addWidget(self.timeline_duration_label)
        estimate_row.addWidget(self.render_duration_label)
        estimate_row.addWidget(self.estimated_size_label)
        estimate_row.addStretch(1)
        bottom_layout.addLayout(estimate_row)

        playback_row = QHBoxLayout()
        self.preview_pos_label = QLabel("00:00:00")
        self.preview_seek_slider = QSlider(Qt.Horizontal)
        self.preview_seek_slider.setEnabled(False)
        self.preview_seek_slider.setRange(0, 0)
        self.preview_seek_slider.sliderMoved.connect(self._on_seek_slider_moved)
        self.preview_total_label = QLabel("00:00:00")
        playback_row.addWidget(self.preview_pos_label)
        playback_row.addWidget(self.preview_seek_slider, 1)
        playback_row.addWidget(self.preview_total_label)
        bottom_layout.addLayout(playback_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        bottom_layout.addWidget(self.progress_bar)

        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMaximumBlockCount(2000)
        self.log_console.setFixedHeight(160)
        bottom_layout.addWidget(self.log_console)

        action_row = QHBoxLayout()
        self.render_btn = QPushButton("Render")
        self.render_btn.clicked.connect(self._start_render)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_active_worker)
        self.cancel_btn.setEnabled(False)
        self.save_project_btn = QPushButton("Save Project")
        self.save_project_btn.clicked.connect(self._save_project)
        self.load_project_btn = QPushButton("Load Project")
        self.load_project_btn.clicked.connect(self._load_project)
        self.preview_short_btn = QPushButton("Short Preview")
        self.preview_short_btn.clicked.connect(lambda: self._start_preview_render(short_preview=True))
        self.preview_full_btn = QPushButton("Full Preview")
        self.preview_full_btn.clicked.connect(lambda: self._start_preview_render(short_preview=False))
        self.preview_play_btn = QPushButton("Play")
        self.preview_play_btn.clicked.connect(self._toggle_preview_playback)
        self.preview_stop_btn = QPushButton("Stop")
        self.preview_stop_btn.clicked.connect(self._stop_preview_playback)
        self.preview_status_label = QLabel("Preview: not built")
        self.preview_status_label.setStyleSheet("color: #6A7583;")
        action_row.addWidget(self.save_project_btn)
        action_row.addWidget(self.load_project_btn)
        action_row.addWidget(self.preview_short_btn)
        action_row.addWidget(self.preview_full_btn)
        action_row.addWidget(self.preview_play_btn)
        action_row.addWidget(self.preview_stop_btn)
        action_row.addWidget(self.preview_status_label, 1)
        action_row.addStretch(1)
        action_row.addWidget(self.render_btn)
        action_row.addWidget(self.cancel_btn)
        bottom_layout.addLayout(action_row)
        root.addWidget(bottom_panel)
        self._apply_recommended_defaults(mark_dirty=False)
        self.mode_tabs.setCurrentIndex(1)
        self._on_mode_tab_changed(1)
        self._sync_simple_from_advanced()
        self._refresh_action_state()
        self._refresh_estimates()
        self._refresh_bitrate_state()
        self._refresh_chunk_output_state()
        self._refresh_preview_controls()
        self._build_mp3_splitter_tab()
        self._build_mp4_stitcher_tab()

    def _build_mp3_splitter_tab(self) -> None:
        tab = QWidget()
        self.workspace_tabs.addTab(tab, "MP3 Splitter")
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        panel = QFrame()
        panel.setObjectName("panel")
        root.addWidget(panel)
        layout = QGridLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        self.split_input_line = QLineEdit()
        self.split_input_btn = QPushButton("MP3...")
        self.split_input_btn.clicked.connect(self._choose_splitter_input)
        self.split_output_line = QLineEdit()
        self.split_output_btn = QPushButton("Output Folder...")
        self.split_output_btn.clicked.connect(self._choose_splitter_output)
        self.split_chunk_spin = QSpinBox()
        self.split_chunk_spin.setRange(1, 240)
        self.split_chunk_spin.setValue(10)
        self.split_chunk_spin.setSuffix(" min")
        self.split_bitrate_combo = QComboBox()
        self.split_bitrate_combo.addItems(["128k", "160k", "192k", "256k", "320k"])
        self.split_bitrate_combo.setCurrentText("192k")

        self.split_start_btn = QPushButton("Split MP3")
        self.split_start_btn.clicked.connect(self._start_mp3_split)
        self.split_cancel_btn = QPushButton("Cancel")
        self.split_cancel_btn.clicked.connect(self._cancel_mp3_split)
        self.split_cancel_btn.setEnabled(False)

        layout.addWidget(QLabel("Input MP3"), 0, 0)
        layout.addWidget(self.split_input_line, 0, 1)
        layout.addWidget(self.split_input_btn, 0, 2)
        layout.addWidget(QLabel("Output Folder"), 1, 0)
        layout.addWidget(self.split_output_line, 1, 1)
        layout.addWidget(self.split_output_btn, 1, 2)
        layout.addWidget(QLabel("Chunk Length"), 2, 0)
        layout.addWidget(self.split_chunk_spin, 2, 1)
        layout.addWidget(QLabel("Bitrate"), 3, 0)
        layout.addWidget(self.split_bitrate_combo, 3, 1)
        layout.addWidget(self.split_start_btn, 4, 1)
        layout.addWidget(self.split_cancel_btn, 4, 2)

        self.split_progress = QProgressBar()
        self.split_progress.setRange(0, 1000)
        self.split_progress.setValue(0)
        root.addWidget(self.split_progress)

        self.split_log = QPlainTextEdit()
        self.split_log.setReadOnly(True)
        self.split_log.setMaximumBlockCount(2000)
        root.addWidget(self.split_log, 1)

    def _build_mp4_stitcher_tab(self) -> None:
        tab = QWidget()
        self.workspace_tabs.addTab(tab, "MP4 Stitcher")
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        panel = QFrame()
        panel.setObjectName("panel")
        root.addWidget(panel)
        layout = QGridLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        self.stitch_folder_line = QLineEdit()
        self.stitch_folder_btn = QPushButton("Folder...")
        self.stitch_folder_btn.clicked.connect(self._choose_stitch_folder)
        self.stitch_load_btn = QPushButton("Load Files")
        self.stitch_load_btn.clicked.connect(self._load_stitch_files_from_folder)
        self.stitch_remove_btn = QPushButton("Remove Selected")
        self.stitch_remove_btn.clicked.connect(self._remove_selected_stitch_files)
        self.stitch_output_line = QLineEdit()
        self.stitch_output_btn = QPushButton("Output...")
        self.stitch_output_btn.clicked.connect(self._choose_stitch_output)
        self.stitch_smart_ordering_checkbox = QCheckBox("Smart Ordering")
        self.stitch_smart_ordering_checkbox.setToolTip(
            "Reorder clips by audio BPM/key fit before stitching."
        )
        self.stitch_smart_fade_checkbox = QCheckBox("Smart Audio Fade")
        self.stitch_smart_fade_checkbox.setToolTip(
            "Apply audio crossfades between clips (video stays hard-cut stitched)."
        )
        self.stitch_crossfade_spin = QDoubleSpinBox()
        self.stitch_crossfade_spin.setRange(0.5, 8.0)
        self.stitch_crossfade_spin.setSingleStep(0.25)
        self.stitch_crossfade_spin.setValue(2.0)
        self.stitch_crossfade_spin.setSuffix(" sec")

        self.stitch_start_btn = QPushButton("Stitch MP4")
        self.stitch_start_btn.clicked.connect(self._start_mp4_stitch)
        self.stitch_cancel_btn = QPushButton("Cancel")
        self.stitch_cancel_btn.clicked.connect(self._cancel_mp4_stitch)
        self.stitch_cancel_btn.setEnabled(False)

        note = QLabel(
            "Default mode is straight glue. Enable Smart Audio Fade only if you want crossfades in sound."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6A7583;")

        layout.addWidget(QLabel("Input Folder"), 0, 0)
        layout.addWidget(self.stitch_folder_line, 0, 1)
        layout.addWidget(self.stitch_folder_btn, 0, 2)
        layout.addWidget(self.stitch_load_btn, 0, 3)
        layout.addWidget(QLabel("Output MP4"), 1, 0)
        layout.addWidget(self.stitch_output_line, 1, 1)
        layout.addWidget(self.stitch_output_btn, 1, 2)
        layout.addWidget(self.stitch_remove_btn, 1, 3)
        layout.addWidget(self.stitch_smart_ordering_checkbox, 2, 0)
        layout.addWidget(self.stitch_smart_fade_checkbox, 2, 1)
        layout.addWidget(self.stitch_crossfade_spin, 2, 2)
        layout.addWidget(note, 3, 0, 1, 4)
        layout.addWidget(self.stitch_start_btn, 4, 2)
        layout.addWidget(self.stitch_cancel_btn, 4, 3)

        self.stitch_tree = ReorderableTrackTree()
        self.stitch_tree.setColumnCount(3)
        self.stitch_tree.setHeaderLabels(["#", "Clip", "Duration"])
        self.stitch_tree.setRootIsDecorated(False)
        self.stitch_tree.setSelectionBehavior(QTreeWidget.SelectRows)
        self.stitch_tree.setDragDropMode(QTreeWidget.InternalMove)
        self.stitch_tree.setDefaultDropAction(Qt.MoveAction)
        self.stitch_tree.order_changed.connect(self._on_stitch_order_changed)
        root.addWidget(self.stitch_tree, 1)

        self.stitch_progress = QProgressBar()
        self.stitch_progress.setRange(0, 1000)
        self.stitch_progress.setValue(0)
        root.addWidget(self.stitch_progress)

        self.stitch_log = QPlainTextEdit()
        self.stitch_log.setReadOnly(True)
        self.stitch_log.setMaximumBlockCount(2000)
        root.addWidget(self.stitch_log, 1)

    def _append_split_log(self, message: str) -> None:
        self.split_log.appendPlainText(message)
        bar = self.split_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_stitch_log(self, message: str) -> None:
        self.stitch_log.appendPlainText(message)
        bar = self.stitch_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _renumber_tree(self, tree: QTreeWidget) -> None:
        for idx in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(idx)
            item.setText(0, str(idx + 1))

    def _current_stitch_paths(self) -> list[Path]:
        paths: list[Path] = []
        if not hasattr(self, "stitch_tree"):
            return paths
        for idx in range(self.stitch_tree.topLevelItemCount()):
            item = self.stitch_tree.topLevelItem(idx)
            path_text = item.data(0, Qt.UserRole)
            if path_text:
                paths.append(Path(path_text))
        return paths

    def _set_mp3_split_busy(self, busy: bool) -> None:
        self.split_input_line.setEnabled(not busy)
        self.split_input_btn.setEnabled(not busy)
        self.split_output_line.setEnabled(not busy)
        self.split_output_btn.setEnabled(not busy)
        self.split_chunk_spin.setEnabled(not busy)
        self.split_bitrate_combo.setEnabled(not busy)
        self.split_start_btn.setEnabled(not busy)
        self.split_cancel_btn.setEnabled(busy)

    def _set_mp4_stitch_busy(self, busy: bool) -> None:
        self.stitch_folder_line.setEnabled(not busy)
        self.stitch_folder_btn.setEnabled(not busy)
        self.stitch_load_btn.setEnabled(not busy)
        self.stitch_remove_btn.setEnabled(not busy)
        self.stitch_output_line.setEnabled(not busy)
        self.stitch_output_btn.setEnabled(not busy)
        self.stitch_smart_ordering_checkbox.setEnabled(not busy)
        self.stitch_smart_fade_checkbox.setEnabled(not busy)
        self.stitch_crossfade_spin.setEnabled(not busy)
        self.stitch_tree.setEnabled(not busy)
        self.stitch_start_btn.setEnabled(not busy)
        self.stitch_cancel_btn.setEnabled(busy)

    def _choose_splitter_input(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose MP3 File",
            self.split_input_line.text().strip() or ".",
            "MP3 (*.mp3)",
        )
        if file_path:
            self.split_input_line.setText(file_path)
            if not self.split_output_line.text().strip():
                self.split_output_line.setText(str(Path(file_path).parent))

    def _choose_splitter_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose Output Folder",
            self.split_output_line.text().strip() or ".",
        )
        if directory:
            self.split_output_line.setText(directory)

    def _start_mp3_split(self) -> None:
        if self._mp3_split_worker and self._mp3_split_worker.isRunning():
            return
        if self._busy or (self._mp4_stitch_worker and self._mp4_stitch_worker.isRunning()):
            QMessageBox.warning(self, "Busy", "Wait for current operation to finish first.")
            return
        input_path = Path(self.split_input_line.text().strip())
        output_dir = Path(self.split_output_line.text().strip() or ".")
        if not input_path.exists() or not input_path.is_file():
            QMessageBox.warning(self, "Invalid Input", "Select a valid input MP3 file.")
            return
        self.split_progress.setValue(0)
        self.split_log.clear()
        self._append_split_log("Starting MP3 split...")
        self._set_mp3_split_busy(True)
        self._mp3_split_worker = Mp3SplitWorker(
            service=self._media_tools_service,
            input_path=input_path,
            output_dir=output_dir,
            chunk_minutes=int(self.split_chunk_spin.value()),
            bitrate=self.split_bitrate_combo.currentText().strip() or "192k",
        )
        self._mp3_split_worker.log.connect(self._append_split_log)
        self._mp3_split_worker.progress.connect(self._on_mp3_split_progress)
        self._mp3_split_worker.finished_paths.connect(self._on_mp3_split_finished)
        self._mp3_split_worker.failed.connect(self._on_mp3_split_failed)
        self._mp3_split_worker.cancelled.connect(self._on_mp3_split_cancelled)
        self._mp3_split_worker.start()

    def _cancel_mp3_split(self) -> None:
        if self._mp3_split_worker and self._mp3_split_worker.isRunning():
            self._mp3_split_worker.cancel()
            self._append_split_log("Cancelling MP3 split...")

    def _on_mp3_split_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self.split_progress.setValue(0)
            return
        self.split_progress.setValue(int(current / total * 1000))

    def _on_mp3_split_finished(self, paths: object) -> None:
        self._set_mp3_split_busy(False)
        self.split_progress.setValue(1000)
        file_paths = [Path(p) for p in (paths or [])]
        self._append_split_log(f"Done. Created {len(file_paths)} chunk file(s).")
        QMessageBox.information(self, "MP3 Splitter", f"Created {len(file_paths)} chunk file(s).")

    def _on_mp3_split_failed(self, message: str) -> None:
        self._set_mp3_split_busy(False)
        self._append_split_log(f"Error: {message}")
        QMessageBox.critical(self, "MP3 Splitter Failed", message)

    def _on_mp3_split_cancelled(self) -> None:
        self._set_mp3_split_busy(False)
        self._append_split_log("MP3 split cancelled.")

    def _choose_stitch_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose Video Folder",
            self.stitch_folder_line.text().strip() or ".",
        )
        if directory:
            self.stitch_folder_line.setText(directory)
            if not self.stitch_output_line.text().strip():
                self.stitch_output_line.setText(str(Path(directory) / "stitched.mp4"))
            self._load_stitch_files_from_folder()

    def _load_stitch_files_from_folder(self) -> None:
        folder_text = self.stitch_folder_line.text().strip()
        folder = Path(folder_text) if folder_text else None
        if folder is None or not folder.exists() or not folder.is_dir():
            return
        try:
            files = self._media_tools_service.discover_mp4_inputs(folder)
        except Exception as exc:
            self._append_stitch_log(f"Load warning: {exc}")
            return
        self.stitch_tree.clear()
        for idx, path in enumerate(files, start=1):
            duration_text = "--"
            try:
                duration_text = format_hms(ffprobe_duration_ms(path, logger=self._service.logger))
            except Exception:
                pass
            item = QTreeWidgetItem([str(idx), path.name, duration_text])
            item.setData(0, Qt.UserRole, str(path))
            self.stitch_tree.addTopLevelItem(item)
        self.stitch_tree.resizeColumnToContents(0)
        self.stitch_tree.resizeColumnToContents(2)
        self._append_stitch_log(f"Loaded {len(files)} clip(s).")

    def _remove_selected_stitch_files(self) -> None:
        items = self.stitch_tree.selectedItems()
        if not items:
            return
        for item in items:
            idx = self.stitch_tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self.stitch_tree.takeTopLevelItem(idx)
        self._renumber_tree(self.stitch_tree)
        self._append_stitch_log(f"Removed {len(items)} clip(s) from stitch list.")

    def _on_stitch_order_changed(self) -> None:
        self._renumber_tree(self.stitch_tree)

    def _choose_stitch_output(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Choose Output MP4",
            self.stitch_output_line.text().strip() or "stitched.mp4",
            "MP4 (*.mp4)",
        )
        if file_path:
            self.stitch_output_line.setText(file_path)

    def _start_mp4_stitch(self) -> None:
        if self._mp4_stitch_worker and self._mp4_stitch_worker.isRunning():
            return
        if self._busy or (self._mp3_split_worker and self._mp3_split_worker.isRunning()):
            QMessageBox.warning(self, "Busy", "Wait for current operation to finish first.")
            return
        folder = Path(self.stitch_folder_line.text().strip())
        output_path = Path(self.stitch_output_line.text().strip())
        if not folder.exists() or not folder.is_dir():
            QMessageBox.warning(self, "Invalid Input", "Select a valid input folder.")
            return
        stitch_paths = self._current_stitch_paths()
        if not stitch_paths:
            self._load_stitch_files_from_folder()
            stitch_paths = self._current_stitch_paths()
        if not stitch_paths:
            QMessageBox.warning(self, "No Clips", "Load clips and keep at least one clip in the list.")
            return
        if not output_path.name:
            QMessageBox.warning(self, "Invalid Output", "Select a valid output MP4 path.")
            return
        self.stitch_progress.setValue(0)
        self.stitch_log.clear()
        self._append_stitch_log("Starting MP4 stitch...")
        self._set_mp4_stitch_busy(True)
        self._mp4_stitch_worker = Mp4StitchWorker(
            service=self._media_tools_service,
            folder=folder,
            output_path=output_path,
            smart_ordering=self.stitch_smart_ordering_checkbox.isChecked(),
            smart_fade=self.stitch_smart_fade_checkbox.isChecked(),
            crossfade_sec=float(self.stitch_crossfade_spin.value()),
            input_files=stitch_paths,
        )
        self._mp4_stitch_worker.log.connect(self._append_stitch_log)
        self._mp4_stitch_worker.progress.connect(self._on_mp4_stitch_progress)
        self._mp4_stitch_worker.finished_path.connect(self._on_mp4_stitch_finished)
        self._mp4_stitch_worker.failed.connect(self._on_mp4_stitch_failed)
        self._mp4_stitch_worker.cancelled.connect(self._on_mp4_stitch_cancelled)
        self._mp4_stitch_worker.start()

    def _cancel_mp4_stitch(self) -> None:
        if self._mp4_stitch_worker and self._mp4_stitch_worker.isRunning():
            self._mp4_stitch_worker.cancel()
            self._append_stitch_log("Cancelling MP4 stitch...")

    def _on_mp4_stitch_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self.stitch_progress.setValue(0)
            return
        self.stitch_progress.setValue(int(current / total * 1000))

    def _on_mp4_stitch_finished(self, path: object) -> None:
        self._set_mp4_stitch_busy(False)
        self.stitch_progress.setValue(1000)
        out_path = Path(str(path))
        self._append_stitch_log(f"Done: {out_path}")
        QMessageBox.information(self, "MP4 Stitcher", f"Output written:\n{out_path}")

    def _on_mp4_stitch_failed(self, message: str) -> None:
        self._set_mp4_stitch_busy(False)
        self._append_stitch_log(f"Error: {message}")
        QMessageBox.critical(self, "MP4 Stitcher Failed", message)

    def _on_mp4_stitch_cancelled(self) -> None:
        self._set_mp4_stitch_busy(False)
        self._append_stitch_log("MP4 stitch cancelled.")

    def _append_log(self, message: str) -> None:
        self.log_console.appendPlainText(message)
        bar = self.log_console.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _status_brush(self, score: Optional[float]) -> QBrush:
        if score is None:
            return QBrush(QColor("#6A7583"))
        if score < 35:
            return QBrush(QColor("#4CAF50"))
        if score < 70:
            return QBrush(QColor("#E39E45"))
        return QBrush(QColor("#D65A5A"))

    def _adaptive_status_text(self, score: Optional[float]) -> str:
        if score is None:
            return "--"
        if score < 35:
            return "Low"
        if score < 70:
            return "Mid"
        return "High"

    def _cache_status_text(self, summary: Optional[dict]) -> str:
        if not summary:
            return "--"
        has_loudness = bool(summary.get("has_loudness"))
        has_bpm_key = bool(summary.get("has_bpm_key"))
        has_rms_edges = bool(summary.get("has_rms_edges"))
        has_adaptive = bool(summary.get("has_adaptive_metrics"))
        if has_loudness and has_bpm_key and has_rms_edges:
            return "Full"
        if has_loudness or has_bpm_key or has_rms_edges or has_adaptive:
            return "Partial"
        return "--"

    def _clone_overrides(self, overrides: PresetOverrides) -> PresetOverrides:
        return PresetOverrides(
            lpf_hz=overrides.lpf_hz,
            hpf_hz=overrides.hpf_hz,
            lpf_q=overrides.lpf_q,
            saturation_scale=overrides.saturation_scale,
            tape_drive=overrides.tape_drive,
            tape_bias=overrides.tape_bias,
            compression_scale=overrides.compression_scale,
            comp_attack_ms=overrides.comp_attack_ms,
            comp_release_ms=overrides.comp_release_ms,
            comp_ratio=overrides.comp_ratio,
            bit_depth=overrides.bit_depth,
            sample_rate_reduction_hz=overrides.sample_rate_reduction_hz,
            wow_depth=overrides.wow_depth,
            wow_rate_hz=overrides.wow_rate_hz,
            flutter_depth=overrides.flutter_depth,
            flutter_rate_hz=overrides.flutter_rate_hz,
            stereo_width=overrides.stereo_width,
            vinyl_noise_level_db=overrides.vinyl_noise_level_db,
            tape_hiss_level_db=overrides.tape_hiss_level_db,
            atmosphere_volume_db=overrides.atmosphere_volume_db,
            atmosphere_stereo_width=overrides.atmosphere_stereo_width,
            atmosphere_lpf_hz=overrides.atmosphere_lpf_hz,
        )

    def _clone_override_map(self) -> dict[PresetName, PresetOverrides]:
        cloned: dict[PresetName, PresetOverrides] = {}
        for preset in PresetName:
            cloned[preset] = self._clone_overrides(
                self._preset_overrides_by_preset.get(preset, PresetOverrides())
            )
        return cloned

    def _active_preset(self) -> PresetName:
        return PresetName(self.preset_combo.currentText())

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(path.resolve()).casefold()

    def _analysis_exclusion_keys(self, songs_folder: Path) -> set[str]:
        excluded: set[str] = set()
        candidates: list[Path] = []

        output_text = self.output_line.text().strip()
        if output_text:
            output_path = Path(output_text)
            candidates.extend([output_path, output_path.with_suffix(".mp3"), output_path.with_suffix(".wav")])
        else:
            candidates.extend([songs_folder / "mix.mp3", songs_folder / "mix.wav"])

        rain_text = self.rain_line.text().strip()
        if rain_text:
            candidates.append(Path(rain_text))

        for path in candidates:
            try:
                excluded.add(self._path_key(path))
            except Exception:
                continue
        return excluded

    def _smart_ordering_mode(self) -> SmartOrderingMode:
        raw = self.smart_ordering_mode_combo.currentData()
        try:
            return SmartOrderingMode(raw)
        except Exception:
            return SmartOrderingMode.bpm_key_balanced

    def _refresh_smart_ordering_mode_state(self) -> None:
        enabled = self.smart_crossfade_checkbox.isChecked() and self.smart_ordering_checkbox.isChecked()
        self.smart_ordering_mode_combo.setEnabled(enabled)

    def _on_smart_crossfade_toggled(self, enabled: bool) -> None:
        self.smart_ordering_checkbox.setEnabled(enabled)
        if not enabled:
            self.smart_ordering_checkbox.setChecked(False)
        self._refresh_smart_ordering_mode_state()
        self._on_plan_controls_changed()

    def _apply_recommended_defaults(self, mark_dirty: bool = True) -> None:
        if not hasattr(self, "preset_combo"):
            return
        self._syncing_simple_controls = True
        try:
            self.preset_combo.setCurrentText(PresetName.tokyo_cassette.value)
            self.quality_combo.setCurrentText(QualityMode.balanced.value)
            self.adaptive_checkbox.setChecked(True)
            self.smart_crossfade_checkbox.setChecked(True)
            self.smart_ordering_checkbox.setChecked(True)
            mode_idx = self.smart_ordering_mode_combo.findData(SmartOrderingMode.bpm_key_balanced.value)
            if mode_idx >= 0:
                self.smart_ordering_mode_combo.setCurrentIndex(mode_idx)
            self.shuffle_checkbox.setChecked(False)
            self.crossfade_spin.setValue(6.0)
            self.lufs_spin.setValue(-14.0)
            self.target_checkbox.setChecked(False)
            self.target_spin.setValue(60)
            self.preview_checkbox.setChecked(False)
            self.preview_spin.setValue(60)
            self.rain_slider.setValue(-28)
            self.format_combo.setCurrentText(OutputFormat.mp3.value)
            self.bitrate_combo.setCurrentText("192k")
            self.chunk_output_checkbox.setChecked(False)
            self.chunk_minutes_spin.setValue(10)
        finally:
            self._syncing_simple_controls = False
        self._refresh_smart_ordering_mode_state()
        self._refresh_bitrate_state()
        self._refresh_chunk_output_state()
        self._sync_simple_from_advanced()
        if mark_dirty:
            self._on_plan_controls_changed()

    def _reset_to_recommended(self) -> None:
        self._apply_recommended_defaults(mark_dirty=True)
        self._append_log("Recommended settings applied.")

    def _sync_simple_from_advanced(self, *_args) -> None:
        if self._syncing_simple_controls or not hasattr(self, "simple_preset_combo"):
            return
        self._syncing_simple_controls = True
        try:
            self.simple_preset_combo.setCurrentText(self.preset_combo.currentText())
            self.simple_output_format_combo.setCurrentText(self.format_combo.currentText())
            self.simple_target_checkbox.setChecked(self.target_checkbox.isChecked())
            self.simple_target_spin.setValue(self.target_spin.value())
            self.simple_preview_checkbox.setChecked(self.preview_checkbox.isChecked())
            self.simple_preview_spin.setValue(self.preview_spin.value())
        finally:
            self._syncing_simple_controls = False

    def _apply_simple_controls_to_advanced(self, *_args) -> None:
        if self._syncing_simple_controls or not hasattr(self, "preset_combo"):
            return
        if hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() != 0:
            return
        self._syncing_simple_controls = True
        try:
            # Keep hidden expert options pinned to the recommended baseline in simple mode.
            self.quality_combo.setCurrentText(QualityMode.balanced.value)
            self.adaptive_checkbox.setChecked(True)
            self.smart_crossfade_checkbox.setChecked(True)
            self.smart_ordering_checkbox.setChecked(True)
            mode_idx = self.smart_ordering_mode_combo.findData(SmartOrderingMode.bpm_key_balanced.value)
            if mode_idx >= 0:
                self.smart_ordering_mode_combo.setCurrentIndex(mode_idx)
            self.shuffle_checkbox.setChecked(False)
            self.crossfade_spin.setValue(6.0)
            self.lufs_spin.setValue(-14.0)
            self.rain_slider.setValue(-28)
            self.bitrate_combo.setCurrentText("192k")
            self.chunk_output_checkbox.setChecked(False)
            self.chunk_minutes_spin.setValue(10)

            self.preset_combo.setCurrentText(self.simple_preset_combo.currentText())
            self.format_combo.setCurrentText(self.simple_output_format_combo.currentText())
            self.target_checkbox.setChecked(self.simple_target_checkbox.isChecked())
            self.target_spin.setValue(self.simple_target_spin.value())
            self.preview_checkbox.setChecked(self.simple_preview_checkbox.isChecked())
            self.preview_spin.setValue(self.simple_preview_spin.value())
        finally:
            self._syncing_simple_controls = False
        self._on_plan_controls_changed()

    def _on_mode_tab_changed(self, index: int) -> None:
        if not hasattr(self, "top_controls_frame"):
            return
        simple_mode = index == 0
        if simple_mode and not self._simple_mode_initialized:
            self._apply_recommended_defaults(mark_dirty=False)
            self._simple_mode_initialized = True
        if simple_mode:
            self._apply_simple_controls_to_advanced()
        self.simple_mode_frame.setVisible(simple_mode)
        self.top_controls_frame.setVisible(not simple_mode)
        if simple_mode:
            self.mode_mode_hint.setText(
                "Simple mode: only core controls are shown. Advanced uses full control cards below."
            )
        else:
            self.mode_mode_hint.setText(
                "Advanced mode active: full control cards are shown below."
            )
        self._sync_simple_from_advanced()

    def _active_workspace_mode(self) -> WorkspaceMode:
        if hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() == 0:
            return WorkspaceMode.simple
        return WorkspaceMode.advanced

    def _set_workspace_mode(self, mode: WorkspaceMode) -> None:
        if not hasattr(self, "mode_tabs"):
            return
        idx = 0 if mode == WorkspaceMode.simple else 1
        if self.mode_tabs.currentIndex() != idx:
            self.mode_tabs.setCurrentIndex(idx)
        else:
            self._on_mode_tab_changed(idx)

    def _analysis_validation_error(self) -> Optional[str]:
        songs_folder_text = self.folder_line.text().strip()
        if not songs_folder_text:
            return "Songs folder is required."
        songs_folder = Path(songs_folder_text)
        if not songs_folder.exists() or not songs_folder.is_dir():
            return f"Songs folder does not exist or is not a directory: {songs_folder}"
        return None

    def _render_validation_error(self) -> Optional[str]:
        songs_error = self._analysis_validation_error()
        if songs_error:
            return songs_error
        output_text = self.output_line.text().strip()
        if not output_text:
            return "Output path is required."
        output = Path(output_text)
        if output.exists() and output.is_dir():
            return f"Output path points to a directory, not a file: {output}"
        parent = output.parent if output.parent != Path("") else Path(".")
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return f"Cannot create/access output directory {parent}: {exc}"
        cache_text = self.cache_folder_line.text().strip()
        if cache_text:
            cache_root = Path(cache_text)
            if cache_root.exists() and cache_root.is_file():
                return f"Render cache path points to a file, not a folder: {cache_root}"
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return f"Cannot create/access render cache folder {cache_root}: {exc}"
        return None

    def _refresh_action_state(self) -> None:
        analysis_error = self._analysis_validation_error()
        render_error = self._render_validation_error()
        if analysis_error:
            self.validation_label.setText(analysis_error)
        elif self._session is not None and render_error:
            self.validation_label.setText(render_error)
        else:
            self.validation_label.setText("")
        self.analyze_btn.setEnabled((not self._busy) and analysis_error is None)
        self.render_btn.setEnabled((not self._busy) and self._session is not None and render_error is None)
        self.cancel_btn.setEnabled(self._busy)
        self._refresh_preview_controls()

    def _preload_tracks_from_folder(self) -> None:
        if self._busy:
            return
        folder_text = self.folder_line.text().strip()
        folder = Path(folder_text) if folder_text else None
        if folder is None or not folder.exists() or not folder.is_dir():
            return
        if self._session is not None and self._session.settings.songs_folder == folder:
            return
        try:
            files = discover_audio_files(folder)
        except Exception as exc:
            self._append_log(f"Preload warning: {exc}")
            return
        excluded_keys = self._analysis_exclusion_keys(folder)
        if excluded_keys:
            before = len(files)
            files = [p for p in files if self._path_key(p) not in excluded_keys]
            removed = before - len(files)
            if removed > 0:
                self._append_log(f"Skipped {removed} non-source audio file(s) (output/rain).")
        self.track_tree.clear()
        full_cache_hits = 0
        partial_cache_hits = 0
        for idx, path in enumerate(files, start=1):
            summary = read_analysis_cache_summary(path)
            bpm_text = "--"
            key_text = "--"
            cache_text = self._cache_status_text(summary)
            if summary is not None:
                bpm = summary.get("bpm")
                key = summary.get("key")
                if isinstance(bpm, float):
                    bpm_text = f"{bpm:.1f}"
                if isinstance(key, str) and key:
                    key_text = key
                if cache_text == "Full":
                    full_cache_hits += 1
                elif cache_text == "Partial":
                    partial_cache_hits += 1
            item = QTreeWidgetItem(
                [
                    str(idx),
                    path.name,
                    "--",
                    bpm_text,
                    key_text,
                    cache_text,
                    "--",
                ]
            )
            item.setData(0, Qt.UserRole, str(path))
            self.track_tree.addTopLevelItem(item)
        if files:
            self._append_log(f"Loaded {len(files)} tracks (pre-analysis).")
            if full_cache_hits > 0 or partial_cache_hits > 0:
                self._append_log(
                    (
                        "Found reusable analysis cache for "
                        f"{full_cache_hits + partial_cache_hits}/{len(files)} tracks "
                        f"(full: {full_cache_hits}, partial: {partial_cache_hits})."
                    )
                )
            self._renumber_tree(self.track_tree)
            self.track_tree.resizeColumnToContents(0)
            self.track_tree.resizeColumnToContents(2)
            self.track_tree.resizeColumnToContents(3)
            self.track_tree.resizeColumnToContents(4)
            self.track_tree.resizeColumnToContents(5)
            self.track_tree.resizeColumnToContents(6)

    def _preview_ready(self) -> bool:
        return self._preview_audio_path is not None and self._preview_audio_path.exists()

    def _refresh_preview_controls(self) -> None:
        if not hasattr(self, "preview_short_btn"):
            return
        can_build = (not self._busy) and self._session is not None
        self.preview_short_btn.setEnabled(can_build)
        self.preview_full_btn.setEnabled(can_build)
        media_ok = MULTIMEDIA_AVAILABLE and self._player is not None
        ready = (not self._busy) and self._preview_ready() and media_ok
        playing = (
            ready
            and self._player is not None
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        self.preview_play_btn.setEnabled(ready)
        self.preview_stop_btn.setEnabled(ready)
        self.preview_seek_slider.setEnabled(ready)
        self.preview_play_btn.setText("Pause" if playing else "Play")
        if not MULTIMEDIA_AVAILABLE:
            self.preview_status_label.setText("Preview: playback unavailable (QtMultimedia missing)")
        elif self._preview_audio_path is None:
            self.preview_status_label.setText("Preview: not built")
        elif self._preview_dirty:
            self.preview_status_label.setText("Preview: stale (rebuild recommended)")
        else:
            self.preview_status_label.setText(f"Preview: ready ({self._preview_audio_path.name})")
        if not ready:
            self.preview_seek_slider.setRange(0, 0)
            self.preview_seek_slider.setValue(0)
            self.preview_pos_label.setText("00:00:00")
            self.preview_total_label.setText("00:00:00")

    def _mark_preview_dirty(self, *_args) -> None:
        if self._preview_audio_path is not None:
            self._preview_dirty = True
        self._refresh_preview_controls()

    def _check_storage_before_render(self, session: EngineSessionModel, settings: GuiSettings) -> bool:
        try:
            estimate = self._service.estimate_render_storage(session=session, settings=settings)
        except Exception as exc:
            QMessageBox.warning(self, "Storage Check Failed", f"Could not evaluate storage requirements: {exc}")
            return False

        required_cache = int(estimate["required_cache_bytes"])
        available_cache = int(estimate["available_cache_bytes"])
        cache_root = Path(estimate["cache_root"])
        estimated_output = int(estimate["estimated_output_bytes"])
        available_output = int(estimate["available_output_bytes"])
        output_root = Path(estimate["output_root"])

        if available_cache < required_cache:
            self._append_log(
                "Storage check failed: "
                f"cache requires {self._human_size(required_cache)}, "
                f"available {self._human_size(available_cache)} at {cache_root}."
            )
            QMessageBox.warning(
                self,
                "Insufficient Cache Storage",
                (
                    "Not enough free storage for render cache.\n\n"
                    f"Cache folder: {cache_root}\n"
                    f"Required (estimated): {self._human_size(required_cache)}\n"
                    f"Available: {self._human_size(available_cache)}\n\n"
                    "Set a different Render Cache Folder and try again."
                ),
            )
            return False

        if available_output < estimated_output:
            self._append_log(
                "Storage check failed: "
                f"output requires {self._human_size(estimated_output)}, "
                f"available {self._human_size(available_output)} at {output_root}."
            )
            QMessageBox.warning(
                self,
                "Insufficient Output Storage",
                (
                    "Not enough free storage for final output.\n\n"
                    f"Output folder: {output_root}\n"
                    f"Estimated output: {self._human_size(estimated_output)}\n"
                    f"Available: {self._human_size(available_output)}"
                ),
            )
            return False
        return True

    def _on_plan_controls_changed(self, *_args) -> None:
        if self._syncing_simple_controls:
            return
        self._mark_preview_dirty()
        self._rebuild_plan_from_ui()

    def _rebuild_plan_from_ui(self) -> None:
        if self._busy or self._session is None:
            return
        try:
            settings = self._collect_settings(require_output=False)
            self._session = self._service.rebuild_plan(
                session=self._session,
                settings=settings,
                ordered_paths=self._current_ordered_paths() or self._session.ordered_paths,
            )
            self.timeline.set_plan(self._session.mix_plan, has_rain=settings.rain_path is not None)
            self._refresh_estimates()
        except Exception as exc:
            self._append_log(f"Plan refresh warning: {exc}")

    def _human_size(self, size_bytes: float) -> str:
        units = ["B", "KB", "MB", "GB"]
        value = max(0.0, float(size_bytes))
        idx = 0
        while value >= 1024.0 and idx < len(units) - 1:
            value /= 1024.0
            idx += 1
        return f"{value:.1f} {units[idx]}"

    def _selected_bitrate_bps(self) -> int:
        raw = self.bitrate_combo.currentText().strip().lower()
        if raw.endswith("k") and raw[:-1].isdigit():
            return int(raw[:-1]) * 1000
        return 192_000

    def _refresh_bitrate_state(self, *_args) -> None:
        if not hasattr(self, "bitrate_combo"):
            return
        is_mp3 = self.format_combo.currentText() == OutputFormat.mp3.value
        self.bitrate_combo.setEnabled(is_mp3 and (not self._busy))

    def _refresh_chunk_output_state(self, *_args) -> None:
        if not hasattr(self, "chunk_output_checkbox"):
            return
        is_mp3 = self.format_combo.currentText() == OutputFormat.mp3.value
        self.chunk_output_checkbox.setEnabled(is_mp3 and (not self._busy))
        self.chunk_minutes_spin.setEnabled(
            is_mp3 and self.chunk_output_checkbox.isChecked() and (not self._busy)
        )

    def _estimate_output_size_bytes(self, duration_sec: float) -> float:
        fmt = self.format_combo.currentText()
        if fmt == OutputFormat.wav.value:
            # 48kHz stereo 16-bit PCM (2 bytes per sample per channel)
            bytes_per_second = 48_000 * 2 * 2
            return duration_sec * bytes_per_second
        bitrate_bps = self._selected_bitrate_bps()
        return duration_sec * (bitrate_bps / 8.0)

    def _refresh_estimates(self, *_args) -> None:
        if self._session is None:
            self.timeline_duration_label.setText("Timeline: Analyze to estimate")
            self.render_duration_label.setText("Render Scope: --")
            self.estimated_size_label.setText("Estimated Size: --")
            return

        full_ms = max(1, self._session.mix_plan.estimated_duration_ms)
        if self.preview_checkbox.isChecked():
            render_ms = int(max(10.0, float(self.preview_spin.value())) * 1000)
        else:
            render_ms = full_ms

        self.timeline_duration_label.setText(f"Timeline: {format_hms(full_ms)}")
        scope = f"{format_hms(render_ms)} ({'preview' if self.preview_checkbox.isChecked() else 'full'})"
        self.render_duration_label.setText(f"Render Scope: {scope}")
        estimated_size = self._estimate_output_size_bytes(render_ms / 1000.0)
        self.estimated_size_label.setText(f"Estimated Size: {self._human_size(estimated_size)}")

    def _collect_settings(
        self,
        require_output: bool = True,
        output_override: Optional[Path] = None,
    ) -> GuiSettings:
        songs_folder_text = self.folder_line.text().strip()
        output_text = self.output_line.text().strip()
        if not songs_folder_text:
            raise ValueError("Songs folder is required.")
        songs_folder = Path(songs_folder_text)
        cache_folder_text = self.cache_folder_line.text().strip()
        cache_folder = Path(cache_folder_text) if cache_folder_text else None
        if output_override is not None:
            output = output_override
        elif output_text:
            output = Path(output_text)
        elif require_output:
            raise ValueError("Output path is required.")
        else:
            suffix = ".wav" if self.format_combo.currentText() == OutputFormat.wav.value else ".mp3"
            output = songs_folder / f"mix{suffix}"
        rain = Path(self.rain_line.text().strip()) if self.rain_line.text().strip() else None
        target = self.target_spin.value() if self.target_checkbox.isChecked() else None
        preset = self._active_preset()
        active_override = self._clone_overrides(
            self._preset_overrides_by_preset.get(preset, PresetOverrides())
        )
        output_format = OutputFormat(self.format_combo.currentText())
        return GuiSettings(
            songs_folder=songs_folder,
            output_path=output,
            cache_folder=cache_folder,
            rain_path=rain,
            preset=preset,
            quality_mode=QualityMode(self.quality_combo.currentText()),
            output_format=output_format,
            bitrate=self.bitrate_combo.currentText().strip() or "192k",
            output_chunks_enabled=self.chunk_output_checkbox.isChecked(),
            output_chunk_minutes=max(1, int(self.chunk_minutes_spin.value())),
            adaptive_lofi=self.adaptive_checkbox.isChecked(),
            adaptive_report=Path(self.adaptive_report_line.text().strip() or "adaptive_report.json"),
            rain_level_db=float(self.rain_slider.value()),
            rain_presence=RainPresence(str(self.rain_presence_combo.currentData())),
            rain_preserve_low_drops=self.rain_low_drops_checkbox.isChecked(),
            crossfade_sec=self.crossfade_spin.value(),
            lufs=self.lufs_spin.value(),
            shuffle=self.shuffle_checkbox.isChecked(),
            target_duration_min=target,
            smart_crossfade=self.smart_crossfade_checkbox.isChecked(),
            smart_ordering=self.smart_crossfade_checkbox.isChecked() and self.smart_ordering_checkbox.isChecked(),
            smart_ordering_mode=self._smart_ordering_mode(),
            preview_mode=self.preview_checkbox.isChecked(),
            preview_duration_sec=float(self.preview_spin.value()),
            workspace_mode=self._active_workspace_mode(),
            metadata_tags=dict(self._render_metadata_tags),
            preset_overrides=active_override,
            preset_overrides_by_name=self._clone_override_map(),
        )

    def _current_ordered_paths(self) -> list[Path]:
        ordered: list[Path] = []
        for idx in range(self.track_tree.topLevelItemCount()):
            item = self.track_tree.topLevelItem(idx)
            path_text = item.data(0, Qt.UserRole)
            if path_text:
                ordered.append(Path(path_text))
        return ordered

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if hasattr(self, "workspace_tabs"):
            self.workspace_tabs.setEnabled(not busy)
        for widget in (
            self.folder_line,
            self.folder_browse_btn,
            self.mode_tabs,
            self.reset_recommended_btn,
            self.remove_track_btn,
            self.simple_preset_combo,
            self.simple_output_format_combo,
            self.simple_target_checkbox,
            self.simple_target_spin,
            self.simple_preview_checkbox,
            self.simple_preview_spin,
            self.rain_line,
            self.rain_browse_btn,
            self.output_line,
            self.output_browse_btn,
            self.adaptive_report_line,
            self.adaptive_report_btn,
            self.cache_folder_line,
            self.cache_folder_btn,
            self.preset_combo,
            self.preset_editor_btn,
            self.reset_preset_btn,
            self.reset_all_presets_btn,
            self.quality_combo,
            self.adaptive_checkbox,
            self.smart_crossfade_checkbox,
            self.smart_ordering_checkbox,
            self.smart_ordering_mode_combo,
            self.shuffle_checkbox,
            self.crossfade_spin,
            self.lufs_spin,
            self.target_checkbox,
            self.target_spin,
            self.preview_checkbox,
            self.preview_spin,
            self.format_combo,
            self.bitrate_combo,
            self.chunk_output_checkbox,
            self.chunk_minutes_spin,
            self.save_project_btn,
            self.load_project_btn,
            self.preview_short_btn,
            self.preview_full_btn,
            self.preview_play_btn,
            self.preview_stop_btn,
            self.preview_seek_slider,
            self.track_tree,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self.target_spin.setEnabled(self.target_checkbox.isChecked())
            self.preview_spin.setEnabled(self.preview_checkbox.isChecked())
            self._refresh_smart_ordering_mode_state()
            self._refresh_bitrate_state()
            self._refresh_chunk_output_state()
        self._refresh_action_state()
        self._refresh_preview_controls()

    def _media_tools_busy(self) -> bool:
        return bool(
            (self._mp3_split_worker and self._mp3_split_worker.isRunning())
            or (self._mp4_stitch_worker and self._mp4_stitch_worker.isRunning())
        )

    def _choose_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select Songs Folder", self.folder_line.text().strip() or ".")
        if directory:
            self.folder_line.setText(directory)
            self._preload_tracks_from_folder()

    def _choose_rain(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Rain File",
            self.rain_line.text().strip() or ".",
            "Audio (*.mp3 *.wav *.m4a *.flac *.ogg *.opus *.aac)",
        )
        if file_path:
            self.rain_line.setText(file_path)
            self._mark_preview_dirty()

    def _choose_adaptive_report(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Adaptive Report Path",
            self.adaptive_report_line.text().strip() or "adaptive_report.json",
            "JSON (*.json)",
        )
        if file_path:
            self.adaptive_report_line.setText(file_path)
            self._mark_preview_dirty()

    def _choose_cache_folder(self) -> None:
        start = self.cache_folder_line.text().strip() or tempfile.gettempdir()
        folder = QFileDialog.getExistingDirectory(self, "Render Cache Folder", start)
        if folder:
            self.cache_folder_line.setText(folder)

    def _choose_output(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Select Output Path",
            self.output_line.text().strip() or "mix.mp3",
            "Audio (*.mp3 *.wav)",
        )
        if file_path:
            self.output_line.setText(file_path)
            suffix = Path(file_path).suffix.lower()
            if suffix == ".wav":
                self.format_combo.setCurrentText("wav")
            elif suffix == ".mp3":
                self.format_combo.setCurrentText("mp3")
            self._mark_preview_dirty()

    def _on_preset_changed(self, *_args) -> None:
        preset = self._active_preset()
        if preset not in self._preset_overrides_by_preset:
            self._preset_overrides_by_preset[preset] = PresetOverrides()
        self._sync_simple_from_advanced()
        self._mark_preview_dirty()

    def _reset_current_preset_overrides(self) -> None:
        preset = self._active_preset()
        self._preset_overrides_by_preset[preset] = PresetOverrides()
        self._append_log(f"Preset overrides reset for {preset.value}.")
        self._mark_preview_dirty()

    def _reset_all_preset_overrides(self) -> None:
        self._preset_overrides_by_preset = {preset: PresetOverrides() for preset in PresetName}
        self._append_log("Preset overrides reset for all presets.")
        self._mark_preview_dirty()

    def _open_preset_editor(self) -> None:
        preset = self._active_preset()
        active = self._preset_overrides_by_preset.get(preset, PresetOverrides())
        base_preset = get_preset(preset)
        dialog = PresetEditorDialog(
            active,
            base_preset=base_preset,
            preset_name=preset.value,
            parent=self,
        )
        if dialog.exec():
            self._preset_overrides_by_preset[preset] = dialog.value()
            updated = self._preset_overrides_by_preset[preset]
            self._append_log(
                f"{preset.value} overrides updated: lpf={updated.lpf_hz}, "
                f"hpf={updated.hpf_hz}, "
                f"sat={updated.saturation_scale}, "
                f"comp={updated.compression_scale}, "
                f"bits={updated.bit_depth}, "
                f"sr={updated.sample_rate_reduction_hz}"
            )
            self._mark_preview_dirty()

    def _prompt_render_metadata(self) -> Optional[dict[str, str]]:
        dialog = RenderMetadataDialog(initial=self._render_metadata_tags, parent=self)
        if not dialog.exec():
            return None
        if dialog.skip_requested:
            return {}
        return dialog.metadata_tags()

    def _start_analysis(self, ordered_paths: Optional[list[Path]] = None) -> None:
        if self._analysis_worker and self._analysis_worker.isRunning():
            return
        if self._media_tools_busy():
            QMessageBox.warning(self, "Busy", "Wait for MP3 Splitter/MP4 Stitcher to finish first.")
            return
        validation_error = self._analysis_validation_error()
        if validation_error:
            QMessageBox.warning(self, "Invalid Settings", validation_error)
            return
        try:
            settings = self._collect_settings(require_output=False)
        except Exception as exc:
            QMessageBox.critical(self, "Invalid Settings", str(exc))
            return

        self.progress_bar.setValue(0)
        self.log_console.clear()
        self._set_busy(True)
        self._render_mode = "analysis"
        self._append_log("Starting analysis...")
        worker_ordered_paths: Optional[list[Path]]
        if ordered_paths is not None:
            worker_ordered_paths = ordered_paths
        else:
            visible_paths = self._current_ordered_paths()
            worker_ordered_paths = visible_paths if visible_paths else None
            if settings.smart_crossfade and settings.smart_ordering:
                if worker_ordered_paths is None:
                    self._append_log("Recomputing smart ordering from analysis metrics.")
                else:
                    self._append_log("Applying smart ordering on current selected track subset.")
        self._analysis_worker = AnalysisWorker(
            service=self._service,
            settings=settings,
            ordered_paths=worker_ordered_paths,
        )
        self._analysis_worker.log.connect(self._append_log)
        self._analysis_worker.progress.connect(self._on_analysis_progress)
        self._analysis_worker.finished_session.connect(self._on_analysis_finished)
        self._analysis_worker.failed.connect(self._on_worker_failed)
        self._analysis_worker.cancelled.connect(self._on_worker_cancelled)
        self._analysis_worker.start()

    def _on_analysis_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self.progress_bar.setValue(0)
            return
        self.progress_bar.setValue(int(current / total * 1000))

    def _on_analysis_finished(self, session: EngineSessionModel) -> None:
        self._session = session
        self._render_mode = "final"
        self._set_busy(False)
        self.progress_bar.setValue(1000)
        self._populate_tracks(session)
        self.timeline.set_plan(session.mix_plan, has_rain=session.settings.rain_path is not None)
        self._refresh_estimates()
        self._mark_preview_dirty()
        if session.settings.adaptive_lofi:
            try:
                report, proc = self._service.export_adaptive_report(session, settings=session.settings)
                self._append_log(f"Adaptive report updated: {report}")
                self._append_log(f"Processing tracklist updated: {proc}")
            except Exception as exc:
                self._append_log(f"Adaptive report export warning: {exc}")
        if session.warnings:
            self._append_log(f"Warnings: {len(session.warnings)}")
            shown = 8
            for warning in session.warnings[:shown]:
                self._append_log(f"- {warning}")
            if len(session.warnings) > shown:
                self._append_log(f"... and {len(session.warnings) - shown} more warnings.")

    def _populate_tracks(self, session: EngineSessionModel) -> None:
        self.track_tree.clear()
        analysis_by_id = session.analyses
        for idx, src in enumerate(session.track_sources, start=1):
            analysis = analysis_by_id.get(src.id)
            bpm = f"{analysis.bpm:.1f}" if analysis and analysis.bpm else "--"
            key = analysis.key if analysis and analysis.key else "--"
            cache_text = self._cache_status_text(read_analysis_cache_summary(src.path))
            score = (
                analysis.adaptive_processing.lofi_needed_score
                if analysis and analysis.adaptive_processing
                else None
            )
            item = QTreeWidgetItem(
                [
                    str(idx),
                    src.path.name,
                    format_hms(src.duration_ms),
                    bpm,
                    key,
                    cache_text,
                    self._adaptive_status_text(score),
                ]
            )
            item.setData(0, Qt.UserRole, str(src.path))
            item.setData(1, Qt.UserRole, src.id)
            item.setForeground(6, self._status_brush(score))
            self.track_tree.addTopLevelItem(item)
        self._renumber_tree(self.track_tree)
        self.track_tree.resizeColumnToContents(0)
        self.track_tree.resizeColumnToContents(2)
        self.track_tree.resizeColumnToContents(3)
        self.track_tree.resizeColumnToContents(4)
        self.track_tree.resizeColumnToContents(5)
        self.track_tree.resizeColumnToContents(6)
        if self.track_tree.topLevelItemCount() > 0:
            self.track_tree.setCurrentItem(self.track_tree.topLevelItem(0))

    def _on_track_selected(self) -> None:
        if not self._session:
            return
        items = self.track_tree.selectedItems()
        if not items:
            return
        item = items[0]
        track_id = item.data(1, Qt.UserRole)
        analysis = self._session.analyses.get(track_id)
        if analysis is None:
            return
        metrics = {
            "lufs": analysis.adaptive_metrics.lufs if analysis.adaptive_metrics else analysis.loudness.input_i,
            "crest_factor_db": analysis.adaptive_metrics.crest_factor_db if analysis.adaptive_metrics else None,
            "spectral_centroid_hz": analysis.adaptive_metrics.spectral_centroid_hz if analysis.adaptive_metrics else None,
            "rolloff_hz": analysis.adaptive_metrics.rolloff_hz if analysis.adaptive_metrics else None,
            "stereo_width": analysis.adaptive_metrics.stereo_width if analysis.adaptive_metrics else None,
            "noise_floor_dbfs": analysis.adaptive_metrics.noise_floor_dbfs if analysis.adaptive_metrics else None,
        }
        processing = {
            "lpf_cutoff_hz": analysis.adaptive_processing.lpf_cutoff_hz if analysis.adaptive_processing else None,
            "saturation_strength": analysis.adaptive_processing.saturation_strength
            if analysis.adaptive_processing
            else None,
            "compression_strength": analysis.adaptive_processing.compression_strength
            if analysis.adaptive_processing
            else None,
            "stereo_width_target": analysis.adaptive_processing.stereo_width_target
            if analysis.adaptive_processing
            else None,
            "noise_added_db": analysis.adaptive_processing.noise_added_db if analysis.adaptive_processing else None,
        }
        for key, label in self.metric_labels.items():
            value = metrics.get(key)
            label.setText("--" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value))
        for key, label in self.proc_labels.items():
            value = processing.get(key)
            label.setText("--" if value is None else f"{value:.2f}" if isinstance(value, float) else str(value))
        rationale = (
            analysis.adaptive_processing.rationale
            if analysis.adaptive_processing
            else "No adaptive rationale available."
        )
        self.rationale_console.setPlainText(rationale)

    def _on_track_order_changed(self) -> None:
        self._renumber_tree(self.track_tree)
        if not self._session:
            return
        try:
            settings = self._collect_settings(require_output=False)
            self._session = self._service.rebuild_plan(
                session=self._session,
                settings=settings,
                ordered_paths=self._current_ordered_paths(),
            )
            self.timeline.set_plan(self._session.mix_plan, has_rain=settings.rain_path is not None)
            self._refresh_estimates()
            self._append_log("Timeline updated after drag reorder.")
            self._mark_preview_dirty()
        except Exception as exc:
            self._append_log(f"Reorder update failed: {exc}")

    def _remove_selected_lofi_tracks(self) -> None:
        items = self.track_tree.selectedItems()
        if not items:
            return
        for item in items:
            idx = self.track_tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self.track_tree.takeTopLevelItem(idx)
        self._renumber_tree(self.track_tree)
        self._append_log(f"Removed {len(items)} track(s) from list.")
        self._mark_preview_dirty()
        if not self._session:
            return
        ordered_paths = self._current_ordered_paths()
        if not ordered_paths:
            self._session = None
            self.timeline.set_plan(None, has_rain=bool(self.rain_line.text().strip()))
            self._refresh_estimates()
            self._refresh_action_state()
            return
        try:
            settings = self._collect_settings(require_output=False)
            self._session = self._service.rebuild_plan(
                session=self._session,
                settings=settings,
                ordered_paths=ordered_paths,
            )
            self.timeline.set_plan(self._session.mix_plan, has_rain=settings.rain_path is not None)
            self._refresh_estimates()
            self._append_log("Timeline rebuilt after removing tracks.")
        except Exception as exc:
            self._append_log(f"Remove/rebuild warning: {exc}")

    def _start_render(self) -> None:
        if self._render_worker and self._render_worker.isRunning():
            return
        if self._media_tools_busy():
            QMessageBox.warning(self, "Busy", "Wait for MP3 Splitter/MP4 Stitcher to finish first.")
            return
        validation_error = self._render_validation_error()
        if validation_error:
            QMessageBox.warning(self, "Invalid Settings", validation_error)
            return
        if self._session is None:
            QMessageBox.warning(self, "No Analysis", "Analyze tracks before rendering.")
            return
        metadata_tags = self._prompt_render_metadata()
        if metadata_tags is None:
            return
        self._render_metadata_tags = metadata_tags
        if metadata_tags:
            self._append_log(f"Render metadata tags set: {', '.join(sorted(metadata_tags.keys()))}")
        else:
            self._append_log("Render metadata tags: none")
        try:
            settings = self._collect_settings()
            self._session = self._service.rebuild_plan(
                session=self._session,
                settings=settings,
                ordered_paths=self._current_ordered_paths() or self._session.ordered_paths,
            )
            self._refresh_estimates()
            if not self._check_storage_before_render(session=self._session, settings=settings):
                return
        except Exception as exc:
            QMessageBox.critical(self, "Render Preparation Failed", str(exc))
            return

        self._set_busy(True)
        self._render_mode = "final"
        self.progress_bar.setValue(0)
        self._append_log("Starting render...")
        self._render_worker = RenderWorker(
            service=self._service,
            session=self._session,
            settings=settings,
        )
        self._render_worker.log.connect(self._append_log)
        self._render_worker.progress.connect(self._on_render_progress)
        self._render_worker.finished_render.connect(self._on_render_finished)
        self._render_worker.failed.connect(self._on_worker_failed)
        self._render_worker.cancelled.connect(self._on_worker_cancelled)
        self._render_worker.start()

    def _start_preview_render(self, short_preview: bool) -> None:
        if self._render_worker and self._render_worker.isRunning():
            return
        if self._media_tools_busy():
            QMessageBox.warning(self, "Busy", "Wait for MP3 Splitter/MP4 Stitcher to finish first.")
            return
        if self._session is None:
            QMessageBox.warning(self, "No Analysis", "Analyze tracks before building preview.")
            return
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        try:
            settings = self._collect_settings(
                require_output=False,
            )
            preview_root = settings.cache_folder if settings.cache_folder is not None else Path(tempfile.gettempdir())
            preview_dir = preview_root / "nightfall_preview"
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_path = preview_dir / "preview_mix.mp3"
            preview_excerpt_mode = short_preview
            settings = replace(
                settings,
                preview_mode=preview_excerpt_mode,
                preview_duration_sec=float(self.preview_spin.value()),
                output_format=OutputFormat.mp3,
                output_path=preview_path,
                metadata_tags={},
            )
            self._session = self._service.rebuild_plan(
                session=self._session,
                settings=settings,
                ordered_paths=self._current_ordered_paths() or self._session.ordered_paths,
            )
            if not self._check_storage_before_render(session=self._session, settings=settings):
                return
        except Exception as exc:
            QMessageBox.critical(self, "Preview Preparation Failed", str(exc))
            return

        self._set_busy(True)
        self._render_mode = "preview"
        self.progress_bar.setValue(0)
        if settings.preview_mode:
            self._append_log(f"Building preview excerpt ({int(settings.preview_duration_sec)}s)...")
        else:
            self._append_log("Building full-length preview render...")
        self._render_worker = RenderWorker(
            service=self._service,
            session=self._session,
            settings=settings,
        )
        self._render_worker.log.connect(self._append_log)
        self._render_worker.progress.connect(self._on_render_progress)
        self._render_worker.finished_render.connect(self._on_render_finished)
        self._render_worker.failed.connect(self._on_worker_failed)
        self._render_worker.cancelled.connect(self._on_worker_cancelled)
        self._render_worker.start()

    def _on_render_progress(self, current_ms: int, total_ms: int) -> None:
        if total_ms <= 0:
            self.progress_bar.setValue(0)
            return
        self.progress_bar.setValue(int(current_ms / total_ms * 1000))

    def _on_render_finished(self, artifacts) -> None:
        self._set_busy(False)
        self.progress_bar.setValue(1000)
        if self._render_mode == "preview":
            self._preview_audio_path = artifacts.output_audio_path
            self._preview_dirty = False
            self._append_log(f"Preview ready: {artifacts.output_audio_path}")
            self._load_preview_into_player()
        else:
            self._append_log("Render completed.")
            self._append_log(f"Output: {artifacts.output_audio_path}")
            self._append_log(f"Tracklist: {artifacts.tracklist_txt_path}")
            self._append_log(f"Timestamps TXT: {artifacts.timestamps_txt_path}")
            self._append_log(f"Timestamps CSV: {artifacts.timestamps_csv_path}")
            if artifacts.chunk_output_paths:
                self._append_log(f"MP3 chunks: {len(artifacts.chunk_output_paths)}")
                for chunk_path in artifacts.chunk_output_paths[:10]:
                    self._append_log(f"- {chunk_path}")
                if len(artifacts.chunk_output_paths) > 10:
                    self._append_log(f"... and {len(artifacts.chunk_output_paths) - 10} more chunk files.")
            if artifacts.adaptive_report_path:
                self._append_log(f"Adaptive Report: {artifacts.adaptive_report_path}")
            QMessageBox.information(self, "Render Complete", f"Mix written to:\n{artifacts.output_audio_path}")
        self._render_mode = "final"
        self._refresh_preview_controls()

    def _load_preview_into_player(self) -> None:
        if not MULTIMEDIA_AVAILABLE or self._player is None or not self._preview_ready():
            return
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(self._preview_audio_path)))
        self._refresh_preview_controls()

    def _toggle_preview_playback(self) -> None:
        if not MULTIMEDIA_AVAILABLE or self._player is None:
            QMessageBox.warning(
                self,
                "Playback Unavailable",
                "QtMultimedia is not available in this environment.",
            )
            return
        if not self._preview_ready():
            QMessageBox.warning(self, "Preview Missing", "Build a preview first.")
            return
        if self._preview_dirty:
            self._append_log("Preview is stale; rebuilding is recommended before listening.")
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()
        self._refresh_preview_controls()

    def _stop_preview_playback(self) -> None:
        if self._player is None:
            return
        self._player.stop()
        self._refresh_preview_controls()

    def _on_playback_state_changed(self, *_args) -> None:
        self._refresh_preview_controls()

    def _on_playback_position_changed(self, position_ms: int) -> None:
        if not hasattr(self, "preview_seek_slider"):
            return
        if not self.preview_seek_slider.isSliderDown():
            self.preview_seek_slider.setValue(max(0, int(position_ms)))
        self.preview_pos_label.setText(format_hms(max(0, int(position_ms))))

    def _on_playback_duration_changed(self, duration_ms: int) -> None:
        if not hasattr(self, "preview_seek_slider"):
            return
        duration_ms = max(0, int(duration_ms))
        self.preview_seek_slider.setRange(0, duration_ms)
        self.preview_total_label.setText(format_hms(duration_ms))

    def _on_seek_slider_moved(self, position_ms: int) -> None:
        self.preview_pos_label.setText(format_hms(max(0, int(position_ms))))
        if self._player is not None:
            self._player.setPosition(max(0, int(position_ms)))

    def _on_playback_error(self, *_args) -> None:
        if self._player is None:
            return
        self._append_log(f"Preview playback error: {self._player.errorString()}")
        self._refresh_preview_controls()

    def _cancel_active_worker(self) -> None:
        if self._analysis_worker and self._analysis_worker.isRunning():
            self._analysis_worker.cancel()
            self._append_log("Cancelling analysis...")
        if self._render_worker and self._render_worker.isRunning():
            self._render_worker.cancel()
            self._append_log("Cancelling render...")

    def _on_worker_failed(self, message: str) -> None:
        self._set_busy(False)
        self._render_mode = "final"
        self._append_log(f"Error: {message}")
        QMessageBox.critical(self, "Operation Failed", message)

    def _on_worker_cancelled(self) -> None:
        self._set_busy(False)
        self._render_mode = "final"
        self._append_log("Operation cancelled.")

    def _save_project(self) -> None:
        try:
            settings = self._collect_settings()
        except Exception as exc:
            QMessageBox.critical(self, "Invalid Settings", str(exc))
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Project",
            str(Path(settings.songs_folder) / "project.nightfall"),
            "Nightfall Project (*.nightfall)",
        )
        if not file_path:
            return
        try:
            save_project_file(Path(file_path), settings=settings, ordered_paths=self._current_ordered_paths())
            self._append_log(f"Project saved: {file_path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))

    def _load_project(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Project",
            str(Path.cwd()),
            "Nightfall Project (*.nightfall)",
        )
        if not file_path:
            return
        try:
            settings, ordered = load_project_file(Path(file_path))
            self._apply_loaded_settings(settings)
            self._append_log(f"Project loaded: {file_path}")
            self._start_analysis(ordered_paths=ordered)
        except Exception as exc:
            QMessageBox.critical(self, "Load Failed", str(exc))

    def _apply_loaded_settings(self, settings: GuiSettings) -> None:
        self._render_metadata_tags = dict(settings.metadata_tags)
        self.folder_line.setText(str(settings.songs_folder))
        self.output_line.setText(str(settings.output_path))
        self.cache_folder_line.setText(str(settings.cache_folder) if settings.cache_folder else "")
        self.rain_line.setText(str(settings.rain_path) if settings.rain_path else "")
        self.preset_combo.setCurrentText(settings.preset.value)
        self.quality_combo.setCurrentText(settings.quality_mode.value)
        self.adaptive_checkbox.setChecked(settings.adaptive_lofi)
        self.smart_crossfade_checkbox.setChecked(settings.smart_crossfade)
        self.smart_ordering_checkbox.setChecked(settings.smart_ordering and settings.smart_crossfade)
        mode_idx = self.smart_ordering_mode_combo.findData(settings.smart_ordering_mode.value)
        if mode_idx >= 0:
            self.smart_ordering_mode_combo.setCurrentIndex(mode_idx)
        self.shuffle_checkbox.setChecked(settings.shuffle)
        self.rain_slider.setValue(int(settings.rain_level_db))
        presence_idx = self.rain_presence_combo.findData(settings.rain_presence.value)
        if presence_idx >= 0:
            self.rain_presence_combo.setCurrentIndex(presence_idx)
        self.rain_low_drops_checkbox.setChecked(settings.rain_preserve_low_drops)
        self.crossfade_spin.setValue(settings.crossfade_sec)
        self.lufs_spin.setValue(settings.lufs)
        self.target_checkbox.setChecked(settings.target_duration_min is not None)
        if settings.target_duration_min is not None:
            self.target_spin.setValue(settings.target_duration_min)
        self.preview_checkbox.setChecked(settings.preview_mode)
        self.preview_spin.setValue(int(settings.preview_duration_sec))
        self.adaptive_report_line.setText(str(settings.adaptive_report))
        self.format_combo.setCurrentText(settings.output_format.value)
        if self.bitrate_combo.findText(settings.bitrate) < 0:
            self.bitrate_combo.addItem(settings.bitrate)
        self.bitrate_combo.setCurrentText(settings.bitrate)
        self.chunk_output_checkbox.setChecked(settings.output_chunks_enabled)
        self.chunk_minutes_spin.setValue(max(1, settings.output_chunk_minutes))
        loaded_map = settings.preset_overrides_by_name
        if loaded_map:
            self._preset_overrides_by_preset = {
                preset: self._clone_overrides(loaded_map.get(preset, PresetOverrides()))
                for preset in PresetName
            }
        else:
            self._preset_overrides_by_preset = {preset: PresetOverrides() for preset in PresetName}
            self._preset_overrides_by_preset[settings.preset] = self._clone_overrides(
                settings.preset_overrides
            )
        self._refresh_smart_ordering_mode_state()
        self._set_workspace_mode(settings.workspace_mode)
        self._refresh_bitrate_state()
        self._refresh_chunk_output_state()
        self._sync_simple_from_advanced()
        self._refresh_action_state()
        self._refresh_estimates()
        self._mark_preview_dirty()
