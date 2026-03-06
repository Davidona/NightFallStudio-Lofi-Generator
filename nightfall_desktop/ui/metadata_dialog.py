from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)


class RenderMetadataDialog(QDialog):
    def __init__(self, initial: Optional[dict[str, str]] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Render Metadata")
        self.setModal(True)
        self.resize(520, 360)
        self._skip_requested = False

        payload = initial or {}

        self._fields: dict[str, QLineEdit] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        help_label = QLabel(
            "Optional metadata tags for the exported file. Leave empty to skip specific fields."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #6A7583;")
        root.addWidget(help_label)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        root.addLayout(form)

        for key, label in (
            ("title", "Title"),
            ("artist", "Artist"),
            ("album", "Album"),
            ("album_artist", "Album Artist"),
            ("genre", "Genre"),
            ("date", "Year/Date"),
            ("composer", "Composer"),
            ("comment", "Comment"),
        ):
            field = QLineEdit(payload.get(key, ""))
            self._fields[key] = field
            form.addRow(label, field)

        actions = QHBoxLayout()
        actions.addStretch(1)

        self.skip_btn = QPushButton("Skip Metadata")
        self.skip_btn.clicked.connect(self._on_skip_clicked)
        actions.addWidget(self.skip_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        actions.addWidget(self.cancel_btn)

        self.apply_btn = QPushButton("Continue Render")
        self.apply_btn.clicked.connect(self._on_apply_clicked)
        self.apply_btn.setDefault(True)
        actions.addWidget(self.apply_btn)
        root.addLayout(actions)

    @property
    def skip_requested(self) -> bool:
        return self._skip_requested

    def metadata_tags(self) -> dict[str, str]:
        tags: dict[str, str] = {}
        for key, field in self._fields.items():
            value = field.text().strip()
            if value:
                tags[key] = value
        return tags

    def _on_skip_clicked(self) -> None:
        self._skip_requested = True
        self.accept()

    def _on_apply_clicked(self) -> None:
        self._skip_requested = False
        self.accept()
