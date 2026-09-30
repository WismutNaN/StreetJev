"""Асинхронная подложка для существующей QGraphics-карты."""

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel

from streetjev.infrastructure.map_tiles import DEFAULT_TILE_URL, TileClient, visible_tiles


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
                self.view.transform().m11() * self.view.devicePixelRatioF(),
            )
            if self.enabled and self.view.isVisible()
            else []
        )
        self.wanted = keys
        for key in list(self.items):
            if key not in keys:
                self.view.scene().removeItem(self.items.pop(key))
        self.credit.setVisible(bool(keys))
        self.view.set_detail_active(bool(self.items))
        self.client.select(keys)

    def received(self, key, image):
        if not self.enabled or key not in self.wanted:
            return
        if key not in self.items:
            item = self.view.scene().addPixmap(QPixmap.fromImage(image))
            zoom, x, y = key
            size = 360 / 2**zoom
            item.setScale(size / 256)
            item.setPos(-180 + x * size, -180 + y * size)
            item.setZValue(-10)
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.items[key] = item
        self.view.set_detail_active(True)
        self.credit.setText(self.credit_text)
        self.position_credit()
        self.credit.setToolTip("Города, дороги и названия · OpenStreetMap")

    def failed(self, message):
        self.credit.setText(self.credit_text + " · недоступно")
        self.position_credit()
        self.credit.setToolTip(message + "\nГеографические оценки продолжают работать")

    def close(self):
        self.timer.stop()
        self.client.close()
