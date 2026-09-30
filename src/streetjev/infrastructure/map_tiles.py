"""Тайлы только текущего вида: HTTP-кэш Qt, четыре запроса, без предварительной загрузки."""

import math
import time
from collections import OrderedDict

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest

DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def visible_tiles(bounds, pixels_per_degree):
    left, top, right, bottom = bounds
    zoom = max(0, min(19, math.ceil(math.log2(max(1, 360 * pixels_per_degree / 256)))))
    if right < -180 or left > 180 or bottom < -180 or top > 180:
        return []
    while True:
        count, size = 2**zoom, 360 / 2**zoom
        x0, x1 = max(0, math.floor((left + 180) / size)), min(count - 1, math.floor((right + 180) / size))
        y0, y1 = max(0, math.floor((top + 180) / size)), min(count - 1, math.floor((bottom + 180) / size))
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= 96 or zoom == 0:
            break
        zoom -= 1
    keys = [(zoom, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    cx, cy = ((left + right) / 2 + 180) / size, ((top + bottom) / 2 + 180) / size
    return sorted(keys, key=lambda key: (key[1] + 0.5 - cx) ** 2 + (key[2] + 0.5 - cy) ** 2)


class TileClient(QObject):
    ready = Signal(object, QImage)
    failed = Signal(str)

    def __init__(self, cache_dir, parent=None, template=DEFAULT_TILE_URL):
        super().__init__(parent)
        self.template = template
        self.manager = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self.manager)
        cache.setCacheDirectory(str(cache_dir))
        cache.setMaximumCacheSize(256 * 1024 * 1024)
        self.manager.setCache(cache)
        self.wanted, self.pending, self.retries = [], {}, {}
        self.memory = OrderedDict()
        self.closed = False

    def select(self, keys):
        self.wanted = list(dict.fromkeys(keys))
        for key, reply in list(self.pending.items()):
            if key not in self.wanted:
                reply.abort()
        for key in self.wanted:
            if key in self.memory:
                self.memory.move_to_end(key)
                self.ready.emit(key, self.memory[key])
        self._pump()

    def _pump(self):
        if self.closed:
            return
        for key in self.wanted:
            if len(self.pending) >= 4:
                break
            if key in self.pending or key in self.memory or self.retries.get(key, 0) > time.monotonic():
                continue
            zoom, x, y = key
            request = QNetworkRequest(QUrl(self.template.format(z=zoom, x=x, y=y)))
            request.setRawHeader(b"User-Agent", b"StreetJev/0.1 (desktop geographic map)")
            request.setAttribute(
                QNetworkRequest.Attribute.CacheLoadControlAttribute, QNetworkRequest.CacheLoadControl.PreferCache
            )
            request.setTransferTimeout(8000)
            reply = self.manager.get(request)
            self.pending[key] = reply
            reply.finished.connect(lambda key=key, reply=reply: self._finished(key, reply))

    def _finished(self, key, reply):
        self.pending.pop(key, None)
        if reply.error() == QNetworkReply.NetworkError.NoError:
            content = bytes(reply.readAll())
            picture = QImage.fromData(content) if len(content) < 2_000_000 else QImage()
            if not picture.isNull() and picture.size().width() == 256 and picture.size().height() == 256:
                self.memory[key] = picture
                while len(self.memory) > 128:
                    self.memory.popitem(last=False)
                if key in self.wanted and not self.closed:
                    self.ready.emit(key, picture)
            else:
                self.retries[key] = time.monotonic() + 60
                self.failed.emit("Сервис карты вернул неизвестный формат")
        elif reply.error() != QNetworkReply.NetworkError.OperationCanceledError:
            self.retries[key] = time.monotonic() + 60
            self.failed.emit("Подложка недоступна: " + reply.errorString())
        reply.deleteLater()
        self._pump()

    def close(self):
        self.closed = True
        self.wanted = []
        for reply in list(self.pending.values()):
            reply.abort()
