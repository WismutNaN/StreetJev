"""Проверка полного UI без захвата пользовательского экрана и без GPU."""

import os
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.storage import SqliteRoundRepository
from streetjev.presentation.style import STYLE
from streetjev.presentation.window import MainWindow


def main():
    output = Path("artifacts/ui")
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    # Windows offscreen plugin не перечисляет системные шрифты автоматически.
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    catalog, geography = load_geography()
    repo = SqliteRoundRepository(output / "smoke.sqlite3")
    window = MainWindow(catalog, geography, repo, output, demo=True)
    window.show()
    app.processEvents()
    window.grab().save(str(output / "empty.png"))
    for _ in range(3):
        window.analyze()
        deadline = time.monotonic() + 10
        while window.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert not window.busy, "Фоновый анализ завис"
    assert window.session.round.frames == 3
    window.grab().save(str(output / "countries.png"))
    window.level.setCurrentIndex(1)
    window.map.focus_country("AT")
    app.processEvents()
    window.grab().save(str(output / "regions.png"))
    # Сброс в момент выполняющегося запроса: старый результат не должен проникнуть в новый раунд.
    window.analyze()
    window.reset_round()
    deadline = time.monotonic() + 10
    while window.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not window.busy
    assert window.session.round.frames == 0
    window.close()
    app.processEvents()
    print("UI smoke: демо, фоновая обработка, карта регионов и сброс во время запроса — OK")


if __name__ == "__main__":
    main()
