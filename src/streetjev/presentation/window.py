import json
import logging
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QStyle,
    QTableWidgetItem,
)

from streetjev.application.session import Session
from streetjev.domain.calibration import CalibrationProfile
from streetjev.domain.clues import apply_clues
from streetjev.domain.ground_truth import GroundTruth
from streetjev.infrastructure.calibration import read_profile
from streetjev.infrastructure.capture import ScreenCapture, list_windows, prepare_image
from streetjev.infrastructure.chipoint import ChipointExpert, GeographicLocator
from streetjev.infrastructure.clues import load_clue_facts
from streetjev.infrastructure.coverage import CoverageDensity
from streetjev.infrastructure.demo import DemoExpert
from streetjev.infrastructure.imajev import ImajevExpert
from streetjev.infrastructure.storage import snapshot_json
from streetjev.presentation.hotkeys import GlobalHotkeys, portable_text
from streetjev.presentation.map_scale import format_score, support_color
from streetjev.presentation.ranking import ranked_places, row_score
from streetjev.presentation.scores import display_scores
from streetjev.presentation.selection import RegionOverlay
from streetjev.presentation.worker import AnalysisWorker, SupplementWorker

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, catalog, geography, repository, data_dir: Path, demo=False):
        super().__init__()
        self.catalog, self.repository = catalog, repository
        self.data_dir = data_dir
        self.geography = geography
        self.locator = GeographicLocator(geography)
        self.clue_facts = load_clue_facts(geography)
        self.coverage = CoverageDensity()
        self.profile = CalibrationProfile()
        profile_error = None
        if (data_dir / "calibration.json").exists():
            try:
                self.profile = read_profile(data_dir / "calibration.json", catalog)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                profile_error = f"Профиль калибровки не загружен: {exc}"
                log.warning("%s", profile_error)
        self.session = Session(catalog, repository, self.profile)
        self.settings = QSettings(str(data_dir / "settings.ini"), QSettings.Format.IniFormat)
        self.demo_expert = DemoExpert(catalog)
        self.target = None
        self.busy = False
        self.closing = False
        self.overlays = []
        self.chip_busy = False
        self.refinement_busy = False
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
        self.mode_badge.setToolTip(
            "Профиль калибровки загружен" if self.profile.imajev_models else "Стандартные оценки"
        )
        if profile_error:
            self.status.setText(profile_error)

    def build_ui(self, geography, demo):
        from streetjev.presentation.layout import build_window

        build_window(self, geography, demo)

    def model_settings_changed(self):
        current = (self.url.text().strip(), self.beam.value(), self.coverage_enabled.isChecked())
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
        self.coverage_enabled.setEnabled(not demo and not self.busy)
        self.analyze_button.setText("Демо-кадр" if demo else "Снимок")
        self.chip_enabled.setEnabled(not demo)
        self.answer_button.setEnabled(self.session.round.frames > 0 and not self.busy)

    def answer_mode_changed(self, enabled):
        self.map.set_answer_mode(enabled)
        if enabled:
            self.auto.setChecked(False)
            self.auto_focus.setChecked(False)
            self.status.setText("Кликните по правильному месту; карту можно приближать и перемещать")

    def save_answer(self, latitude, longitude):
        if self.busy or self.closing:
            return
        country, region = self.locator.locate(latitude, longitude)
        if country is None:
            self.status.setText("Здесь не определена страна. Приблизьте карту и уточните точку")
            return
        truth = GroundTruth(latitude, longitude, country, region)
        try:
            self.session.label(truth)
        except Exception as exc:
            log.exception("Не удалось сохранить правильный ответ")
            self.status.setText(f"Ответ не сохранён: {exc}")
            return
        self.auto.setChecked(False)
        self.answer_button.setChecked(False)
        self.answer_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        caption = self.catalog.countries[country].label
        if region:
            caption += " · " + self.catalog.regions[region].label
        dataset = self.subregions.datasets.get(country)
        if dataset:
            code = dataset.locate(latitude, longitude)
            if code:
                caption += " · " + dataset.by_code[code].name
        self.answer_button.setToolTip(f"{caption}\n{latitude:.5f}, {longitude:.5f}\nНажмите, чтобы исправить")
        self.map.show_answer(truth)
        self.status.setText(f"Сохранено: {caption}. «Сброс» — следующий раунд")

    def mode_changed(self):
        self.subregions.enabled = not self.demo.isChecked()
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
        if self.session.truth is not None:
            self.auto.setChecked(False)
            self.status.setText("Ответ сохранён. Нажмите «Сброс» для следующего раунда")
            return
        try:
            expert = (
                self.demo_expert
                if self.demo.isChecked()
                else ImajevExpert(
                    self.catalog,
                    self.url.text().strip(),
                    self.beam.value(),
                    facts=self.clue_facts,
                    profile=self.profile,
                )
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
        self.answer_button.setChecked(False)
        self.answer_button.setText("Ответ")
        self.answer_button.setIcon(QIcon())
        self.answer_button.setToolTip("Отметить правильное место на карте; повторная отметка исправляет ответ")
        self.map.show_answer()
        self.chip_generation += 1
        self.pending_chip.clear()
        self.failed_frames.clear()
        self.chip_status.setText(
            "завершается…"
            if self.chip_busy or self.refinement_busy
            else "готов"
            if self.chip_enabled.isChecked()
            else "выкл."
        )
        self.preview.clear()
        self.preview.hide()
        self.auto_focus.setChecked(True)
        self.update_snapshot()
        self.status.setText("Новый раунд")

    def update_snapshot(self):
        snapshot = self.session.round.snapshot()
        self.answer_button.setEnabled(snapshot.frames > 0 and not self.busy)
        self.map.source = "last" if self.score_source.currentIndex() else "round"
        self.map.scale_mode = "absolute" if self.color_scale.currentIndex() else "relative"
        self.map.left_inset = self.sidebar.width() + 8 if display_scores(snapshot, self.map.source).active else 0
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
        self.update_ranking()

    def update_ranking(self):
        snapshot = self.session.round.snapshot()
        selected = display_scores(snapshot, self.map.source)
        rows = ranked_places(
            self.catalog, selected, self.subregions.places, self.subregions.support, self.subregions.assignments
        )
        self.ranking.setRowCount(len(rows))
        self.sidebar.setVisible(selected.active or not self.preview.isHidden())
        self.fit_side_panel()
        sources = {source: display_scores(snapshot, source) for source in ("round", "last")}
        local_scores = {source: self.subregions.projection(scores)[0] for source, scores in sources.items()}
        for row, entry in enumerate(rows):
            item = QTableWidgetItem(entry.label)
            item.setData(Qt.ItemDataRole.UserRole, (entry.kind, entry.code))
            item.setToolTip(
                entry.label.strip()
                + "\nДвойной клик — приблизить"
                + ("\nКоординаты совпадения Chipoint" if entry.kind == "point" else "")
                + (
                    "\nАдминистративный подрегион ADM2 · поддержка точек Chipoint"
                    if entry.kind == "subregion"
                    else ""
                )
            )
            font = item.font()
            font.setBold(entry.kind == "country")
            item.setFont(font)
            self.ranking.setItem(row, 0, item)
            for column, source in ((1, "round"), (2, "last")):
                value = row_score(entry, sources[source], local_scores[source])
                self.ranking.setItem(
                    row, column, QTableWidgetItem(format_score(value) if value is not None else "—")
                )

    def display_changed(self):
        self.update_snapshot()

    def update_legend(self, scale):
        active = not self.map.country_scale.empty
        for index, dot in enumerate(self.legend_labels):
            intensity = index / (len(self.legend_labels) - 1)
            dot.setVisible(active)
            dot.setText(
                format_score(intensity)
                if self.map.scale_mode == "absolute"
                else "Ниже"
                if index == 0
                else "Выше"
                if index == len(self.legend_labels) - 1
                else ""
            )
            dot.setStyleSheet(
                f"background: {support_color(intensity).name()}; color: {'#102920' if intensity >= 0.3 else '#dce7ef'}; border-radius: 3px; padding: 3px;"
            )
        self.scale_caption.setText(
            "Нет оценок" if not active else "Внутри уровня" if self.map.scale_mode == "relative" else "Общая шкала"
        )
        self.scale_caption.setToolTip(
            "Контраст отдельно для стран, всех исследованных регионов и локальных групп. Цвет не означает достоверность; круг — область поиска."
        )

    def show_location_details(self, message):
        self.status.setText(message)
        self.status.setToolTip(message)

    def focus_leader(self):
        self.auto_focus.setChecked(False)
        scores = display_scores(self.session.round.snapshot(), self.map.source)
        if scores.active:
            country = max(scores.countries, key=scores.countries.get)
            regions = self.catalog.regions_of(country) if country in scores.observed_regions else []
            code = max(regions, key=lambda region: scores.regions.get(region.code, 0)).code if regions else country
            self.map.focus_place(code)

    def focus_ranked(self, row, column):
        self.auto_focus.setChecked(False)
        item = self.ranking.item(row, 0)
        if not item:
            return
        kind, code = item.data(Qt.ItemDataRole.UserRole)
        if kind == "point":
            point = next(
                (
                    p
                    for p in display_scores(self.session.round.snapshot(), self.map.source).points
                    if p.code == code
                ),
                None,
            )
            if point:
                self.map.focus_point(point)
        else:
            self.map.focus_place(code)

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
                from dataclasses import asdict

                data["calibration_profile"] = asdict(self.profile) if self.profile.imajev_models else None
                data["ground_truth"] = asdict(self.session.truth) if self.session.truth else None
                support, _ = self.subregions.projection(display_scores(self.session.round.snapshot()))
                data["subregions"] = {
                    code: {**asdict(self.subregions.places[code]), "support": mass}
                    for code, mass in support.items()
                }
                data["subregion_sources"] = {c: d.metadata for c, d in self.subregions.datasets.items()}
                if self.session.truth:
                    truth = self.session.truth
                    dataset = self.subregions.datasets.get(truth.country)
                    data["ground_truth"]["subregion"] = (
                        dataset.locate(truth.latitude, truth.longitude) if dataset else None
                    )
                data["chipoint_enabled"] = self.chip_enabled.isChecked()
                data["fusion"] = "linear_experts_v2" if self.chip_enabled.isChecked() else None
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
        self.tiles.close()
        self.subregions.close()
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
            "coverage_enabled": self.coverage_enabled.isChecked(),
            "map_details": self.details.isChecked(),
            "local_radius_km": self.local_radius.value(),
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
        if self.chip_busy or self.refinement_busy:
            self.chip_status.setToolTip("Предыдущий запрос ещё выполняется; новый кадр обрабатывает только imajev")
            return
        try:
            expert = ChipointExpert(
                self.catalog,
                self.geography,
                self.chip_url.text().strip(),
                self.chip_timeout.value(),
                coverage=self.coverage if self.coverage_enabled.isChecked() else None,
                profile=self.profile,
            )
        except ValueError as exc:
            self.chip_status.setText("ошибка адреса")
            self.chip_status.setToolTip(str(exc))
            return
        generation = self.chip_generation
        self.chip_busy = True
        self.chip_status.setText("поиск…")
        self.chip_status.setToolTip("Поиск Chipoint. imajev продолжает работать независимо.")
        worker = SupplementWorker(request, expert.infer)
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
        from dataclasses import replace

        try:
            primary = self.session.observation(result.frame.id)
            if primary is None:
                return
            parameters = self.profile.for_model("chipoint", result.evidence.model_version)
            result = replace(
                result,
                evidence=apply_clues(
                    result.evidence,
                    primary.evidence.clues,
                    self.clue_facts,
                    parameters.driving_strength,
                    parameters.hemisphere_strength,
                ),
            )
            if self.session.supplement(result):
                self.chip_status.setText(f"{result.evidence.elapsed_ms / 1000:.1f} с")
                weight = self.profile.fusion_weight(primary.evidence.model_version, result.evidence.model_version)
                self.chip_status.setToolTip(
                    f"Кадр дополнен Chipoint Street Retrieval; вес {weight:.0%}, без reranker"
                )
                self.update_snapshot()
                self.start_refinement(primary, result)
        except Exception as exc:
            self.chip_status.setText("ошибка сохранения")
            self.chip_status.setToolTip(str(exc))
            log.exception("Ошибка сохранения Chipoint")

    def start_refinement(self, primary, secondary):
        expert = ImajevExpert(
            self.catalog, self.url.text().strip(), self.beam.value(), facts=self.clue_facts, profile=self.profile
        )
        if not expert.missing_regions(primary.evidence, secondary.evidence):
            return
        generation = self.chip_generation
        self.refinement_busy = True
        self.chip_status.setText("регионы…")
        worker = SupplementWorker(
            secondary, lambda frame: expert.refine(frame, primary.evidence, secondary.evidence)
        )
        worker.signals.completed.connect(lambda result: self.refinement_complete(generation, result))
        worker.signals.failed.connect(lambda rid, message: self.refinement_failed(generation, rid, message))
        worker.signals.finished.connect(self.refinement_finished)
        self.refinement_worker = worker
        worker.start()

    def refinement_complete(self, generation, result):
        if self.closing or generation != self.chip_generation or result.round_id != self.session.round.id:
            return
        try:
            if self.session.refine(result):
                self.update_snapshot()
                self.chip_status.setText("уточнён")
                self.chip_status.setToolTip("imajev проверил дополнительные страны Chipoint на том же кадре")
        except Exception as exc:
            log.exception("Не удалось сохранить уточнение регионов")
            self.refinement_failed(generation, result.round_id, str(exc))

    def refinement_failed(self, generation, round_id, message):
        if not self.closing and generation == self.chip_generation and round_id == self.session.round.id:
            self.chip_status.setText("без уточнения")
            self.chip_status.setToolTip("Результаты обеих моделей сохранены. Уточнение регионов: " + message)

    def refinement_finished(self):
        self.refinement_busy = False
        if not self.closing and not self.chip_busy and self.chip_status.text() == "завершается…":
            self.chip_status.setText("готов" if self.chip_enabled.isChecked() else "выкл.")

    def chipoint_failed(self, generation, round_id, message):
        if not self.closing and generation == self.chip_generation and round_id == self.session.round.id:
            self.chip_status.setText("недоступен")
            self.chip_status.setToolTip(
                message + "\nimajev продолжает работу. Запуск: services/chipoint/manage.py start"
            )
            log.warning("%s", message)

    def chipoint_finished(self):
        self.chip_busy = False
        if not self.closing and not self.refinement_busy and self.chip_status.text() == "завершается…":
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
