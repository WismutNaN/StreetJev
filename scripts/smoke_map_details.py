"""Подложка и зоны: локальный HTTP fixture, без запросов к публичному серверу тайлов."""

import io
import os
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image, ImageDraw
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
    picture = Image.new("RGB", (256, 256), "#ede9dc")
    draw = ImageDraw.Draw(picture)
    draw.line([(0, 80), (256, 190)], fill="#f2be66", width=10)
    draw.line([(160, 0), (110, 256)], fill="#ffffff", width=7)
    draw.text((20, 20), "Map fixture / streets", fill="#303030")
    stream = io.BytesIO()
    picture.save(stream, format="PNG")
    content, requests = stream.getvalue(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.send_header("ETag", '"fixture-v1"')
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    template = f"http://127.0.0.1:{server.server_port}/{{z}}/{{x}}/{{y}}.png"

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
            window.level.setCurrentIndex(2)
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
            assert len(window.map.cell_items) == 2
            assert window.ranking.rowCount() == 2
            assert not window.map.heat_scale.empty
            window.focus_ranked(0, 0)
            assert window.map.transform().m11() > 500
            window.details.setChecked(True)
            pump(lambda: len(window.tiles.items) == len(window.tiles.wanted) and bool(window.tiles.items))
            assert window.map.detail_active
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
            # Проверка дискового HTTP-кэша новым клиентом (без memory cache).
            client = TileClient(path / "map-cache", template=template)
            loaded = []
            client.ready.connect(lambda key, image: loaded.append(key))
            client.select([window.tiles.wanted[0]])
            pump(lambda: bool(loaded))
            assert len(requests) == count, "Тайлы должны переживать перезапуск клиента"
            client.close()
            # Уличный масштаб + точная отметка, без вмешательства тепловой заливки.
            x, y = project(11.4041, 47.2692)
            window.auto_focus.setChecked(False)
            window.map.fitInView(QRectF(x - 0.005, y - 0.004, 0.01, 0.008), Qt.AspectRatioMode.KeepAspectRatio)
            window.answer_button.click()
            pump(
                lambda: (
                    window.tiles.wanted
                    and window.tiles.wanted[0][0] >= 15
                    and len(window.tiles.items) == len(window.tiles.wanted)
                )
            )
            point = window.map.mapFromScene(QPointF(x, y))
            QTest.mouseClick(window.map.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert abs(window.session.truth.latitude - 47.2692) < 0.0001
            assert abs(window.session.truth.longitude - 11.4041) < 0.0001
            assert window.session.round.snapshot() == snapshot
            window.details.setChecked(False)
            assert not window.tiles.items and not window.map.detail_active
            assert window.session.round.snapshot() == snapshot
            window.session.clear_supplements()
            window.update_snapshot()
            assert not window.map.cell_items and window.ranking.rowCount() == 0
            window.close()
            app.processEvents()
            # Сбой сети — сигнал без зависания и немедленных повторов.
            bad = TileClient(path / "bad-cache", template="http://127.0.0.1:1/{z}/{x}/{y}.png")
            failures = []
            bad.failed.connect(failures.append)
            bad.select([(0, 0, 0)])
            pump(lambda: bool(failures))
            bad.select([(0, 0, 0)])
            assert not bad.pending
            bad.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print(
        "Map details smoke: viewport tiles, HTTP cache, attribution, zoom, zones, precise label, disable/offline — OK"
    )


if __name__ == "__main__":
    main()
