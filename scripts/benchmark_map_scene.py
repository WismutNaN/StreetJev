"""Полная карта: сохранённый раунд, локальные тайлы и ADM2, реальные drag/wheel, без сети."""

import argparse
import gzip
import json
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from benchmark_map_navigation import load_snapshot
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QTransform, QWheelEvent
from PySide6.QtNetwork import QNetworkDiskCache
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsView

from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.subregions import SubregionRepository
from streetjev.infrastructure.vector_cache import VectorCache
from streetjev.presentation.local_overlay import LocalOverlay
from streetjev.presentation.map_geometry import project, unproject
from streetjev.presentation.map_view import CountryOutline, MapView
from streetjev.presentation.subregion_layer import SubregionLayer
from streetjev.presentation.tile_layer import RoadTile, TileLayer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/rounds.sqlite3"))
    parser.add_argument("--round-id", help="По умолчанию последний раунд с точками Chipoint")
    parser.add_argument("--output", type=Path, default=Path("artifacts/scene-performance.json"))
    parser.add_argument("--case", default="all")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--width", type=int, default=2560)
    parser.add_argument("--height", type=int, default=1400)
    parser.add_argument("--tile-fixture", type=Path, default=Path("artifacts/replay-tiles.json"))
    args = parser.parse_args()
    app = QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "seguisym.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    catalog, geography = load_geography()
    snapshot = load_snapshot(args.database, args.round_id)
    view = MapView(catalog, geography)
    view.resize(args.width, args.height)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    view.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
    view.auto_fit = False
    view.show()
    subregions = SubregionLayer(view, Path("data/boundaries"), geography)
    datasets = {}
    for root, countries in ((Path("data/boundaries"), ("RO", "BG", "MY", "RU", "BR")),):
        repo = SubregionRepository(root, geography)
        for country in countries:
            source = root / f"{repo.iso3[country]}-ADM2.json.gz"
            prepared = repo.read_prepared(country, source)
            if prepared:
                datasets[country] = prepared[0]
    subregions.datasets.update(datasets)
    subregions.repository.iso3 = {c: subregions.repository.iso3[c] for c in datasets}
    view.update_snapshot(snapshot)
    cache_root = Path("data/map-vector-cache")
    if args.tile_fixture.exists():
        saved_tiles = {
            tuple(k): (d, time.time() + 3600) for k, d in json.loads(args.tile_fixture.read_text(encoding="utf-8"))
        }
    else:
        cache = QNetworkDiskCache()
        cache.setCacheDirectory(str(cache_root))
        prepared = VectorCache(cache_root)
        saved_tiles = {}
        for file in (cache_root / "data8").rglob("*.d"):
            url = cache.fileMetaData(str(file)).url()
            if not url.path().endswith(".mvt"):
                continue
            # Срок годности намеренно игнорируется только в replay: это фиксированные fixtures,
            # которые позволяют сравнить рендерер без повторной загрузки и смены данных на сервере.
            try:
                with gzip.open(prepared.path(url.toString()), "rt", encoding="utf-8") as stream:
                    details = json.load(stream)["details"]
                key = tuple(int(x) for x in url.path().removesuffix(".mvt").split("/")[-3:])
                saved_tiles[key] = details, time.time() + 3600
            except (OSError, ValueError, KeyError):
                continue
        args.tile_fixture.parent.mkdir(parents=True, exist_ok=True)
        args.tile_fixture.write_text(json.dumps([(k, d) for k, (d, _) in saved_tiles.items()]), encoding="utf-8")
    tiles = TileLayer(view, cache_root)
    # Готовые данные берутся из кэша; _pump отключён, поэтому сети и фонового декодирования нет.
    tiles.client._pump = lambda: None
    tiles.enabled = True
    metrics = defaultdict(list)
    originals = []

    def instrument(cls, method, name):
        original = getattr(cls, method)
        originals.append((cls, method, original))

        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                metrics[name].append((time.perf_counter() - start) * 1000)

        setattr(cls, method, timed)

    for cls, method, name in (
        (RoadTile, "paint", "tiles"),
        (LocalOverlay, "paint", "points"),
        (CountryOutline, "paint", "country_outlines"),
        (MapView, "detail_label_layout", "labels"),
        (MapView, "drawForeground", "frame"),
        (SubregionLayer, "refresh", "adm2_refresh"),
    ):
        instrument(cls, method, name)
    results = []
    cases = [
        ("world", 20, 20, 8),
        ("balkans", 25, 44, 90),
        ("romania", 25, 45, 220),
        ("kuala_lumpur", 101.69, 3.14, 3000),
        ("brazil", -47.6, -22.5, 110),
        ("russia", 60.61, 56.84, 100),
    ]
    for name, min_zoom, max_zoom in (("cached_overview", 4, 8), ("cached_detail", 9, 14)):
        candidates = [k for k in saved_tiles if min_zoom <= k[0] <= max_zoom]
        if not candidates:
            continue

        def cost(key):
            z, x, y = key
            return sum(
                len(saved_tiles[k][0]["roads"]) + 8 * len(saved_tiles[k][0].get("water", ()))
                for k in saved_tiles
                if k[0] == z and abs(k[1] - x) <= 2 and abs(k[2] - y) <= 1
            )

        z, x, y = max(candidates, key=cost)
        size = 360 / 2**z
        lon, lat = unproject(-180 + (x + 0.5) * size, -180 + (y + 0.5) * size)
        cases.append((name, lon, lat, 512 / size * 0.8))
    try:
        for name, lon, lat, scale in cases:
            if args.case not in ("all", name):
                continue
            view.setTransform(QTransform.fromScale(scale, scale))
            view.centerOn(*project(lon, lat))
            for item in tiles.items.values():
                view.scene().removeItem(item)
            tiles.items.clear()
            tiles.client.memory.update(saved_tiles)
            start = time.perf_counter()
            subregions.refresh()
            boundaries_ms = (time.perf_counter() - start) * 1000
            tiles.refresh()
            tiles_ms = (time.perf_counter() - start) * 1000 - boundaries_ms
            app.processEvents()
            refresh_ms = (time.perf_counter() - start) * 1000
            tiles.timer.stop()
            subregions.timer.stop()
            tiles.update_labels()
            tiles.labels_timer.stop()
            app.processEvents()
            start = time.perf_counter()
            center = view.viewport().rect().center()
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
            press_ms = (time.perf_counter() - start) * 1000
            pan, wheel, events = [], [], defaultdict(float)
            frame_count = 0
            for action, values in (("pan", pan), ("wheel", wheel)):
                for i in range(10):
                    metrics.clear()
                    start = time.perf_counter()
                    before = view.viewportTransform()
                    if action == "pan":
                        QTest.mouseMove(view.viewport(), center + QPoint(6 * (i + 1), 3 * (i + 1)), delay=0)
                    else:
                        event = QWheelEvent(
                            QPointF(center),
                            QPointF(view.viewport().mapToGlobal(center)),
                            QPoint(),
                            QPoint(0, 120 if i % 2 == 0 else -120),
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase,
                            False,
                        )
                        app.sendEvent(view.viewport(), event)
                    tiles.timer.stop()
                    tiles.labels_timer.stop()
                    subregions.timer.stop()
                    app.processEvents()
                    assert view.viewportTransform() != before, (name, action, i)
                    frame_count += len(metrics["frame"])
                    values.append((time.perf_counter() - start) * 1000)
                    for key, durations in metrics.items():
                        events[key] += sum(durations)
                if action == "pan":
                    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=center + QPoint(60, 30))
            entry = {
                "name": name,
                "tiles": len(tiles.items),
                "subregions": len(subregions.items),
                "paint_count": frame_count,
                "labels": len(view.detail_labels),
                "press_ms": round(press_ms, 2),
                "refresh_ms": round(refresh_ms, 2),
                "boundaries_ms": round(boundaries_ms, 2),
                "tiles_ms": round(tiles_ms, 2),
                "pan_ms": round(statistics.median(pan), 2),
                "wheel_ms": round(statistics.median(wheel), 2),
                "max_ms": round(max(pan + wheel), 2),
                "component_mean_ms": {k: round(v / 20, 2) for k, v in events.items()},
            }
            start = time.perf_counter()
            if hasattr(view, "navigation_timer"):
                view.navigation_timer.stop()
                view.finish_navigation()
            app.processEvents()
            entry["settle_ms"] = round((time.perf_counter() - start) * 1000, 2)
            if args.screenshots:
                target = args.output.parent / (args.output.stem + "-images")
                target.mkdir(parents=True, exist_ok=True)
                view.grab().save(str(target / (name + ".png")))
            results.append(entry)
            print(entry, flush=True)
        args.output.parent.mkdir(exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "round_id": snapshot.id,
                    "dpr": view.devicePixelRatioF(),
                    "cached_tiles": len(saved_tiles),
                    "datasets": list(datasets),
                    "results": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        for cls, name, original in originals:
            setattr(cls, name, original)
        subregions.close()
        tiles.close()
        view.close()


if __name__ == "__main__":
    main()
