import os

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from streetjev.infrastructure.capture import CaptureTarget, window_rectangle


class RegionOverlay(QWidget):
    selected = Signal(object)
    cancelled = Signal()

    def __init__(self, screen, physical):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.physical = physical
        self.start = None
        self.end = QPoint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(6, 15, 25, 145))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(30, 45, "Выделите область на этом мониторе · Esc — отмена")
        if self.start is not None:
            rect = QRect(self.start, self.end).normalized()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor("#52e2ba"), 2))
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = event.position().toPoint()
            self.end = self.start
            self.update()

    def mouseMoveEvent(self, event):
        self.end = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event):
        if self.start is None or event.button() != Qt.MouseButton.LeftButton:
            return
        rect = QRect(self.start, event.position().toPoint()).normalized().intersected(self.rect())
        x, y, w, h = window_rectangle(int(self.winId())) if os.name == "nt" else self.physical
        sx, sy = w / self.width(), h / self.height()
        result = (
            x + round(rect.x() * sx),
            y + round(rect.y() * sy),
            round(rect.width() * sx),
            round(rect.height() * sy),
        )
        if min(result[2:]) >= 32:
            self.selected.emit(CaptureTarget(f"Область {result[2]} × {result[3]}", rectangle=result))
        else:
            self.cancelled.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
