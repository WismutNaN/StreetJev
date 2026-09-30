"""Pan/zoom настоящих полигонов, включая сложные береговые линии. Без сети и GPU."""

import argparse
import json
import os
import sqlite3
import statistics
import time
from dataclasses import fields
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPen, QTransform
from PySide6.QtWidgets import QApplication, QGraphicsView

from streetjev.domain.localization import LocalPoint
from streetjev.domain.round import RoundSnapshot
from streetjev.infrastructure.catalog import load_geography
from streetjev.presentation.map_geometry import project
from streetjev.presentation.map_view import CountryOutline, MapView


def load_snapshot(database, round_id=None):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        for (raw,) in connection.execute(
            "SELECT snapshot FROM rounds WHERE (? IS NULL OR id=?) ORDER BY started_at DESC",
            (round_id, round_id),
        ):
            data = json.loads(raw)
            if data["frames"] and data.get("points"):
                break
        else:
            raise ValueError("Нет завершённых наблюдений с точками")
    data = {key: value for key, value in data.items() if key in {f.name for f in fields(RoundSnapshot)}}
    for key in ("points", "last_points"):
        data[key] = tuple(LocalPoint(**point) for point in data.get(key, []))
    for key in ("observed_regions", "last_observed_regions"):
        data[key] = frozenset(data.get(key, []))
    data["cells"] = data["last_cells"] = ()
    return RoundSnapshot(**data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/rounds.sqlite3"))
    parser.add_argument("--round-id")
    parser.add_argument("--compare", action="store_true", help="Сравнить с прежними обводками на тех же данных")
    parser.add_argument("--output", type=Path, default=Path("artifacts/map-navigation.json"))
    args = parser.parse_args()
    app = QApplication([])
    catalog, geography = load_geography()
    view = MapView(catalog, geography)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    view.resize(1600, 900)
    view.show()
    view.auto_fit = False
    view.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
    snapshot = load_snapshot(args.database, args.round_id)
    view.update_snapshot(snapshot)
    timings = []
    by_country = {}
    codes = {id(item): code for code, item in view.country_outlines.items()}
    original_paint = CountryOutline.paint
    selected_paint = original_paint

    def legacy_paint(item, painter, option, widget=None):
        # Только для воспроизводимого сравнения. В приложении этого пути больше нет.
        painter.setClipPath(item.path(), Qt.ClipOperation.IntersectClip)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for color, width in ((QColor(16, 30, 41, 180), item.width + 2), (item.color, item.width)):
            pen = QPen(color, width)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(item.path())

    def outline_paint(item, *args):
        started = time.perf_counter()
        selected_paint(item, *args)
        duration = (time.perf_counter() - started) * 1000
        timings.append(duration)
        code = codes[id(item)]
        by_country[code] = by_country.get(code, 0) + duration

    CountryOutline.paint = outline_paint
    results = []
    for name, bounds in (
        ("world", (-180, -145, 360, 275)),
        ("scandinavia", (0, -105, 45, 45)),
        ("country", (4, -89, 12, 15)),
        ("city", (*project(10.74, 59.92), 0.15, 0.1)),
    ):
        for style in ("optimized", "legacy") if args.compare else ("optimized",):
            selected_paint = legacy_paint if style == "legacy" else original_paint
            for code, item in view.country_outlines.items():
                item.setVisible(snapshot.countries.get(code, 0) > 0)
            print(name, style, flush=True)
            view.setTransform(QTransform())
            view.fitInView(QRectF(*bounds), Qt.AspectRatioMode.KeepAspectRatio)
            app.processEvents()
            pan, zoom, outline = [], [], []
            by_country.clear()
            for action, samples in (("pan", pan), ("zoom", zoom)):
                for i in range(5):
                    timings.clear()
                    started = time.perf_counter()
                    if action == "pan":
                        view.horizontalScrollBar().setValue(view.horizontalScrollBar().value() + 8)
                    else:
                        center = view.mapToScene(view.viewport().rect().center())
                        factor = 1.15 if i % 2 == 0 else 1 / 1.15
                        view.scale(factor, factor)
                        view.centerOn(center)
                    # Одна запланированная перерисовка, как при реальном вводе.
                    view.viewport().update()
                    app.processEvents()
                    samples.append((time.perf_counter() - started) * 1000)
                    outline.append(sum(timings))
            result = {
                "name": name,
                "style": style,
                "points": len(snapshot.points),
                "groups": len(view.local_overlay.clusters),
                "pan_median_ms": round(statistics.median(pan), 2),
                "zoom_median_ms": round(statistics.median(zoom), 2),
                "max_ms": round(max(pan + zoom), 2),
                "outlines_median_ms": round(statistics.median(outline), 2),
                "expensive_outlines": sorted(by_country.items(), key=lambda item: item[1], reverse=True)[:3],
            }
            results.append(result)
            print(result, flush=True)
    CountryOutline.paint = original_paint
    dense = []
    overlay = view.local_overlay
    overlay.set_points(
        tuple(
            LocalPoint(str(i), 59.9 + (i // 60) * 0.03, 10 + (i % 60) * 0.03, "NO", None, 0.4 / 1800)
            for i in range(1800)
        ),
        "relative",
    )
    for scale in (450, 1500, 5000, 50000):
        view.setTransform(QTransform.fromScale(scale, scale))
        view.centerOn(*project(10.9, 60.35))
        values = []
        for _ in range(6):
            view.horizontalScrollBar().setValue(view.horizontalScrollBar().value() + 4)
            started = time.perf_counter()
            overlay._render()
            values.append((time.perf_counter() - started) * 1000)
        if scale == 1500:
            # Предыдущий smoke проверял всего 20 групп; здесь более тысячи крупных кругов.
            assert len(overlay.clusters) == 1800 and len(overlay.visible_markers) > 1000
        result = {
            "scale": scale,
            "points": 1800,
            "groups": len(overlay.clusters),
            "visible": len(overlay.visible_markers),
            "first_ms": round(values[0], 2),
            "pan_median_ms": round(statistics.median(values[1:]), 2),
        }
        dense.append(result)
        print("dense", result, flush=True)
    view.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "round_id": snapshot.id,
                "device_pixel_ratio": view.devicePixelRatioF(),
                "navigation": results,
                "dense": dense,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
