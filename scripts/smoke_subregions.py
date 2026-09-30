"""ADM2: фоновая загрузка, рейтинг, карта, сброс и отсутствие выдуманной поддержки. Без сети."""

import argparse
import os
import statistics
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from shapely.geometry import box, mapping

from streetjev.application.session import AnalysisResult
from streetjev.domain.evidence import Distribution, GeoEvidence
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.storage import SqliteRoundRepository
from streetjev.infrastructure.subregions import SubregionDataset, SubregionRepository
from streetjev.presentation.style import STYLE
from streetjev.presentation.window import MainWindow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", choices=("AT", "RU"), default="AT")
    args = parser.parse_args()
    country = args.country
    iso, region, latitude, longitude = (
        ("AUT", "AT-7", 47.2692, 11.4041) if country == "AT" else ("RUS", "RU-SVE", 56.84, 60.61)
    )
    app = QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "seguisym.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    app.setStyleSheet(STYLE)
    catalog, geography = load_geography()
    cached = Path(f".research/boundaries/{iso}-ADM2.json.gz")
    if not cached.exists():
        if country == "RU":
            raise SystemExit("Для стресс-проверки RU нужен существующий .research/boundaries/RUS-ADM2.json.gz")
        metadata = {"boundaryISO": "AUT", "boundaryType": "ADM2", "boundaryLicense": "test fixture"}
        data = {
            "features": [
                {
                    "properties": {"shapeID": "test", "shapeName": "Innsbruck fixture", "shapeType": "ADM2"},
                    "geometry": mapping(box(11.2, 47.1, 11.6, 47.4)),
                }
            ]
        }

    def pump(condition):
        limit = time.monotonic() + 30
        while not condition():
            app.processEvents()
            time.sleep(0.005)
            assert time.monotonic() < limit, "ADM2/UI timeout"

    with TemporaryDirectory() as directory:
        root = Path(directory)
        window = MainWindow(catalog, geography, SqliteRoundRepository(root / "rounds.sqlite3"), root, demo=True)
        try:
            window.resize(1200, 760)
            window.show()
            window.subregions.enabled = True
            window.subregions.repository.iso3 = {country: iso}
            calls = []

            def load(country):
                calls.append(country)
                time.sleep(0.25)
                if cached.exists():

                    def offline(url):
                        raise OSError("Smoke: network disabled")

                    return SubregionRepository(cached.parent, geography, fetch=offline).load(country)
                return SubregionDataset(country, metadata, data, geography)

            window.subregions.repository.load = load
            countries = dict.fromkeys(catalog.countries, 0)
            countries.update({country: 0.8, "DE": 0.2})
            regions = {r.code: float(r.code == region) for r in catalog.regions_of(country)}
            primary = GeoEvidence(
                "imajev", "fixture", Distribution(countries), {country: Distribution(regions)}, "{}", 0
            )
            result = AnalysisResult(window.session.round.id, window.demo_expert.frame(), primary, 1)
            window.session.accept(result)
            payload = {
                "schema": "streetjev.chipoint.v1",
                "mode": "street_retrieval",
                "model": "fixture",
                "candidates": [
                    {"id": "a", "lat": latitude, "lon": longitude, "score": 8},
                    {"id": "b", "lat": latitude + 0.06, "lon": longitude - 0.01, "score": 7},
                ],
            }
            window.session.supplement(
                replace(result, evidence=ChipointExpert(catalog, geography).convert(payload, 0))
            )
            before = window.session.round.snapshot()
            ticks = []
            timer = QTimer()
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            timer.start(10)
            window.update_snapshot()
            window.auto_focus.setChecked(False)
            window.map.focus_country(country)
            if country == "RU":
                window.map.focus_point(window.map.local_overlay.points[0])
            pump(lambda: bool(window.subregions.support) and bool(window.subregions.items))
            timer.stop()
            assert len(ticks) > 10, "Подготовка ADM2 не должна блокировать цикл событий"
            assert calls == [country]
            dataset = window.subregions.datasets[country]
            assert window.session.round.snapshot() == before
            assert sum(window.subregions.support.values()) <= before.countries[country]
            rows = [
                i
                for i in range(window.ranking.rowCount())
                if window.ranking.item(i, 0).data(Qt.ItemDataRole.UserRole)[0] == "subregion"
            ]
            assert rows
            window.focus_ranked(rows[0], 0)
            pump(lambda: window.map.transform().m11() > 200)
            window.subregions.refresh()
            app.processEvents()
            output = Path("artifacts/ui")
            output.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(output / f"subregions-{country.lower()}.png"))
            timings = []
            for step in range(16):
                start = time.perf_counter()
                window.map.horizontalScrollBar().setValue(window.map.horizontalScrollBar().value() + 4)
                window.map.viewport().update()
                app.processEvents()
                timings.append((time.perf_counter() - start) * 1000)
            assert window.session.round.snapshot() == before
            window.reset_round()
            assert not window.subregions.support
            assert all(item.brush().color().alpha() == 0 for item in window.subregions.items.values())
            # Отложенный ответ старого запроса — только геометрия; не возвращает оценки сброшенного раунда.
            window.subregions.received(country, dataset, "")
            assert not window.subregions.support
            assert window.ranking.rowCount() == 0
            print(
                "ADM2 smoke OK",
                {
                    "source": dataset.metadata.get("boundaryID", "fixture"),
                    "ui_ticks": len(ticks),
                    "max_timer_gap_ms": round(max((b - a) * 1000 for a, b in pairwise(ticks)), 2),
                    "pan_median_ms": round(statistics.median(timings), 2),
                    "pan_max_ms": round(max(timings), 2),
                },
            )
        finally:
            window.close()


if __name__ == "__main__":
    main()
