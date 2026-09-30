"""Асинхронная подложка для существующей QGraphics-карты."""

import math
from collections import OrderedDict

from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QLabel

from streetjev.infrastructure.map_tiles import DEFAULT_TILE_URL, TileClient, visible_tiles


class TileRasterCache:
    """Ограниченный кэш готовой подложки; исходная геометрия остаётся векторной."""

    def __init__(self, limit=64 * 1024 * 1024):
        self.limit, self.size = limit, 0
        self.images = OrderedDict()
        self.maximum_side = 1024

    def reserve(self, count):
        # На большом HiDPI-окне все видимые тайлы должны помещаться одновременно:
        # иначе LRU вытеснял бы их по кругу и растеризовал заново на каждом кадре.
        side = (self.limit / (4 * max(1, count))) ** 0.5
        self.maximum_side = max(128, min(1024, 2 ** math.floor(math.log2(side))))

    def get(self, key):
        image = self.images.get(key)
        if image is not None:
            self.images.move_to_end(key)
        return image

    def put(self, key, image):
        old = self.images.pop(key, None)
        self.size -= old.sizeInBytes() if old is not None else 0
        self.images[key] = image
        self.size += image.sizeInBytes()
        while self.size > self.limit:
            _, old = self.images.popitem(last=False)
            self.size -= old.sizeInBytes()


class RoadTile(QGraphicsItem):
    """Дороги и вода без растительности, рельефа, зданий и POI."""

    def __init__(self, key, details, rasters=None):
        super().__init__()
        self.details = details
        self.rasters = rasters if rasters is not None else TileRasterCache()
        self.raster_id = object()
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
        self.has_geometry = any(not p.isEmpty() for p in (self.water, self.rivers, *self.paths))
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
        if not self.has_geometry:
            return
        # У источника есть предельный zoom. Дальше нужны исходные векторы, иначе
        # увеличение последнего растра превратит второстепенные дороги в толстые полосы.
        if self.rectangle.width() * abs(painter.worldTransform().m11()) > 1024:
            self.draw_vectors(painter)
            return
        # Фиксированное разрешение уровня тайлов: pan/zoom повторно используют растр.
        # Собственный кэш ограничивает и размер тайла, и суммарную память слоя.
        pixels = min(self.rasters.maximum_side, round(512 * min(2, painter.device().devicePixelRatioF())))
        ratio = pixels / 512
        key = self.raster_id, pixels
        image = self.rasters.get(key)
        if image is None:
            image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            brush = QPainter(image)
            brush.setRenderHint(QPainter.RenderHint.Antialiasing)
            brush.scale(pixels / self.rectangle.width(), pixels / self.rectangle.height())
            brush.translate(-self.rectangle.left(), -self.rectangle.top())
            self.draw_vectors(brush, ratio)
            brush.end()
            self.rasters.put(key, image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rectangle, image)

    def draw_vectors(self, painter, ratio=1):
        painter.setClipRect(self.rectangle, Qt.ClipOperation.IntersectClip)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#243f50"))
        painter.drawPath(self.water)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(98, 156, 181, 150), 0.75 * ratio)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(self.rivers)
        for major, path in enumerate(self.paths):
            # Ориентиры остаются второстепенными на любом цвете тепловой карты.
            color = QColor(135, 151, 160, 95 if major else 55)
            pen = QPen(color, (0.85 if major else 0.55) * ratio)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(path)


class TileLayer(QObject):
    def __init__(self, view, cache_dir, template=DEFAULT_TILE_URL):
        super().__init__(view)
        self.view = view
        self.enabled = False
        self.items, self.wanted = {}, []
        self.rasters = TileRasterCache()
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
        self.rasters.reserve(max(len(keys), len(self.items)))
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
            item = RoadTile(key, details, self.rasters)
            self.view.scene().addItem(item)
            self.items[key] = item
        self.remove_fallbacks()
        self.rasters.reserve(max(len(self.wanted), len(self.items)))
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
