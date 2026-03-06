from __future__ import annotations


TOKYO_DARK_STYLESHEET = """
QWidget {
  background-color: #121417;
  color: #E4E8EB;
  font-family: "Segoe UI";
  font-size: 10pt;
}
QMainWindow {
  background-color: #0F1115;
}
QFrame#panel {
  background-color: #171B22;
  border: 1px solid #2A313D;
  border-radius: 8px;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit {
  background-color: #10141A;
  border: 1px solid #2B3442;
  border-radius: 6px;
  padding: 4px;
}
QPushButton {
  background-color: #1D2430;
  border: 1px solid #334156;
  border-radius: 6px;
  padding: 6px 10px;
}
QPushButton:hover {
  background-color: #253247;
}
QPushButton:disabled {
  background-color: #1A1E25;
  color: #7E8794;
}
QHeaderView::section {
  background-color: #1A2030;
  color: #B8C4D3;
  border: 0;
  border-right: 1px solid #2B3442;
  padding: 4px;
}
QTreeWidget {
  background-color: #0F131A;
  border: 1px solid #2B3442;
  border-radius: 6px;
}
QTreeWidget::item:selected {
  background-color: #2A3A54;
}
QProgressBar {
  border: 1px solid #2B3442;
  border-radius: 6px;
  text-align: center;
  background: #0D1117;
}
QProgressBar::chunk {
  background-color: #3D6EA1;
  border-radius: 5px;
}
QSlider::groove:horizontal {
  height: 6px;
  background: #2A313D;
  border-radius: 3px;
}
QSlider::handle:horizontal {
  background: #7AA2D8;
  width: 14px;
  margin: -4px 0;
  border-radius: 7px;
}
"""

