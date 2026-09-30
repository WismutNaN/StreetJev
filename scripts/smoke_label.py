"""Реальный Qt click/pan: разметка карты, исправление, сброс, ошибочный профиль."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from streetjev.application.session import AnalysisResult
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.storage import SqliteRoundRepository
from streetjev.presentation.map_view import project, unproject
from streetjev.presentation.style import STYLE
from streetjev.presentation.window import MainWindow


def main():
    app = QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "seguisym.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    output = Path("artifacts/ui")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "calibration.json").write_text("{broken", encoding="utf-8")
        repo = SqliteRoundRepository(path / "rounds.sqlite3")
        catalog, geography = load_geography()
        window = MainWindow(catalog, geography, repo, path, demo=True)
        window.resize(1000, 600)
        window.show()
        app.processEvents()
        assert "не загружен" in window.status.text()
        assert not window.answer_button.isEnabled()
        sample = window.demo_expert.frame()
        window.session.accept(AnalysisResult(window.session.round.id, sample, window.demo_expert.infer(sample), 1))
        window.update_snapshot()
        before = window.session.round.snapshot()
        window.auto_focus.setChecked(False)
        window.map.focus_country("AT")
        app.processEvents()

        def point(lat, lon):
            return window.map.mapFromScene(QPointF(*project(lon, lat)))

        vienna = point(48.2082, 16.3738)
        QTest.mouseClick(window.map.viewport(), Qt.MouseButton.LeftButton, pos=vienna)
        assert window.session.truth is None
        window.answer_button.click()
        QTest.mousePress(window.map.viewport(), Qt.MouseButton.LeftButton, pos=vienna)
        QTest.mouseMove(window.map.viewport(), vienna + QPoint(30, 0))
        QTest.mouseRelease(window.map.viewport(), Qt.MouseButton.LeftButton, pos=vienna + QPoint(30, 0))
        assert window.session.truth is None, "Перетаскивание карты не должно сохранять ответ"
        QTest.mouseClick(window.map.viewport(), Qt.MouseButton.LeftButton, pos=point(48.2082, 16.3738))
        assert window.session.truth.country == "AT"
        assert window.session.truth.region is not None
        assert abs(window.session.truth.latitude - 48.2082) < 0.05
        assert window.map.answer_marker is not None
        assert window.session.round.snapshot() == before
        assert not window.map.answer_mode
        window.start_analysis(lambda: (_ for _ in ()).throw(AssertionError("Захват после разметки")))
        assert not window.busy
        window.map.focus_country("DE")
        window.answer_button.click()
        QTest.mouseClick(window.map.viewport(), Qt.MouseButton.LeftButton, pos=point(48.137, 11.575))
        assert window.session.truth.country == "DE"
        assert window.session.truth.region == "DE-BY"
        assert repo.connection.execute("SELECT count(*) FROM ground_truth").fetchone()[0] == 1
        app.processEvents()
        window.grab().save(str(output / "correct-answer.png"))
        window.reset_round()
        assert not window.answer_button.isEnabled()
        assert window.map.answer_marker is None
        assert repo.connection.execute("SELECT country FROM ground_truth").fetchone()[0] == "DE"
        for lon, lat in ((11, 48), (-140, -35), (179, 80)):
            actual_lon, actual_lat = unproject(*project(lon, lat))
            assert abs(actual_lat - lat) < 1e-8 and abs(actual_lon - lon) < 1e-8
        window.close()
    print("Label smoke: обычный клик, pan, точка/регион, исправление, остановка захвата, сброс, профиль — OK")


if __name__ == "__main__":
    main()
