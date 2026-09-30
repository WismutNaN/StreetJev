"""Профиль самых крупных уже загруженных MVT: без сети и изменений пользовательского кэша."""

import json
import os
import statistics
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import mapbox_vector_tile
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtNetwork import QNetworkDiskCache
from PySide6.QtWidgets import QApplication

from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.vector_details import decode_details
from streetjev.presentation.map_view import MapView
from streetjev.presentation.tile_layer import RoadTile


def timed(call, repeats=5):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = call()
        values.append((time.perf_counter() - start) * 1000)
    return result, round(statistics.median(values), 2)


def main():
    app = QApplication([])
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
    root = Path("data/map-vector-cache")
    cache = QNetworkDiskCache()
    # fileMetaData/data только читают ранее скачанные файлы; сетевого клиента здесь нет.
    cache.setCacheDirectory(str(root))
    available = sorted((root / "data8").rglob("*.d"), key=lambda path: path.stat().st_size, reverse=True)
    closeups = [path for path in available if int(cache.fileMetaData(str(path)).url().path().split("/")[-3]) >= 10]
    files = list(dict.fromkeys(available[:3] + closeups[:3]))
    catalog, geography = load_geography()
    view = MapView(catalog, geography)
    view.resize(1000, 700)
    view.show()
    results = []
    for file in files:
        metadata = cache.fileMetaData(str(file))
        url = metadata.url()
        device = cache.data(url)
        if device is None or not url.path().endswith(".mvt"):
            continue
        content = bytes(device.readAll())
        device.close()
        key = tuple(int(value) for value in url.path().removesuffix(".mvt").split("/")[-3:])
        full, decode_ms = timed(
            lambda content=content: mapbox_vector_tile.decode(content, default_options={"y_coord_down": True})
        )
        details, filtered_ms = timed(lambda content=content, key=key: decode_details(content, key[0]))
        tile, construct_ms = timed(lambda key=key, details=details: RoadTile(key, details))
        view.scene().addItem(tile)
        view.set_detail_labels(tile.labels)
        view.auto_fit = False
        view.fitInView(tile.boundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        app.processEvents()
        target = QImage(view.size(), QImage.Format.Format_ARGB32_Premultiplied)

        def render(target=target):
            target.fill(Qt.GlobalColor.transparent)
            painter = QPainter(target)
            view.render(painter)
            painter.setFont(QFont("Segoe UI", 9))
            painter.setPen(QColor("#a5b3bc"))
            painter.drawText(
                target.rect().adjusted(6, 6, -6, -6),
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
                "© OpenStreetMap contributors",
            )
            painter.end()

        _, render_ms = timed(render, 12)
        if file in closeups[:1]:
            image_path = Path("artifacts/ui/vector-cached-city.png")
            image_path.parent.mkdir(parents=True, exist_ok=True)
            target.save(str(image_path))
        view.scene().removeItem(tile)
        results.append(
            {
                "tile": key,
                "bytes": len(content),
                "original_street_features": len(full.get("streets", {}).get("features", [])),
                "rendered_roads": len(details["roads"]),
                "vertices": sum(len(points) for _, points in details["roads"]),
                "labels": len(details["labels"]),
                "full_mvt_decode_ms": decode_ms,
                "filtered_decode_ms": filtered_ms,
                "gui_tile_creation_ms": construct_ms,
                "view_render_ms": render_ms,
            }
        )
    output = Path("artifacts/map-performance.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output.read_text(encoding="utf-8"))
    view.close()


if __name__ == "__main__":
    main()
