STYLE = """
QWidget { background: #0f1c28; color: #dce7ef; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow { background: #0b1620; }
QWidget#hypotheses { border: 1px solid #304858; border-radius: 5px; background: #0f1c28; }
QLabel#brand { font-size: 26px; font-weight: 700; color: #f1f6f8; }
QLabel#eyebrow { color: #65dab6; font-size: 11px; font-weight: 700; }
QLabel#muted { color: #8da4b8; }
QLabel#metric { font-size: 23px; font-weight: 600; }
QPushButton { background: #1d3344; border: 1px solid #304858; border-radius: 6px;
              padding: 6px 10px; font-weight: 600; }
QPushButton:hover { background: #28485a; border-color: #65dab6; }
QPushButton:disabled { color: #637b8b; background: #152633; }
QPushButton#primary { background: #65dab6; color: #102920; border: none; }
QPushButton#primary:hover { background: #87e5c9; }
QPushButton#reset { border-color: #80694a; color: #ebce9c; }
QLineEdit, QSpinBox, QComboBox, QKeySequenceEdit { background: #0b1620; border: 1px solid #304858;
    border-radius: 5px; padding: 5px; selection-background-color: #287d78; }
QComboBox QAbstractItemView { background: #172939; selection-background-color: #287d78; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #142735; color: #8da4b8; padding: 10px 19px; }
QTabBar::tab:selected { color: #65dab6; border-bottom: 2px solid #65dab6; }
QTableWidget { background: #0f1c28; border: 0; gridline-color: #1d3344; }
QTableWidget::item { padding: 4px; }
QTableWidget::item:selected { background: #255449; }
QHeaderView::section { background: #0f1c28; color: #8da4b8; border: 0; padding: 5px; }
QGroupBox { border: 1px solid #304858; border-radius: 7px; margin-top: 12px; padding-top: 15px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #8da4b8; }
QToolTip { color: #eef5f8; background: #1d3344; border: 1px solid #65dab6; padding: 6px; }
QScrollArea { border: none; }
QCheckBox { spacing: 5px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #557082; border-radius: 3px; background: #0b1620; }
QCheckBox::indicator:checked { background: #65dab6; border-color: #65dab6; }
QSplitter::handle { background: #1d3344; width: 2px; }
QScrollBar:vertical { background: #0f1c28; width: 7px; margin: 0; }
QScrollBar:horizontal { background: #0f1c28; height: 7px; margin: 0; }
QScrollBar::handle { background: #304858; border-radius: 3px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""
