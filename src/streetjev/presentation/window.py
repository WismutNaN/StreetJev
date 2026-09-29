import json
import logging
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QTableWidgetItem,
)

from streetjev.application.session import Session
from streetjev.infrastructure.capture import ScreenCapture, list_windows, prepare_image
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.demo import DemoExpert
from streetjev.infrastructure.imajev import ImajevExpert
from streetjev.infrastructure.storage import snapshot_json
from streetjev.presentation.hotkeys import GlobalHotkeys, portable_text
from streetjev.presentation.map_scale import format_score
from streetjev.presentation.map_view import heat_color
from streetjev.presentation.scores import display_scores
from streetjev.presentation.selection import RegionOverlay
from streetjev.presentation.worker import AnalysisWorker, SupplementWorker

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, catalog, geography, repository, data_dir: Path, demo=False):
        super().__init__()
        self.catalog, self.repository = catalog, repository
        self.geography = geography
        self.session = Session(catalog, repository)
        self.settings = QSettings(str(data_dir / "settings.ini"), QSettings.Format.IniFormat)
        self.demo_expert = DemoExpert(catalog)
        self.target = None
        self.busy = False
        self.closing = False
        self.overlays = []
        self.chip_busy = False
        self.chip_generation = 0
        self.pending_chip = {}
        self.failed_frames = set()
        self.primary_frame_id = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.analyze)
        self.hotkeys = GlobalHotkeys(QApplication.instance())
        self.setWindowTitle("StreetJev · географические гипотезы")
        self.resize(1320, 820)
        self.setMinimumSize(1000, 600)
        self.build_ui(geography, demo)
        self.update_snapshot()
        self.apply_hotkeys()

    def build_ui(self, geography, demo):
        from streetjev.presentation.layout import build_window

        build_window(self, geography, demo)

    def model_settings_changed(self):
        current = (self.url.text().strip(), self.beam.value())
        if hasattr(self, "_model_settings") and current != self._model_settings:
            self._model_settings = current
            self.auto.setChecked(False)
            self.reset_round()

    def set_mode_ui(self):
        demo = self.demo.isChecked()
        self.mode_badge.setText("Демо" if demo else "imajev")
        self.region_button.setEnabled(not demo and not self.busy)
        self.window_button.setEnabled(not demo and not self.busy)
        self.file_button.setEnabled(not demo and not self.busy)
        self.url.setEnabled(not demo and not self.busy)
        self.beam.setEnabled(not demo and not self.busy)
        self.analyze_button.setText("Демо-кадр" if demo else "Снимок")
        self.chip_enabled.setEnabled(not demo)

    def mode_changed(self):
        self.auto.setChecked(False)
        self.chip_enabled.setChecked(False)
        self.reset_round()
        self.set_mode_ui()

    def interval_changed(self):
        self.timer.setInterval(self.interval.value() * 1000)

    def toggle_timer(self, enabled):
        if enabled:
            if not self.demo.isChecked() and self.target is None:
                self.auto.setChecked(False)
                self.status.setText("Для автозахвата сначала выберите область или окно.")
                return
            self.timer.start(self.interval.value() * 1000)
        else:
            self.timer.stop()

    def choose_window(self):
        try:
            windows = list_windows()
            if not windows:
                raise RuntimeError("Нет доступных окон. Разверните нужное окно или выберите область.")
            names = [f"{item.name}  [{item.hwnd}]" for item in windows]
            choice, accepted = QInputDialog.getItem(self, "Выбор окна", "Окно для анализа", names, 0, False)
            if accepted:
                self.set_target(windows[names.index(choice)])
        except Exception as exc:
            log.exception("Не удалось выбрать окно")
            self.status.setText(str(exc))

    def choose_region(self):
        self.auto.setChecked(False)
        self.hide()
        QTimer.singleShot(200, self.show_overlays)

    def show_overlays(self):
        try:
            for screen in QApplication.screens():
                geometry = screen.geometry()
                fallback = (
                    geometry.x(),
                    geometry.y(),
                    round(geometry.width() * screen.devicePixelRatio()),
                    round(geometry.height() * screen.devicePixelRatio()),
                )
                overlay = RegionOverlay(screen, fallback)
                overlay.selected.connect(self.selection_done)
                overlay.cancelled.connect(self.selection_cancelled)
                self.overlays.append(overlay)
                overlay.show()
                overlay.activateWindow()
        except Exception as exc:
            log.exception("Не удалось открыть выбор области")
            self.selection_cancelled()
            self.status.setText(str(exc))

    def close_overlays(self):
        for overlay in self.overlays:
            overlay.close()
            overlay.deleteLater()
        self.overlays.clear()
        self.show()
        self.activateWindow()

    def selection_cancelled(self):
        self.close_overlays()

    def selection_done(self, target):
        self.close_overlays()
        self.set_target(target)

    def set_target(self, target):
        self.target = target
        self.target_label.setText(target.name)
        self.target_label.setToolTip(target.name)
        self.reset_round()
        self.status.setText("Источник выбран")

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть изображение", "", "Изображения (*.jpg *.jpeg *.png *.webp)"
        )
        if path:
            self.auto.setChecked(False)

            def capture():
                with Image.open(path) as image:
                    return prepare_image(image, Path(path).name)

            self.start_analysis(capture)

    def analyze(self):
        if self.busy or self.overlays or self.closing:
            return
        if self.demo.isChecked():
            self.start_analysis(self.demo_expert.frame)
        elif self.target:
            self.start_analysis(ScreenCapture(self.target).capture)
        else:
            self.status.setText("Сначала выберите область, окно или откройте изображение.")

    def start_analysis(self, capture):
        if self.busy or self.closing:
            return
        try:
            expert = (
                self.demo_expert
                if self.demo.isChecked()
                else ImajevExpert(self.catalog, self.url.text().strip(), self.beam.value())
            )
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.busy = True
        self.analyze_button.setEnabled(False)
        self.demo.setEnabled(False)
        self.set_mode_ui()
        self.status.setText("imajev: анализ…")
        worker = AnalysisWorker(self.session.round, capture, expert)
        worker.signals.captured.connect(lambda frame, rid=self.session.round.id: self.show_frame(rid, frame))
        worker.signals.eligible.connect(self.start_chipoint)
        worker.signals.completed.connect(self.analysis_complete)
        worker.signals.failed.connect(self.analysis_failed)
        worker.signals.finished.connect(self.analysis_finished)
        self.worker = worker
        self.pool.start(worker)

    def show_frame(self, round_id, frame):
        if round_id != self.session.round.id:
            return
        if frame.jpeg:
            self.preview.show()
            self.sidebar.show()
            pixmap = QPixmap()
            pixmap.loadFromData(frame.jpeg, "JPEG")
            self.preview.setPixmap(
                pixmap.scaled(
                    self.preview.width(),
                    110,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.preview.hide()
        self.fit_side_panel()

    def analysis_complete(self, result):
        if result.round_id != self.session.round.id:
            return
        if result.evidence is None:
            self.status.setText("Повторный кадр пропущен")
            return
        try:
            if self.session.accept(result):
                pending = self.pending_chip.pop(result.frame.id, None)
                if pending and self.chip_enabled.isChecked():
                    self.accept_chipoint(pending)
                self.update_snapshot()
                self.status.setText(f"imajev · {result.evidence.elapsed_ms / 1000:.1f} с")
        except Exception as exc:
            log.exception("Не удалось сохранить наблюдение")
            self.analysis_failed(result.round_id, str(exc))

    def analysis_failed(self, round_id, message):
        if round_id == self.session.round.id:
            if self.primary_frame_id:
                self.failed_frames.add(self.primary_frame_id)
                self.pending_chip.pop(self.primary_frame_id, None)
            self.auto.setChecked(False)
            self.status.setText(message)
            log.error("Ошибка анализа: %s", message)

    def analysis_finished(self):
        self.busy = False
        self.analyze_button.setEnabled(True)
        self.demo.setEnabled(True)
        self.set_mode_ui()
        if self.closing:
            QTimer.singleShot(0, self.close)

    def reset_round(self):
        try:
            self.session.reset()
        except Exception as exc:
            log.exception("Не удалось создать раунд")
            self.status.setText(f"Не удалось создать раунд: {exc}")
            return
        self.demo_expert = DemoExpert(self.catalog)
        self.chip_generation += 1
        self.pending_chip.clear()
        self.failed_frames.clear()
        self.chip_status.setText(
            "завершается…" if self.chip_busy else "готов" if self.chip_enabled.isChecked() else "выкл."
        )
        self.preview.clear()
        self.preview.hide()
        self.auto_focus.setChecked(True)
        self.update_snapshot()
        self.status.setText("Новый раунд")

    def update_snapshot(self):
        snapshot = self.session.round.snapshot()
        self.map.source = "last" if self.score_source.currentIndex() else "round"
        self.map.scale_mode = "absolute" if self.color_scale.currentIndex() else "relative"
        self.map.update_snapshot(snapshot)
        if self.auto_focus.isChecked():
            self.map.focus_hypotheses(snapshot)
        self.frames_label.setText(f"{snapshot.frames} кадров")
        self.effective.setText(f"Вес учтённых кадров: {snapshot.effective_frames:.2f}")
        self.uncertainty.setText(
            f"«Не знаю» у модели на кадре: {format_score(snapshot.last_unknown)}"
            if snapshot.frames
            else "«Не знаю» у модели на кадре: —"
        )
        self.agreement.setText(
            f"Согласие {snapshot.agreeing_frames}/{snapshot.known_frames}" if snapshot.known_frames else ""
        )
        self.frames_label.setToolTip(self.effective.text() + "\n" + self.uncertainty.text())
        self.agreement.setToolTip(
            "В скольких кадрах лидер раунда был первым. Это не вероятность правильного ответа."
        )
        selected = display_scores(snapshot, self.map.source)
        regional = self.level.currentIndex() == 1
        places = self.catalog.regions if regional else self.catalog.countries
        probabilities = selected.regions if regional else selected.countries
        if regional:
            probabilities = {
                k: p for k, p in probabilities.items() if places[k].country_code in selected.observed_regions
            }
        ranked = sorted(probabilities.items(), key=lambda pair: pair[1], reverse=True)[:12]
        if ranked:
            ranked = [item for index, item in enumerate(ranked) if index < 3 or item[1] >= ranked[0][1] * 0.01]
        self.ranking.setRowCount(len(ranked) if selected.active else 0)
        self.sidebar.setVisible(selected.active or not self.preview.isHidden())
        self.fit_side_panel()
        if selected.active:
            for row, (key, _probability) in enumerate(ranked):
                name = places[key].label
                if regional:
                    name += " · " + places[key].country_code
                item = QTableWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, places[key].country_code or key)
                self.ranking.setItem(row, 0, item)
                for column, source in ((1, "round"), (2, "last")):
                    scores = display_scores(snapshot, source)
                    available = scores.active and (
                        not regional or places[key].country_code in scores.observed_regions
                    )
                    value = (scores.regions if regional else scores.countries).get(key, 0)
                    self.ranking.setItem(row, column, QTableWidgetItem(format_score(value) if available else "—"))

    def display_changed(self):
        self.update_snapshot()

    def update_legend(self, scale):
        ticks = scale.legend()
        for index, dot in enumerate(self.legend_labels):
            dot.setVisible(index < len(ticks))
            if index < len(ticks):
                intensity, text = ticks[index]
                dot.setText(text)
                foreground = "#102920" if intensity >= 0.4 else "#dce7ef"
                dot.setStyleSheet(
                    f"background: {heat_color(intensity).name()}; color: {foreground}; "
                    "border-radius: 3px; padding: 3px;"
                )
        if scale.empty:
            message = "Нет оценок"
        elif scale.flat:
            message = "Равные оценки"
        elif scale.mode == "relative":
            message = f"Макс. {format_score(scale.maximum)}"
        else:
            message = "Абсолютная"
        self.scale_caption.setText(message)
        self.scale_caption.setToolTip(
            "В легенде реальные оценки; яркость не означает достоверность. Серый — нет данных или нижняя граница шкалы."
        )

    def level_changed(self):
        if hasattr(self, "map"):
            self.map.set_level("regions" if self.level.currentIndex() else "countries")
            self.update_snapshot()

    def focus_leader(self):
        self.auto_focus.setChecked(False)
        scores = display_scores(self.session.round.snapshot(), self.map.source)
        if scores.active:
            self.map.focus_country(max(scores.countries, key=scores.countries.get))

    def focus_ranked(self, row, column):
        self.auto_focus.setChecked(False)
        item = self.ranking.item(row, 0)
        if item:
            self.map.focus_country(item.data(Qt.ItemDataRole.UserRole))

    def apply_hotkeys(self):
        try:
            self.hotkeys.register(
                [
                    (portable_text(self.capture_key), self.analyze),
                    (portable_text(self.reset_key), self.reset_round),
                ]
            )
            self.status.setText("Готово")
            self.analyze_button.setToolTip("Снимок · " + portable_text(self.capture_key))
            self.reset_button.setToolTip("Новый раунд · " + portable_text(self.reset_key))
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def export_snapshot(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт раунда", "streetjev-round.json", "JSON (*.json)")
        if path:
            try:
                data = json.loads(snapshot_json(self.session.round.snapshot()))
                data["mode"] = "demo" if self.demo.isChecked() else "imajev"
                data["calibrated_for_geography"] = False
                data["chipoint_enabled"] = self.chip_enabled.isChecked()
                data["fusion"] = "linear_experts_v1" if self.chip_enabled.isChecked() else None
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText("Текущее распределение экспортировано.")
            except OSError as exc:
                self.status.setText(f"Не удалось записать файл: {exc}")

    def closeEvent(self, event):
        self.timer.stop()
        if self.busy:
            self.closing = True
            self.setEnabled(False)
            self.status.setText("Завершаю текущий запрос перед закрытием…")
            event.ignore()
            return
        self.hotkeys.close()
        self.closing = True
        self.chip_generation += 1
        for name, value in {
            "url": self.url.text(),
            "beam": self.beam.value(),
            "interval": self.interval.value(),
            "capture_key": portable_text(self.capture_key),
            "reset_key": portable_text(self.reset_key),
            "color_scale": self.color_scale.currentIndex(),
            "chip_enabled": self.chip_enabled.isChecked(),
            "chip_url": self.chip_url.text(),
            "chip_timeout": self.chip_timeout.value(),
        }.items():
            self.settings.setValue(name, value)
        self.settings.sync()
        self.repository.close()
        event.accept()

    def show_settings(self):
        self.settings_dialog.show()
        self.settings_dialog.raise_()

    def show_world(self):
        self.auto_focus.setChecked(False)
        self.map.show_world()

    def auto_focus_changed(self, enabled):
        if enabled:
            self.map.focus_hypotheses(self.session.round.snapshot())

    def chipoint_changed(self, enabled):
        self.chip_generation += 1
        self.pending_chip.clear()
        if not enabled:
            try:
                self.session.clear_supplements()
                self.update_snapshot()
            except Exception as exc:
                log.exception("Не удалось отключить Chipoint")
                self.chip_enabled.blockSignals(True)
                self.chip_enabled.setChecked(True)
                self.chip_enabled.blockSignals(False)
                self.status.setText(f"Не удалось отключить Chipoint: {exc}")
                return
        self.chip_status.setText("готов" if enabled else "выкл.")

    def start_chipoint(self, request):
        self.primary_frame_id = request.frame.id
        if (
            request.round_id != self.session.round.id
            or not self.chip_enabled.isChecked()
            or self.demo.isChecked()
            or self.closing
        ):
            return
        if self.chip_busy:
            self.chip_status.setToolTip("Предыдущий запрос ещё выполняется; новый кадр обрабатывает только imajev")
            return
        try:
            expert = ChipointExpert(
                self.catalog, self.geography, self.chip_url.text().strip(), self.chip_timeout.value()
            )
        except ValueError as exc:
            self.chip_status.setText("ошибка адреса")
            self.chip_status.setToolTip(str(exc))
            return
        generation = self.chip_generation
        self.chip_busy = True
        self.chip_status.setText("поиск…")
        self.chip_status.setToolTip("Поиск Chipoint. imajev продолжает работать независимо.")
        worker = SupplementWorker(request, expert)
        worker.signals.completed.connect(lambda result: self.chipoint_complete(generation, result))
        worker.signals.failed.connect(lambda rid, message: self.chipoint_failed(generation, rid, message))
        worker.signals.finished.connect(self.chipoint_finished)
        self.chip_worker = worker
        worker.start()

    def chipoint_complete(self, generation, result):
        if self.closing or generation != self.chip_generation or result.round_id != self.session.round.id:
            return
        if result.frame.id in self.failed_frames:
            return
        if self.session.has_frame(result.frame.id):
            self.accept_chipoint(result)
        else:
            self.pending_chip[result.frame.id] = result
            self.chip_status.setText("ждёт imajev")

    def accept_chipoint(self, result):
        try:
            if self.session.supplement(result):
                self.chip_status.setText(f"{result.evidence.elapsed_ms / 1000:.1f} с")
                self.chip_status.setToolTip("Кадр дополнен Chipoint Street Retrieval; вес 35%, без reranker")
                self.update_snapshot()
        except Exception as exc:
            self.chip_status.setText("ошибка сохранения")
            self.chip_status.setToolTip(str(exc))
            log.exception("Ошибка сохранения Chipoint")

    def chipoint_failed(self, generation, round_id, message):
        if not self.closing and generation == self.chip_generation and round_id == self.session.round.id:
            self.chip_status.setText("недоступен")
            self.chip_status.setToolTip(
                message + "\nimajev продолжает работу. Запуск: services/chipoint/manage.py start"
            )
            log.warning("%s", message)

    def chipoint_finished(self):
        self.chip_busy = False
        if not self.closing and self.chip_status.text() == "завершается…":
            self.chip_status.setText("готов" if self.chip_enabled.isChecked() else "выкл.")

    def fit_side_panel(self):
        needed = self.ranking.horizontalHeader().height() + 28 * self.ranking.rowCount() + 2
        room = self.height() - 165 - (115 if not self.preview.isHidden() else 0)
        self.ranking.setFixedHeight(max(60, min(needed, room)))
        self.sidebar.setFixedHeight(self.ranking.height() + 34 + (115 if not self.preview.isHidden() else 0))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "ranking"):
            self.fit_side_panel()
