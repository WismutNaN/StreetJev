"""Подложка и зоны: локальный HTTP fixture, без запросов к публичному серверу тайлов."""

import os
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import mapbox_vector_tile
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from streetjev.application.session import AnalysisResult
from streetjev.domain.evidence import Distribution, GeoEvidence
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.map_tiles import TileClient
from streetjev.infrastructure.storage import SqliteRoundRepository
from streetjev.infrastructure.vector_details import decode_details
from streetjev.presentation.map_view import project
from streetjev.presentation.style import STYLE
from streetjev.presentation.window import MainWindow


def main():
    app = QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "seguisym.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setStyleSheet(STYLE)
    content = mapbox_vector_tile.encode(
        [
            {
                "name": "streets",
                "features": [
                    {"geometry": "LINESTRING(0 1200, 4096 3000)", "properties": {"kind": "primary"}},
                    {"geometry": "LINESTRING(2500 0, 1500 4096)", "properties": {"kind": "residential"}},
                ],
            },
            {
                "name": "place_labels",
                "features": [
                    {
                        "geometry": "POINT(1300 1300)",
                        "properties": {"name_ru": "Тестовый город", "kind": "city", "population": 10000},
                    }
                ],
            },
            {
                "name": "land",
                "features": [
                    {"geometry": "POLYGON((0 0,4096 0,4096 4096,0 4096,0 0))", "properties": {"kind": "forest"}}
                ],
            },
        ],
        default_options={"y_coord_down": True},
    )
    requests = []
    delay = threading.Event()
    delay.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            delay.wait(5)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.mapbox-vector-tile")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.send_header("ETag", '"fixture-v1"')
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    template = f"http://127.0.0.1:{server.server_port}/{{z}}/{{x}}/{{y}}.mvt"

    def pump(condition):
        deadline = time.monotonic() + 8
        while not condition() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert condition(), "Карта не завершила асинхронную операцию"

    try:
        with TemporaryDirectory() as directory:
            output = Path("artifacts/ui")
            output.mkdir(parents=True, exist_ok=True)
            path = Path(directory)
            catalog, geography = load_geography()
            repo = SqliteRoundRepository(path / "rounds.sqlite3")
            window = MainWindow(catalog, geography, repo, path, demo=True)
            window.tiles.client.template = template
            window.resize(1000, 600)
            window.show()
            sample = window.demo_expert.frame()
            primary = GeoEvidence("imajev", "test", Distribution(dict.fromkeys(catalog.countries, 1)), {}, "{}", 0)
            result = AnalysisResult(window.session.round.id, sample, primary, 1)
            window.session.accept(result)
            assert window.map.heat_scale.empty
            payload = {
                "schema": "streetjev.chipoint.v1",
                "mode": "street_retrieval",
                "model": "fixture",
                "candidates": [
                    {"id": "a", "lat": 47.2692, "lon": 11.4041, "score": 8},
                    {"id": "b", "lat": 47.2701, "lon": 11.405, "score": 7},
                    {"id": "c", "lat": 47.38, "lon": 11.62, "score": 6},
                ],
            }
            secondary = ChipointExpert(catalog, geography).convert(payload, 0)
            window.session.supplement(replace(result, evidence=secondary))
            window.update_snapshot()
            assert len(window.map.local_overlay.points) == 3
            assert any(
                window.ranking.item(row, 0).data(Qt.ItemDataRole.UserRole)[0] == "point"
                for row in range(window.ranking.rowCount())
            )
            assert not window.map.heat_scale.empty
            window.map.focus_point(window.map.local_overlay.points[0])
            assert window.map.transform().m11() > 500
            fills = {code: item.brush().color().rgba() for code, item in window.map.items_by_code.items()}
            local_points = window.map.local_overlay.points
            window.details.setChecked(True)
            pump(lambda: len(window.tiles.items) == len(window.tiles.wanted) and bool(window.map.detail_labels))
            assert window.map.detail_active
            assert window.map.detail_labels
            assert fills == {code: item.brush().color().rgba() for code, item in window.map.items_by_code.items()}
            assert window.map.local_overlay.points == local_points
            assert all(not hasattr(item, "pixmap") for item in window.tiles.items.values())
            assert window.tiles.credit.isVisible()
            assert window.width() == 1000
            assert all(headers["User-Agent"].startswith("StreetJev/") for _, headers in requests)
            assert all("no-cache" not in headers.get("Cache-Control", "") for _, headers in requests)
            snapshot = window.session.round.snapshot()
            count = len(requests)
            window.tiles.refresh()
            app.processEvents()
            assert len(requests) == count, "Повторный вид должен использовать кэш"
            window.grab().save(str(output / "local-zones-fixture.png"))
            # Новый клиент читает уже разобранные векторы без сети и повторного MVT decode.
            client = TileClient(path / "map-vector-cache", template=template)
            loaded = []
            client.ready.connect(lambda key, image: loaded.append(key))
            with patch(
                "streetjev.infrastructure.map_tiles.decode_details", side_effect=AssertionError("No decode")
            ):
                client.select([window.tiles.wanted[0]])
                pump(lambda: bool(loaded))
            assert len(requests) == count, "Тайлы должны переживать перезапуск клиента"
            client.close()
            # Уличный масштаб + точная отметка, без вмешательства тепловой заливки.
            x, y = project(11.4041, 47.2692)
            window.auto_focus.setChecked(False)
            delay.clear()
            window.map.fitInView(QRectF(x - 0.005, y - 0.004, 0.01, 0.008), Qt.AspectRatioMode.KeepAspectRatio)
            window.tiles.refresh()
            assert window.tiles.items, "Предыдущие дороги сохраняются во время загрузки нового масштаба"
            assert any(key not in window.tiles.wanted for key in window.tiles.items)
            delay.set()
            window.answer_button.click()
            pump(
                lambda: (
                    window.tiles.wanted
                    and window.tiles.wanted[0][0] == 14
                    and len(window.tiles.items) == len(window.tiles.wanted)
                )
            )
            point = window.map.mapFromScene(QPointF(x, y))
            QTest.mouseClick(window.map.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert abs(window.session.truth.latitude - 47.2692) < 0.0001
            assert abs(window.session.truth.longitude - 11.4041) < 0.0001
            assert window.session.round.snapshot() == snapshot
            layout = window.map.detail_label_layout()
            assert window.map.detail_label_layout() is layout, "Повторная отрисовка не перевёрстывает подписи"
            window.details.setChecked(False)
            assert not window.tiles.items and not window.map.detail_active
            assert not window.map.detail_labels
            assert window.session.round.snapshot() == snapshot
            window.session.clear_supplements()
            window.update_snapshot()
            assert not window.map.local_overlay.points and window.ranking.rowCount() > 0
            window.close()
            app.processEvents()
            # Сбой сети — сигнал без зависания и немедленных повторов.
            bad = TileClient(path / "bad-cache", template="http://127.0.0.1:1/{z}/{x}/{y}.mvt")
            failures = []
            bad.failed.connect(failures.append)
            bad.select([(0, 0, 0)])
            pump(lambda: bool(failures))
            bad.select([(0, 0, 0)])
            assert not bad.pending
            bad.close()
            # Медленный декодер не блокирует события UI; decode и диск — на worker threads.
            slow = TileClient(path / "slow-cache", template=template)
            loaded = []
            slow.ready.connect(lambda *_: loaded.append(True))

            def slow_decode(*args):
                time.sleep(0.25)
                return decode_details(*args)

            with patch("streetjev.infrastructure.map_tiles.decode_details", side_effect=slow_decode):
                slow.select([(14, 0, 0)])
                ticks = []
                from PySide6.QtCore import QTimer

                timer = QTimer()
                timer.setInterval(10)
                timer.timeout.connect(lambda: ticks.append(time.monotonic()))
                timer.start()
                pump(lambda: bool(loaded))
                timer.stop()
                assert len(ticks) >= 10, "Интерфейс должен обрабатывать события во время decode"
            slow.close()
    finally:
        delay.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print(
        "Map details smoke: viewport tiles, HTTP cache, attribution, zoom, zones, precise label, disable/offline — OK"
    )


if __name__ == "__main__":
    main()
