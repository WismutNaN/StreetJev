"""Тайлы только текущего вида: HTTP-кэш Qt, четыре запроса, без предварительной загрузки."""

import math
import time
from collections import OrderedDict

from google.protobuf.message import DecodeError
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest

from streetjev.infrastructure.vector_cache import VectorCache, expires_at
from streetjev.infrastructure.vector_details import decode_details

DEFAULT_TILE_URL = "https://vector.openstreetmap.org/shortbread_v1/{z}/{x}/{y}.mvt"


def visible_tiles(bounds, pixels_per_degree):
    left, top, right, bottom = bounds
    zoom = max(0, min(14, math.ceil(math.log2(max(1, 360 * pixels_per_degree / 512)))))
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


class DecodeJob(QRunnable):
    def __init__(self, content, url, key, expiry, cache, signal):
        super().__init__()
        self.content, self.url, self.key, self.expiry, self.cache, self.signal = (
            content,
            url,
            key,
            expiry,
            cache,
            signal,
        )

    def run(self):
        try:
            if self.content is None:
                saved = self.cache.read(self.url)
                details, self.expiry = saved if saved else (None, 0)
            else:
                details = decode_details(self.content, self.key[0])
                self.cache.write(self.url, details, self.expiry)
            message = ""
        except (DecodeError, ValueError, KeyError, TypeError, IndexError, AssertionError):
            details, message = None, "Сервис карты вернул неизвестный формат"
        try:
            self.signal.emit(self.key, details, self.expiry, message)
        except RuntimeError:
            pass  # Окно уже уничтожено.


class TileClient(QObject):
    ready = Signal(object, object)
    failed = Signal(str)
    decoded = Signal(object, object, float, str)

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
        self.prepared = VectorCache(cache_dir)
        self.decoding = set()
        self.cache_checked = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.decoded.connect(self._decoded)
        self.closed = False

    def select(self, keys):
        self.wanted = list(dict.fromkeys(keys))
        for key, reply in list(self.pending.items()):
            if key not in self.wanted:
                reply.abort()
        for key in self.wanted:
            if key in self.memory and self.memory[key][1] <= time.time():
                self.memory.pop(key)
                self.cache_checked.discard(key)
            if key in self.memory:
                self.memory.move_to_end(key)
                self.ready.emit(key, self.memory[key][0])
        while len(self.memory) > 128:
            self.memory.popitem(last=False)
        self._pump()

    def _pump(self):
        if self.closed:
            return
        for key in self.wanted:
            if len(self.pending) + len(self.decoding) >= 4:
                break
            if (
                key in self.pending
                or key in self.decoding
                or key in self.memory
                or self.retries.get(key, 0) > time.monotonic()
            ):
                continue
            zoom, x, y = key
            url = self.template.format(z=zoom, x=x, y=y)
            if key not in self.cache_checked:
                self.decoding.add(key)
                self.pool.start(DecodeJob(None, url, key, 0, self.prepared, self.decoded))
                continue
            request = QNetworkRequest(QUrl(url))
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
            if len(content) > 8_000_000:
                self.retries[key] = time.monotonic() + 60
                self.failed.emit("Слишком большой векторный тайл")
            else:
                expiry = expires_at(
                    {bytes(k).decode().lower(): bytes(v).decode() for k, v in reply.rawHeaderPairs()}
                )
                self.decoding.add(key)
                self.pool.start(
                    DecodeJob(content, reply.url().toString(), key, expiry, self.prepared, self.decoded)
                )
        elif reply.error() != QNetworkReply.NetworkError.OperationCanceledError:
            self.retries[key] = time.monotonic() + 60
            self.failed.emit("Подложка недоступна: " + reply.errorString())
        reply.deleteLater()
        self._pump()

    def _decoded(self, key, details, expiry, message):
        self.decoding.discard(key)
        if self.closed:
            return
        if details is None:
            self.cache_checked.add(key)
            if message:
                self.retries[key] = time.monotonic() + 60
                self.failed.emit(message)
        else:
            self.memory[key] = (details, expiry)
            while len(self.memory) > 128:
                self.memory.popitem(last=False)
            if key in self.wanted:
                self.ready.emit(key, details)
        self._pump()

    def close(self):
        self.closed = True
        self.pool.clear()
        self.wanted = []
        for reply in list(self.pending.values()):
            reply.abort()
