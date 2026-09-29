"""Проверка полного UI без захвата пользовательского экрана и без GPU."""

import os
import time
from pathlib import Path
from uuid import uuid4

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PIL import Image
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from streetjev.application.session import AnalysisResult
from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.infrastructure.capture import prepare_image
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
    saved_scores = window.session.round.snapshot().countries
    window.grab().save(str(output / "countries.png"))
    window.color_scale.setCurrentIndex(1)
    assert window.map.heat_scale.mode == "absolute"
    window.score_source.setCurrentIndex(1)
    assert window.map.source == "last"
    assert window.ranking.columnCount() == 3
    assert window.session.round.snapshot().countries == saved_scores
    window.score_source.setCurrentIndex(0)
    window.color_scale.setCurrentIndex(0)
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
    assert window.map.heat_scale.empty
    # Малая совместная масса регионов: лидер слоя меньше 0.1%, но география различима.
    fixture = GeoEvidence(
        "demo",
        "scale-smoke",
        Distribution(dict.fromkeys(catalog.countries, 1.0)),
        {"AT": Distribution({region.code: index + 1 for index, region in enumerate(catalog.regions_of("AT"))})},
        "{}",
        0,
    )
    test_frame = Frame(uuid4().hex, b"", b"test", "2026-09-30", "Тест шкалы")
    window.session.accept(AnalysisResult(window.session.round.id, test_frame, fixture, 1))
    window.update_snapshot()
    app.processEvents()
    assert window.map.heat_scale.maximum < 0.001
    highest_region = max(
        window.session.round.snapshot().last_regions, key=window.session.round.snapshot().last_regions.get
    )
    assert window.map.items_by_code[highest_region].brush().color().name() == "#fb6f50"
    assert window.map.items_by_code["DE"].brush().color().name() == "#192a37"
    before_zoom = window.map.heat_scale
    window.map.focus_country("AT")
    assert window.map.heat_scale == before_zoom
    window.grab().save(str(output / "low-regions.png"))
    window.color_scale.setCurrentIndex(1)
    assert window.map.items_by_code[highest_region].brush().color().name() != "#fb6f50"
    window.grab().save(str(output / "low-regions-absolute.png"))
    window.color_scale.setCurrentIndex(0)
    window.resize(1000, 600)
    app.processEvents()
    assert window.width() == 1000 and window.height() == 600
    assert window.sidebar.height() <= window.map.height()
    window.grab().save(str(output / "compact-1000.png"))
    preview_frame = prepare_image(Image.new("RGB", (640, 420), "#557d89"), "Синтетический preview")
    window.show_frame(window.session.round.id, preview_frame)
    app.processEvents()
    assert window.sidebar.height() > window.preview.height() + window.ranking.height()
    window.grab().save(str(output / "preview-1000.png"))
    window.show_settings()
    app.processEvents()
    window.settings_dialog.grab().save(str(output / "settings.png"))
    window.settings_dialog.close()
    window.close()
    app.processEvents()
    print("UI smoke: фоновые задачи, сброс, раунд/кадр, обе шкалы и контраст малых региональных оценок — OK")


if __name__ == "__main__":
    main()
