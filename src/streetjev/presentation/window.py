import json
import logging
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from streetjev.application.session import Session
from streetjev.infrastructure.capture import ScreenCapture, list_windows, prepare_image
from streetjev.infrastructure.demo import DemoExpert
from streetjev.infrastructure.imajev import ImajevExpert
from streetjev.infrastructure.storage import snapshot_json
from streetjev.presentation.hotkeys import GlobalHotkeys, portable_text
from streetjev.presentation.map_scale import format_score
from streetjev.presentation.map_view import MapView, heat_color
from streetjev.presentation.scores import display_scores
from streetjev.presentation.selection import RegionOverlay
from streetjev.presentation.worker import AnalysisWorker

log = logging.getLogger(__name__)


def label(text, name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    return item


class MainWindow(QMainWindow):
    def __init__(self, catalog, geography, repository, data_dir: Path, demo=False):
        super().__init__()
        self.catalog, self.repository = catalog, repository
        self.session = Session(catalog, repository)
        self.settings = QSettings(str(data_dir / "settings.ini"), QSettings.Format.IniFormat)
        self.demo_expert = DemoExpert(catalog)
        self.target = None
        self.busy = False
        self.closing = False
        self.overlays = []
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.analyze)
        self.hotkeys = GlobalHotkeys(QApplication.instance())
        self.setWindowTitle("StreetJev · географические гипотезы")
        self.resize(1380, 880)
        self.setMinimumSize(1050, 720)
        self.build_ui(geography, demo)
        self.update_snapshot()
        self.apply_hotkeys()

    def build_ui(self, geography, demo):
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(16)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("SCREEN → EVIDENCE → MAP", "eyebrow"))
        titles.addWidget(label("StreetJev", "brand"))
        header.addLayout(titles)
        header.addStretch()
        self.mode_badge = label("", "eyebrow")
        header.addWidget(self.mode_badge)
        self.frames_label = label("0 кадров", "metric")
        header.addWidget(self.frames_label)
        root.addLayout(header)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        panel = QVBoxLayout(left)
        panel.setContentsMargins(0, 0, 14, 0)
        panel.setSpacing(12)
        tabs = QTabWidget()
        control = QWidget()
        controls = QVBoxLayout(control)
        controls.setContentsMargins(0, 12, 0, 0)
        source = QGroupBox("Источник изображения")
        source_layout = QVBoxLayout(source)
        buttons = QHBoxLayout()
        self.region_button = QPushButton("Выбрать область")
        self.region_button.clicked.connect(self.choose_region)
        self.window_button = QPushButton("Выбрать окно")
        self.window_button.clicked.connect(self.choose_window)
        buttons.addWidget(self.region_button)
        buttons.addWidget(self.window_button)
        source_layout.addLayout(buttons)
        self.target_label = label("Область или окно ещё не выбраны", "muted")
        self.target_label.setWordWrap(True)
        source_layout.addWidget(self.target_label)
        self.preview = label("Здесь появится последний кадр", "muted")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(90)
        self.preview.setMaximumHeight(130)
        source_layout.addWidget(self.preview)
        controls.addWidget(source)
        self.analyze_button = QPushButton("Анализировать кадр")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.clicked.connect(self.analyze)
        controls.addWidget(self.analyze_button)
        self.file_button = QPushButton("Открыть изображение…")
        self.file_button.clicked.connect(self.open_image)
        controls.addWidget(self.file_button)
        auto_row = QHBoxLayout()
        self.auto = QCheckBox("Автозахват каждые")
        self.auto.toggled.connect(self.toggle_timer)
        auto_row.addWidget(self.auto)
        self.interval = QSpinBox()
        self.interval.setRange(1, 300)
        self.interval.setValue(int(self.settings.value("interval", 5)))
        self.interval.setSuffix(" с")
        self.interval.valueChanged.connect(self.interval_changed)
        auto_row.addWidget(self.interval)
        controls.addLayout(auto_row)
        self.reset_button = QPushButton("Новый раунд · сбросить знания")
        self.reset_button.setObjectName("reset")
        self.reset_button.clicked.connect(self.reset_round)
        controls.addWidget(self.reset_button)
        self.status = label("Выберите источник и сделайте первый кадр.", "status")
        self.status.setWordWrap(True)
        controls.addWidget(self.status)
        self.effective = label("Вес учтённых кадров: 0.0", "muted")
        controls.addWidget(self.effective)
        self.uncertainty = label("Недостаточно данных: —", "muted")
        controls.addWidget(self.uncertainty)
        self.agreement = label("", "muted")
        self.agreement.setWordWrap(True)
        controls.addWidget(self.agreement)
        controls.addStretch()
        tabs.addTab(control, "Наблюдения")
        config = QWidget()
        form = QFormLayout(config)
        self.demo = QCheckBox("Демонстрационные данные")
        self.demo.setChecked(demo)
        self.demo.toggled.connect(self.mode_changed)
        form.addRow(self.demo)
        self.url = QLineEdit(str(self.settings.value("url", "http://127.0.0.1:18765")))
        self.url.editingFinished.connect(self.model_settings_changed)
        form.addRow("Сервер imajev", self.url)
        self.beam = QSpinBox()
        self.beam.setRange(0, 5)
        self.beam.setValue(int(self.settings.value("beam", 3)))
        self.beam.setSpecialValueText("Только страны")
        self.beam.valueChanged.connect(self.model_settings_changed)
        form.addRow("Ветвей регионов", self.beam)
        self.capture_key = QKeySequenceEdit(QKeySequence(str(self.settings.value("capture_key", "Ctrl+Alt+G"))))
        self.reset_key = QKeySequenceEdit(QKeySequence(str(self.settings.value("reset_key", "Ctrl+Alt+R"))))
        form.addRow("Снимок", self.capture_key)
        form.addRow("Сброс", self.reset_key)
        save_keys = QPushButton("Применить горячие клавиши")
        save_keys.clicked.connect(self.apply_hotkeys)
        form.addRow(save_keys)
        hint = label(
            "Модель запускается из этого репозитория:\nuv run python services/imajev/manage.py start\n\n"
            "При смене модели или настроек начните новый раунд.\n"
            "Проценты — оценки модели; географическая точность пока не откалибрована.",
            "muted",
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        tabs.addTab(config, "Настройки")
        panel.addWidget(tabs)
        self.ranking = QTableWidget(0, 3)
        self.ranking.setHorizontalHeaderLabels(["Гипотеза", "Раунд", "Кадр"])
        self.ranking.setToolTip(
            "Раунд: взвешенное среднее. Кадр: исходная оценка модели на последнем снимке.\n"
            "Для региона показана совместная оценка страны и региона; «—» означает нет данных."
        )
        self.ranking.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.ranking.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.ranking.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.ranking.verticalHeader().hide()
        self.ranking.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.ranking.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.ranking.setMinimumHeight(190)
        self.ranking.cellDoubleClicked.connect(self.focus_ranked)
        panel.addWidget(self.ranking)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(370)
        splitter.addWidget(scroll)
        right = QWidget()
        map_layout = QVBoxLayout(right)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_toolbar = QHBoxLayout()
        self.level = QComboBox()
        self.level.addItems(["Страны", "Регионы"])
        self.level.currentIndexChanged.connect(self.level_changed)
        map_toolbar.addWidget(self.level)
        map_toolbar.addStretch()
        world = QPushButton("Весь мир")
        world.clicked.connect(lambda: self.map.show_world())
        map_toolbar.addWidget(world)
        top = QPushButton("К лидеру")
        top.clicked.connect(self.focus_leader)
        map_toolbar.addWidget(top)
        export = QPushButton("Экспорт JSON")
        export.clicked.connect(self.export_snapshot)
        map_toolbar.addWidget(export)
        map_layout.addLayout(map_toolbar)
        self.map = MapView(self.catalog, geography)
        self.map.country_clicked.connect(lambda _: self.level.setCurrentIndex(1))
        view_toolbar = QHBoxLayout()
        self.score_source = QComboBox()
        self.score_source.addItems(["Оценки раунда", "Последний кадр"])
        self.score_source.currentIndexChanged.connect(self.display_changed)
        view_toolbar.addWidget(self.score_source)
        self.color_scale = QComboBox()
        self.color_scale.addItems(["Относительная шкала · log", "Абсолютная шкала"])
        self.color_scale.setCurrentIndex(int(self.settings.value("color_scale", 0)))
        self.color_scale.currentIndexChanged.connect(self.display_changed)
        view_toolbar.addWidget(self.color_scale)
        view_toolbar.addStretch()
        map_layout.addLayout(view_toolbar)
        map_layout.addWidget(self.map, 1)
        self.scale_caption = label("", "muted")
        self.scale_caption.setWordWrap(True)
        map_layout.addWidget(self.scale_caption)
        legend = QHBoxLayout()
        self.legend_labels = []
        for _ in range(6):
            dot = label("")
            self.legend_labels.append(dot)
            legend.addWidget(dot)
        map_layout.addLayout(legend)
        self.map.scale_changed.connect(self.update_legend)
        map_layout.addWidget(
            label("Колесо — масштаб · перетаскивание — перемещение · двойной щелчок — регионы страны", "muted")
        )
        map_layout.addWidget(
            label("Серый — нет данных или нижняя граница шкалы · оценки не откалиброваны · Natural Earth", "muted")
        )
        splitter.addWidget(right)
        splitter.setSizes([410, 920])
        root.addWidget(splitter, 1)
        self.setCentralWidget(container)
        self.set_mode_ui()
        self._model_settings = (self.url.text().strip(), self.beam.value())

    def model_settings_changed(self):
        current = (self.url.text().strip(), self.beam.value())
        if hasattr(self, "_model_settings") and current != self._model_settings:
            self._model_settings = current
            self.auto.setChecked(False)
            self.reset_round()

    def set_mode_ui(self):
        demo = self.demo.isChecked()
        self.mode_badge.setText("ДЕМО · СИНТЕТИЧЕСКИЕ ДАННЫЕ" if demo else "IMAJEV · ЛОКАЛЬНЫЙ СЕРВЕР")
        self.region_button.setEnabled(not demo and not self.busy)
        self.window_button.setEnabled(not demo and not self.busy)
        self.file_button.setEnabled(not demo and not self.busy)
        self.url.setEnabled(not demo and not self.busy)
        self.beam.setEnabled(not demo and not self.busy)
        self.analyze_button.setText("Добавить демо-наблюдение" if demo else "Анализировать кадр")

    def mode_changed(self):
        self.auto.setChecked(False)
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
        self.reset_round()
        self.status.setText("Источник выбран. Нажмите «Анализировать кадр» или горячую клавишу.")

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
        self.status.setText("Снимаю кадр и проверяю географические признаки…")
        worker = AnalysisWorker(self.session.round, capture, expert)
        worker.signals.captured.connect(lambda frame, rid=self.session.round.id: self.show_frame(rid, frame))
        worker.signals.completed.connect(self.analysis_complete)
        worker.signals.failed.connect(self.analysis_failed)
        worker.signals.finished.connect(self.analysis_finished)
        self.worker = worker
        self.pool.start(worker)

    def show_frame(self, round_id, frame):
        if round_id != self.session.round.id:
            return
        if frame.jpeg:
            pixmap = QPixmap()
            pixmap.loadFromData(frame.jpeg, "JPEG")
            self.preview.setPixmap(
                pixmap.scaled(
                    310, 150, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
            )
        else:
            self.preview.setText("Демо: кадр не снимается и не анализируется")

    def analysis_complete(self, result):
        if result.round_id != self.session.round.id:
            self.status.setText("Ответ предыдущего раунда отброшен. Новый раунд готов.")
            return
        if result.evidence is None:
            self.status.setText("Кадр почти не изменился — повторное наблюдение пропущено.")
            return
        try:
            if self.session.accept(result):
                self.update_snapshot()
                self.status.setText(
                    f"Наблюдение добавлено · {result.evidence.elapsed_ms / 1000:.2f} с\n"
                    f"Вес новизны: {result.novelty:.2f}"
                )
        except Exception as exc:
            log.exception("Не удалось сохранить наблюдение")
            self.analysis_failed(result.round_id, str(exc))

    def analysis_failed(self, round_id, message):
        if round_id == self.session.round.id:
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
        self.preview.clear()
        self.preview.setText("Новый раунд · пока нет кадров")
        self.update_snapshot()
        self.status.setText("Знания сброшены. Следующий кадр начнёт новый раунд.")

    def update_snapshot(self):
        snapshot = self.session.round.snapshot()
        self.map.source = "last" if self.score_source.currentIndex() else "round"
        self.map.scale_mode = "absolute" if self.color_scale.currentIndex() else "relative"
        self.map.update_snapshot(snapshot)
        self.frames_label.setText(f"{snapshot.frames} кадров")
        self.effective.setText(f"Вес учтённых кадров: {snapshot.effective_frames:.2f}")
        self.uncertainty.setText(
            f"«Не знаю» у модели на кадре: {format_score(snapshot.last_unknown)}"
            if snapshot.frames
            else "«Не знаю» у модели на кадре: —"
        )
        if snapshot.known_frames == 0:
            self.agreement.setText("Пока нет географических наблюдений.")
        elif snapshot.known_frames == 1:
            self.agreement.setText("Один кадр. Проверки другими ракурсами пока нет.")
        else:
            self.agreement.setText(
                f"Лидер раунда был первым в {snapshot.agreeing_frames} из "
                f"{snapshot.known_frames} кадров. Совпадение не гарантирует правильность."
            )
        selected = display_scores(snapshot, self.map.source)
        regional = self.level.currentIndex() == 1
        places = self.catalog.regions if regional else self.catalog.countries
        probabilities = selected.regions if regional else selected.countries
        if regional:
            probabilities = {
                k: p for k, p in probabilities.items() if places[k].country_code in selected.observed_regions
            }
        ranked = sorted(probabilities.items(), key=lambda pair: pair[1], reverse=True)[:8]
        self.ranking.setRowCount(len(ranked) if selected.active else 0)
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
                    "border-radius: 4px; padding: 5px;"
                )
        if scale.empty:
            message = "Для этого слоя ещё нет оценок."
        elif scale.flat:
            message = "Оценки равны — цветом нечего выделить."
        elif scale.mode == "relative":
            message = (
                f"Относительный контраст · максимум слоя {format_score(scale.maximum)}. "
                "В легенде реальные оценки, яркость не означает достоверность."
            )
        else:
            message = "Абсолютная шкала оценок 0–100%. Оценка модели не равна вероятности правильного ответа."
        self.scale_caption.setText(message)

    def level_changed(self):
        if hasattr(self, "map"):
            self.map.set_level("regions" if self.level.currentIndex() else "countries")
            self.update_snapshot()

    def focus_leader(self):
        scores = display_scores(self.session.round.snapshot(), self.map.source)
        if scores.active:
            self.map.focus_country(max(scores.countries, key=scores.countries.get))

    def focus_ranked(self, row, column):
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
            self.status.setText("Горячие клавиши действуют и при работе в другом окне.")
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def export_snapshot(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт раунда", "streetjev-round.json", "JSON (*.json)")
        if path:
            try:
                data = json.loads(snapshot_json(self.session.round.snapshot()))
                data["mode"] = "demo" if self.demo.isChecked() else "imajev"
                data["calibrated_for_geography"] = False
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
        for name, value in {
            "url": self.url.text(),
            "beam": self.beam.value(),
            "interval": self.interval.value(),
            "capture_key": portable_text(self.capture_key),
            "reset_key": portable_text(self.reset_key),
            "color_scale": self.color_scale.currentIndex(),
        }.items():
            self.settings.setValue(name, value)
        self.settings.sync()
        self.repository.close()
        event.accept()
