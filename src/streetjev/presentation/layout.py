"""Компактная компоновка: действия в шапке, настройки вне рабочей карты."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from streetjev.presentation.map_view import MapView


def label(text="", name="muted"):
    widget = QLabel(text)
    widget.setObjectName(name)
    return widget


def button(text, action, tip=""):
    widget = QPushButton(text)
    widget.clicked.connect(action)
    widget.setToolTip(tip)
    return widget


def build_window(w, geography, demo):
    container = QWidget()
    root = QVBoxLayout(container)
    root.setContentsMargins(8, 8, 8, 6)
    root.setSpacing(6)
    actions = QHBoxLayout()
    actions.setSpacing(6)
    w.region_button = button("Область", w.choose_region, "Выбрать область экрана")
    w.window_button = button("Окно", w.choose_window, "Выбрать окно для захвата")
    w.file_button = button("Файл", w.open_image, "Проанализировать изображение")
    w.analyze_button = button("Снимок", w.analyze, "Сделать и проанализировать снимок · Ctrl+Alt+G")
    w.analyze_button.setObjectName("primary")
    w.auto = QCheckBox("Авто")
    w.auto.setToolTip("Снимать выбранный источник с заданным интервалом")
    w.auto.toggled.connect(w.toggle_timer)
    w.interval = QSpinBox()
    w.interval.setRange(1, 300)
    w.interval.setSuffix(" с")
    w.interval.setValue(int(w.settings.value("interval", 5)))
    w.interval.valueChanged.connect(w.interval_changed)
    w.interval.setMaximumWidth(75)
    w.interval.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
    w.reset_button = button("Сброс", w.reset_round, "Начать новый раунд · Ctrl+Alt+R")
    w.reset_button.setObjectName("reset")
    for widget in (
        w.region_button,
        w.window_button,
        w.file_button,
        w.analyze_button,
        w.auto,
        w.interval,
        w.reset_button,
    ):
        actions.addWidget(widget)
    actions.addSpacing(8)
    w.chip_enabled = QCheckBox("Chipoint")
    w.chip_enabled.setToolTip("Дополнительный поиск по галерее Street View. imajev работает независимо.")
    actions.addWidget(w.chip_enabled)
    w.chip_status = label("выкл.")
    w.chip_status.setMaximumWidth(160)
    actions.addWidget(w.chip_status)
    actions.addStretch()
    actions.addWidget(button("Экспорт", w.export_snapshot, "Экспорт текущих оценок в JSON"))
    actions.addWidget(button("Настройки", w.show_settings))
    root.addLayout(actions)

    view = QHBoxLayout()
    view.setSpacing(6)
    w.target_label = label("Источник не выбран")
    w.target_label.setMaximumWidth(265)
    view.addWidget(w.target_label)
    w.mode_badge = label("", "eyebrow")
    view.addWidget(w.mode_badge)
    view.addStretch()
    w.level = QComboBox()
    w.level.addItems(["Страны", "Регионы"])
    w.level.currentIndexChanged.connect(w.level_changed)
    w.score_source = QComboBox()
    w.score_source.addItems(["Раунд", "Кадр"])
    w.score_source.setToolTip("Источник оценок карты и порядок таблицы")
    w.score_source.currentIndexChanged.connect(w.display_changed)
    w.color_scale = QComboBox()
    w.color_scale.addItems(["Контраст · log", "Шкала 0–100%"])
    w.color_scale.setCurrentIndex(int(w.settings.value("color_scale", 0)))
    w.color_scale.currentIndexChanged.connect(w.display_changed)
    w.auto_focus = QCheckBox("Автофокус")
    w.auto_focus.setToolTip(
        "Приближать согласованную группу лидеров. При разбросе по миру — вся карта. Ручное перемещение приостанавливает автофокус."
    )
    w.auto_focus.setChecked(True)
    for widget in (w.level, w.score_source, w.color_scale, w.auto_focus):
        view.addWidget(widget)
    view.addWidget(button("Мир", w.show_world))
    view.addWidget(button("Лидер", w.focus_leader))
    root.addLayout(view)

    canvas = QWidget()
    canvas_layout = QGridLayout(canvas)
    canvas_layout.setContentsMargins(0, 0, 0, 0)
    w.map = MapView(w.catalog, geography)
    w.map.country_clicked.connect(lambda _: w.level.setCurrentIndex(1))
    w.map.manual_navigation.connect(lambda: w.auto_focus.setChecked(False))
    w.auto_focus.toggled.connect(w.auto_focus_changed)
    canvas_layout.addWidget(w.map, 0, 0)
    sidebar = QWidget()
    w.sidebar = sidebar
    sidebar.setObjectName("hypotheses")
    side = QVBoxLayout(sidebar)
    side.setContentsMargins(6, 5, 6, 5)
    side.setSpacing(5)
    w.preview = label("")
    w.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w.preview.setFixedHeight(110)
    w.preview.hide()
    side.addWidget(w.preview)
    w.ranking = QTableWidget(0, 3)
    w.ranking.setHorizontalHeaderLabels(["Гипотеза", "Раунд", "Кадр"])
    w.ranking.setToolTip(
        "Раунд — взвешенное среднее. Кадр — последний учтённый ответ. Проценты не откалиброваны по правильным локациям."
    )
    w.ranking.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in (1, 2):
        w.ranking.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    w.ranking.verticalHeader().hide()
    w.ranking.verticalHeader().setDefaultSectionSize(28)
    w.ranking.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    w.ranking.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    w.ranking.cellDoubleClicked.connect(w.focus_ranked)
    side.addWidget(w.ranking, 1)
    metrics = QHBoxLayout()
    w.frames_label = label("0 кадров")
    w.effective, w.uncertainty, w.agreement = label(), label(), label()
    metrics.addWidget(w.frames_label)
    metrics.addStretch()
    metrics.addWidget(w.agreement)
    side.addLayout(metrics)
    sidebar.setFixedWidth(306)
    canvas_layout.addWidget(sidebar, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    root.addWidget(canvas, 1)

    footer = QHBoxLayout()
    footer.setSpacing(6)
    w.status = label("Выберите источник")
    w.status.setMinimumWidth(160)
    w.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    footer.addWidget(w.status, 1)
    w.scale_caption = label()
    w.scale_caption.setMaximumWidth(140)
    footer.addWidget(w.scale_caption)
    w.legend_labels = []
    for _ in range(6):
        dot = label()
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setMinimumWidth(58)
        dot.setToolTip(
            "Реальная оценка. Цвет отражает контраст текущего слоя, а не достоверность. Границы: Natural Earth."
        )
        w.legend_labels.append(dot)
        footer.addWidget(dot)
    root.addLayout(footer)
    w.map.scale_changed.connect(w.update_legend)
    w.setCentralWidget(container)
    build_settings(w, demo)
    w.set_mode_ui()
    w._model_settings = (w.url.text().strip(), w.beam.value())
    w.chip_enabled.toggled.connect(w.chipoint_changed)
    w.chip_enabled.setChecked(str(w.settings.value("chip_enabled", "false")).lower() == "true" and not demo)


def build_settings(w, demo):
    w.settings_dialog = QDialog(w)
    w.settings_dialog.setWindowTitle("Настройки")
    w.settings_dialog.setMinimumWidth(480)
    form = QFormLayout(w.settings_dialog)
    w.demo = QCheckBox("Демонстрация")
    w.demo.setChecked(demo)
    w.demo.toggled.connect(w.mode_changed)
    form.addRow(w.demo)
    w.url = QLineEdit(str(w.settings.value("url", "http://127.0.0.1:18765")))
    w.url.editingFinished.connect(w.model_settings_changed)
    form.addRow("imajev", w.url)
    w.beam = QSpinBox()
    w.beam.setRange(0, 5)
    w.beam.setValue(int(w.settings.value("beam", 3)))
    w.beam.setSpecialValueText("Только страны")
    w.beam.valueChanged.connect(w.model_settings_changed)
    form.addRow("Ветвей регионов", w.beam)
    w.chip_url = QLineEdit(str(w.settings.value("chip_url", "http://127.0.0.1:18766")))
    w.chip_timeout = QSpinBox()
    w.chip_timeout.setRange(2, 180)
    w.chip_timeout.setSuffix(" с")
    w.chip_timeout.setValue(int(w.settings.value("chip_timeout", 20)))
    form.addRow("Chipoint", w.chip_url)
    form.addRow("Ожидание Chipoint", w.chip_timeout)
    w.capture_key = QKeySequenceEdit(QKeySequence(str(w.settings.value("capture_key", "Ctrl+Alt+G"))))
    w.reset_key = QKeySequenceEdit(QKeySequence(str(w.settings.value("reset_key", "Ctrl+Alt+R"))))
    form.addRow("Снимок", w.capture_key)
    form.addRow("Сброс", w.reset_key)
    form.addRow(button("Применить клавиши", w.apply_hotkeys))
    note = label(
        "Chipoint: street retrieval, без финального reranker.\nВес в объединении: 35%. Оценки не откалиброваны.\nСлужба: services/chipoint/manage.py start"
    )
    note.setWordWrap(True)
    form.addRow(note)
    close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    close.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
    close.rejected.connect(w.settings_dialog.close)
    form.addRow(close)
