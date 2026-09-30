"""Асинхронная подложка для существующей QGraphics-карты."""

from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QLabel

from streetjev.infrastructure.map_tiles import DEFAULT_TILE_URL, TileClient, visible_tiles


class RoadTile(QGraphicsItem):
    """Дороги и вода без растительности, рельефа, зданий и POI."""

    def __init__(self, key, details):
        super().__init__()
        self.details = details
        zoom, x, y = key
        size = 360 / 2**zoom
        left, top = -180 + x * size, -180 + y * size
        self.rectangle = QRectF(left, top, size, size)
        self.paths = (QPainterPath(), QPainterPath())
        self.water, self.rivers = QPainterPath(), QPainterPath()
        self.water.setFillRule(Qt.FillRule.OddEvenFill)
        for polygon in details.get("water", ()):
            for ring in polygon:
                if not ring:
                    continue
                self.water.moveTo(left + ring[0][0] * size, top + ring[0][1] * size)
                for px, py in ring[1:]:
                    self.water.lineTo(left + px * size, top + py * size)
                self.water.closeSubpath()
        for line in details.get("rivers", ()):
            if not line:
                continue
            self.rivers.moveTo(left + line[0][0] * size, top + line[0][1] * size)
            for px, py in line[1:]:
                self.rivers.lineTo(left + px * size, top + py * size)
        for major, points in details["roads"]:
            path = self.paths[int(major)]
            path.moveTo(left + points[0][0] * size, top + points[0][1] * size)
            for px, py in points[1:]:
                path.lineTo(left + px * size, top + py * size)
        self.labels = [
            (left + x * size, top + y * size, name, population, "place")
            for x, y, name, population in details["labels"]
        ]
        self.labels.extend(
            (left + x * size, top + y * size, name, priority, kind)
            for x, y, name, priority, kind in details.get("landmarks", ())
        )
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(4)

    def boundingRect(self):
        return self.rectangle

    def shape(self):
        return QPainterPath()  # Выбор точки и региона проходит к исходной карте.

    def paint(self, painter, option, widget=None):
        painter.setClipRect(self.rectangle, Qt.ClipOperation.IntersectClip)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#243f50"))
        painter.drawPath(self.water)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(98, 156, 181, 150), 0.75)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(self.rivers)
        for major, path in enumerate(self.paths):
            # Ориентиры остаются второстепенными на любом цвете тепловой карты.
            color = QColor(135, 151, 160, 95 if major else 55)
            pen = QPen(color, 0.85 if major else 0.55)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(path)


class TileLayer(QObject):
    def __init__(self, view, cache_dir, template=DEFAULT_TILE_URL):
        super().__init__(view)
        self.view = view
        self.enabled = False
        self.items, self.wanted = {}, []
        self.client = TileClient(cache_dir, self, template)
        self.client.ready.connect(self.received)
        self.client.failed.connect(self.failed)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.labels_timer = QTimer(self)
        self.labels_timer.setSingleShot(True)
        self.labels_timer.setInterval(40)
        self.labels_timer.timeout.connect(self.update_labels)
        self.credit = QLabel(
            '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>', view.viewport()
        )
        self.credit.setOpenExternalLinks(True)
        self.credit.setStyleSheet(
            "QLabel { background: #0f1c28; color: #dce7ef; padding: 4px; font-size: 11px; } a { color: #65dab6; }"
        )
        self.credit.setText(
            '<a style="color:#65dab6" href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>'
        )
        self.credit.hide()
        self.credit_text = self.credit.text()
        view.viewport_changed.connect(self.schedule)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.schedule()
        if not enabled:
            self.refresh()

    def schedule(self):
        self.position_credit()
        self.timer.start()

    def position_credit(self):
        self.credit.adjustSize()
        self.credit.move(
            max(0, self.view.viewport().width() - self.credit.width() - 6),
            max(0, self.view.viewport().height() - self.credit.height() - 6),
        )
        self.credit.raise_()

    def refresh(self):
        rectangle = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        keys = (
            visible_tiles(
                (rectangle.left(), rectangle.top(), rectangle.right(), rectangle.bottom()),
                self.view.transform().m11(),  # Вектор остаётся чётким при любом DPI.
            )
            if self.enabled and self.view.isVisible()
            else []
        )
        self.wanted = keys
        for key in list(self.items):
            # Предыдущий масштаб остаётся видимым до прихода новых тайлов.
            if not keys or not rectangle.intersects(self.items[key].boundingRect()):
                self.view.scene().removeItem(self.items.pop(key))
            elif key not in keys:
                self.items[key].setOpacity(0.45)
            else:
                self.items[key].setOpacity(1)
        self.remove_fallbacks()
        self.credit.setVisible(bool(keys))
        self.view.set_detail_active(bool(self.items))
        self.update_labels()
        self.client.select(keys)

    def received(self, key, details):
        if not self.enabled or key not in self.wanted:
            return
        if key not in self.items or self.items[key].details is not details:
            if key in self.items:
                self.view.scene().removeItem(self.items.pop(key))
            item = RoadTile(key, details)
            self.view.scene().addItem(item)
            self.items[key] = item
        self.remove_fallbacks()
        self.labels_timer.start()
        self.view.set_detail_active(True)
        self.credit.setText(self.credit_text)
        self.position_credit()
        self.credit.setToolTip("Дороги, населённые пункты, реки и озёра · OpenStreetMap")

    def update_labels(self):
        unique = {
            (round(x, 7), round(y, 7), name): (x, y, name, population, kind)
            for key, item in sorted(self.items.items(), key=lambda entry: entry[0] in self.wanted)
            for x, y, name, population, kind in item.labels
        }
        self.view.set_detail_labels(sorted(unique.values(), key=lambda item: item[3], reverse=True))

    def remove_fallbacks(self):
        complete = all(key in self.items for key in self.wanted)
        old = [key for key in self.items if key not in self.wanted]
        for key in old if complete else old[:-96]:
            self.view.scene().removeItem(self.items.pop(key))

    def failed(self, message):
        self.credit.setText(self.credit_text + " · недоступно")
        self.position_credit()
        self.credit.setToolTip(message + "\nГеографические оценки продолжают работать")

    def close(self):
        self.timer.stop()
        self.labels_timer.stop()
        self.client.close()
