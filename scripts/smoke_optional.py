"""Qt + медленный эксперт: imajev не ждёт Chipoint, поздние ответы безопасны."""

import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

import streetjev.presentation.window as ui
from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.storage import SqliteRoundRepository
from streetjev.presentation.style import STYLE


def main():
    app = QApplication([])
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
    app.setStyleSheet(STYLE)
    output = Path("artifacts/optional")
    output.mkdir(parents=True, exist_ok=True)
    catalog, geography = load_geography()
    release = threading.Event()
    calls = []

    def evidence(country):
        values = dict.fromkeys(catalog.countries, 0.1 / (len(catalog.countries) - 1))
        values[country] = 0.9
        return GeoEvidence("imajev", "test", Distribution(values), {}, "{}", 15)

    class Primary:
        def __init__(self, *args, **kwargs):
            pass

        def infer(self, frame):
            return evidence("AT")

        def missing_regions(self, primary, secondary):
            return []

    class Slow:
        def __init__(self, *args, **kwargs):
            pass

        def infer(self, frame):
            calls.append(frame.id)
            if not release.wait(5):
                raise TimeoutError("Тестовый эксперт не получил разрешение завершиться")
            return replace(evidence("DE"), source="chipoint")

    ui.ImajevExpert, ui.ChipointExpert = Primary, Slow
    repo = SqliteRoundRepository(output / "smoke.sqlite3")
    window = ui.MainWindow(catalog, geography, repo, output, demo=False)
    window.details.setChecked(False)
    window.show()
    window.chip_enabled.setChecked(True)

    def pump(until):
        deadline = time.monotonic() + 5
        while not until() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert until(), "UI не достиг ожидаемого состояния"

    def capture(value):
        return lambda: Frame(uuid4().hex, b"", bytes([value]) * 1024, "2026-09-30", "Проверка конкуренции")

    window.start_analysis(capture(0))
    pump(lambda: not window.busy and window.chip_busy)
    assert window.session.round.frames == 1
    assert window.analyze_button.isEnabled()
    window.start_analysis(capture(100))
    pump(lambda: not window.busy and window.session.round.frames == 2)
    assert len(calls) == 1, "Медленный эксперт не должен накапливать очередь"
    window.grab().save(str(output / "imajev-while-chipoint.png"))
    release.set()
    pump(lambda: not window.chip_busy)
    assert window.session.round.frames == 2
    assert window.session.round.countries["AT"] < 0.9
    window.chip_enabled.setChecked(False)
    assert abs(window.session.round.countries["AT"] - 0.9) < 1e-8
    assert window.session.round.frames == 2

    # Отключение во время HTTP: продолжаем imajev в том же раунде, поздний ответ игнорируем.
    release.clear()
    window.chip_enabled.setChecked(True)
    window.start_analysis(capture(200))
    pump(lambda: not window.busy and window.chip_busy)
    window.chip_enabled.setChecked(False)
    before = window.session.round.snapshot()
    release.set()
    pump(lambda: not window.chip_busy)
    assert window.session.round.snapshot() == before

    # Сброс при выполняющемся втором эксперте.
    release.clear()
    window.chip_enabled.setChecked(True)
    window.reset_round()
    window.start_analysis(capture(50))
    pump(lambda: not window.busy and window.chip_busy)
    window.reset_round()
    release.set()
    pump(lambda: not window.chip_busy)
    assert window.session.round.frames == 0

    # Второй эксперт ответил первым: ждём основной ответ, не создаём второй кадр.
    primary_release = threading.Event()

    class SlowPrimary(Primary):
        def infer(self, frame):
            assert primary_release.wait(5)
            return super().infer(frame)

    ui.ImajevExpert = SlowPrimary
    window.start_analysis(capture(80))
    pump(lambda: bool(window.pending_chip))
    assert window.session.round.frames == 0
    primary_release.set()
    pump(lambda: not window.busy and not window.chip_busy)
    assert window.session.round.frames == 1
    assert window.session.round.countries["AT"] < 0.9
    ui.ImajevExpert = Primary
    window.reset_round()

    # Ошибка Chipoint не останавливает таймер или приём imajev.
    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        def infer(self, frame):
            raise TimeoutError("Тестовый timeout Chipoint")

    ui.ChipointExpert = Broken
    window.start_analysis(capture(10))
    pump(lambda: not window.busy and not window.chip_busy)
    assert window.session.round.frames == 1
    assert window.chip_status.text() == "недоступен"

    # После обоих ответов дополнительный регион приходит в тот же кадр.
    refine_release = threading.Event()

    class Refining(Primary):
        def missing_regions(self, primary, secondary):
            return ["DE"] if "DE" not in primary.regions else []

        def refine(self, frame, primary, secondary):
            assert refine_release.wait(5)
            regions = Distribution({p.code: 1 for p in catalog.regions_of("DE")})
            return replace(primary, regions={"DE": regions}, raw_json='{"chipoint_region_refinement": {}}')

    ui.ImajevExpert, ui.ChipointExpert = Refining, Slow
    window.reset_round()
    window.start_analysis(capture(30))
    pump(lambda: not window.busy and window.refinement_busy)
    assert window.session.round.frames == 1
    assert "DE" not in window.session.round.observed_regions
    refine_release.set()
    pump(lambda: not window.refinement_busy and not window.chip_busy)
    assert window.session.round.frames == 1
    assert "DE" in window.session.round.observed_regions
    assert window.chip_status.text() == "уточнён"

    # Уточнение, пришедшее после отключения или сброса, не применяется.
    for reset in (False, True):
        window.chip_enabled.setChecked(True)
        window.reset_round()
        refine_release.clear()
        window.start_analysis(capture(60))
        pump(lambda: not window.busy and window.refinement_busy)
        if reset:
            window.reset_round()
        else:
            window.chip_enabled.setChecked(False)
        refine_release.set()
        pump(lambda: not window.refinement_busy and not window.chip_busy)
        assert "DE" not in window.session.round.observed_regions
        assert window.session.round.frames == (0 if reset else 1)

    window.chip_enabled.setChecked(False)
    window.close()
    app.processEvents()
    print("Optional smoke: медленный ответ, нет очереди, disable, reset, timeout, уточнение регионов — OK")


if __name__ == "__main__":
    main()
