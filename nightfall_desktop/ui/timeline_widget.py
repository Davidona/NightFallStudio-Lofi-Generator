from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from nightfall_mix.mixer import MixPlan
from nightfall_mix.utils import format_hms


@dataclass
class _Segment:
    rect: QRectF
    track_name: str
    start_ms: int
    end_ms: int


class TimelineWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(180)
        self._plan: Optional[MixPlan] = None
        self._has_rain = False
        self._segments: list[_Segment] = []

    def set_plan(self, plan: Optional[MixPlan], has_rain: bool) -> None:
        self._plan = plan
        self._has_rain = has_rain
        self.update()

    def _duration_ms(self) -> int:
        if not self._plan:
            return 0
        return max(1, self._plan.estimated_duration_ms)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#111722"))

        panel_rect = self.rect().adjusted(10, 10, -10, -10)
        painter.setPen(QPen(QColor("#2E3B50"), 1))
        painter.drawRoundedRect(panel_rect, 8, 8)
        self._segments.clear()

        if not self._plan or not self._plan.timeline:
            painter.setPen(QColor("#8894A7"))
            painter.drawText(panel_rect, Qt.AlignCenter, "Timeline will appear after analysis")
            return

        duration = self._duration_ms()
        inner = panel_rect.adjusted(14, 20, -14, -20)
        timeline_h = 80
        base_y = inner.y() + 20
        rain_h = 22

        if self._has_rain:
            rain_rect = QRectF(inner.x(), base_y - rain_h - 8, inner.width(), rain_h)
            painter.fillRect(rain_rect, QColor("#2A4A6F"))
            painter.setPen(QColor("#8AB0D8"))
            painter.drawText(rain_rect, Qt.AlignCenter, "Rain Layer")

        for idx, entry in enumerate(self._plan.timeline):
            x0 = inner.x() + (entry.start_time_ms / duration) * inner.width()
            x1 = inner.x() + (entry.end_time_ms / duration) * inner.width()
            rect = QRectF(x0, base_y, max(2.0, x1 - x0), timeline_h)
            hue = 190 + ((idx * 23) % 40)
            color = QColor.fromHsl(hue, 120, 90)
            painter.fillRect(rect, color)
            painter.setPen(QColor("#131A23"))
            painter.drawRect(rect)
            self._segments.append(
                _Segment(rect=rect, track_name=entry.filename, start_ms=entry.start_time_ms, end_ms=entry.end_time_ms)
            )

        painter.setPen(QColor("#E0E7EF"))
        for idx, transition in enumerate(self._plan.transitions):
            left = self._plan.timeline[idx]
            right = self._plan.timeline[idx + 1]
            cross_start = right.start_time_ms
            cross_end = cross_start + transition.crossfade_ms
            x0 = inner.x() + (cross_start / duration) * inner.width()
            x1 = inner.x() + (cross_end / duration) * inner.width()
            cf_rect = QRectF(x0, base_y, max(1.5, x1 - x0), timeline_h)
            painter.fillRect(cf_rect, QColor(255, 170, 60, 110))

        painter.setPen(QColor("#A8B6C8"))
        painter.drawText(inner.x(), inner.bottom() + 24, "00:00:00")
        painter.drawText(inner.right() - 80, inner.bottom() + 24, format_hms(duration))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        tip = None
        for segment in self._segments:
            if segment.rect.contains(pos):
                tip = (
                    f"{segment.track_name}\n"
                    f"Start: {format_hms(segment.start_ms)}\n"
                    f"End: {format_hms(segment.end_ms)}"
                )
                break
        if tip:
            QToolTip.showText(self.mapToGlobal(QPoint(pos.x(), pos.y())), tip, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

