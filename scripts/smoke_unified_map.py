"""Единая карта: реальные полигоны, синтетические оценки, без сети и GPU."""

import json
import os
import statistics
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from streetjev.application.session import AnalysisResult
from streetjev.domain.evidence import Distribution, GeoEvidence
from streetjev.domain.localization import LocalPoint
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.storage import SqliteRoundRepository
from streetjev.presentation.map_geometry import project
from streetjev.presentation.style import STYLE
from streetjev.presentation.window import MainWindow


def main():
    app = QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "seguisym.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    app.setStyleSheet(STYLE)
    catalog, geography = load_geography()
    with TemporaryDirectory() as directory, ExitStack() as cleanup:
        root = Path(directory)
        repo = SqliteRoundRepository(root / "rounds.sqlite3")
        cleanup.callback(repo.close)
        window = MainWindow(catalog, geography, repo, root, demo=True)
        cleanup.callback(window.close)
        window.resize(1000, 600)
        window.show()
        app.processEvents()
        countries = dict.fromkeys(catalog.countries, 0)
        countries.update(DE=0.48, AT=0.31, CH=0.15, IT=0.06)
        regions = {}
        for country, weights in {
            "DE": {"DE-BY": 0.7, "DE-BW": 0.3},
            "AT": {"AT-7": 0.7, "AT-5": 0.2, "AT-8": 0.1},
            "CH": {"CH-GR": 0.6, "CH-SG": 0.3, "CH-ZH": 0.1},
        }.items():
            values = dict.fromkeys((r.code for r in catalog.regions_of(country)), 0)
            values.update(weights)
            regions[country] = Distribution(values)
        evidence = GeoEvidence("imajev", "fixture", Distribution(countries), regions, "{}", 0)
        result = AnalysisResult(window.session.round.id, window.demo_expert.frame(), evidence, 1)
        window.session.accept(result)
        payload = {
            "schema": "streetjev.chipoint.v1",
            "mode": "street_retrieval",
            "model": "fixture",
            "candidates": [
                {"id": "a", "lat": 47.2692, "lon": 11.4041, "score": 8},
                {"id": "b", "lat": 47.5702, "lon": 10.7041, "score": 7},
                {"id": "c", "lat": 46.8512, "lon": 9.5341, "score": 6},
            ],
        }
        window.session.supplement(replace(result, evidence=ChipointExpert(catalog, geography).convert(payload, 0)))
        window.update_snapshot()
        app.processEvents()
        before = window.session.round.snapshot()
        assert all(window.map.country_outlines[c].isVisible() for c in ("DE", "AT", "CH"))
        assert all(window.map.items_by_code[c].isVisible() for c in ("DE-BY", "AT-7", "CH-GR"))
        assert len(window.map.local_overlay.points) == 3
        assert {
            window.ranking.item(i, 0).data(Qt.ItemDataRole.UserRole)[0] for i in range(window.ranking.rowCount())
        } == {"country", "region", "point"}
        assert not hasattr(window, "level")
        output = Path("artifacts/ui")
        output.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(output / "unified-map.png"))
        point = window.map.local_overlay.points[0]
        window.map.focus_point(point)
        app.processEvents()
        overlay = window.map.local_overlay
        overlay._render()
        cached = overlay.image
        overlay._render()
        assert overlay.image is cached
        old_radius = max(radius for _, _, radius in overlay.visible_markers)
        window.local_radius.setValue(40)
        overlay._render()
        assert max(radius for _, _, radius in overlay.visible_markers) == old_radius * 2
        assert window.session.round.snapshot() == before
        pos = window.map.mapFromScene(QPointF(*project(point.longitude, point.latitude)))
        QTest.mouseClick(window.map.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        assert "Chipoint" in window.status.text()
        assert window.session.truth is None
        window.answer_button.click()
        app.processEvents()
        overlay._render()
        assert overlay.image is None
        QTest.mouseClick(window.map.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        assert abs(window.session.truth.latitude - point.latitude) < 0.001
        assert window.session.round.snapshot() == before
        window.color_scale.setCurrentIndex(1)
        assert window.map.country_scale.mode == overlay.mode == "absolute"
        window.color_scale.setCurrentIndex(0)
        # Два близких совпадения раскрываются при увеличении, поддержка не растёт.
        a = LocalPoint("stress0", 47.26921, 11.40417, "AT", "AT-7", 0.2)
        b = replace(a, id="stress1", longitude=11.40427, mass=0.1)
        overlay.set_points((a, b), "relative")
        window.map.focus_country("AT")
        overlay.prepare()
        assert len(overlay.clusters) == 1
        window.map.focus_cluster(overlay.clusters[0])
        overlay.prepare()
        assert len(overlay.clusters) == 2
        assert abs(sum(g.mass for g in overlay.clusters) - 0.3) < 1e-12
        # Перекрытие двух поисковых кругов не складывает их непрозрачность.
        b = replace(a, id="overlap", longitude=a.longitude + 0.25, mass=0.1)
        overlay.set_points((a, b), "relative")
        x, y = project(a.longitude + 0.125, a.latitude)
        window.map.fitInView(QRectF(x - 0.6, y - 0.45, 1.2, 0.9), Qt.AspectRatioMode.KeepAspectRatio)
        overlay._render()
        assert len(overlay.clusters) == 2
        sample = window.map.viewportTransform().map(QPointF(x, y + 0.1))
        assert all((center - sample).manhattanLength() > 16 for center, _, _ in overlay.visible_markers)
        assert all(
            (center.x() - sample.x()) ** 2 + (center.y() - sample.y()) ** 2 < (radius - 4) ** 2
            for center, _, radius in overlay.visible_markers
        )
        ratio = overlay.image.devicePixelRatio()
        assert 37 <= overlay.image.pixelColor(round(sample.x() * ratio), round(sample.y() * ratio)).alpha() <= 39
        # Повторный рендер и перемещение с 5000 совпадениями не повторяют подготовку на каждом кадре.
        points = tuple(
            LocalPoint(str(i), 47 + (i // 100) * 0.003, 10 + (i % 100) * 0.003, "AT", "AT-7", 0.3 / 5000)
            for i in range(5000)
        )
        overlay.set_points(points, "relative")
        x, y = project(10.15, 47.1)
        window.map.fitInView(QRectF(x - 0.5, y - 0.4, 1, 0.8), Qt.AspectRatioMode.KeepAspectRatio)
        timings = []
        for i in range(12):
            if i:
                window.map.horizontalScrollBar().setValue(window.map.horizontalScrollBar().value() + 3)
            started = time.perf_counter()
            window.map.viewport().grab()
            timings.append((time.perf_counter() - started) * 1000)
        assert len(overlay.clusters) < len(points)
        metrics = {
            "points": len(points),
            "groups": len(overlay.clusters),
            "first_render_ms": round(timings[0], 2),
            "pan_render_median_ms": round(statistics.median(timings[1:]), 2),
            "pan_render_max_ms": round(max(timings[1:]), 2),
        }
        Path("artifacts/unified-map-performance.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        window.session.clear_supplements()
        window.update_snapshot()
        assert not overlay.points
        window.reset_round()
        assert not overlay.points and not any(item.isVisible() for item in window.map.country_outlines.values())
        window.close()
        app.processEvents()
        assert "local_radius_km=40" in (root / "settings.ini").read_text(encoding="utf-8")
    print("Unified map smoke — OK", metrics)


if __name__ == "__main__":
    main()
