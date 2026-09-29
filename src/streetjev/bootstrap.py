import argparse
import ctypes
import logging
import os
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="StreetJev — карта вероятностей по снимкам экрана")
    parser.add_argument("--demo", action="store_true", help="синтетические наблюдения без модели и захвата экрана")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="каталог журнала и настроек")
    args = parser.parse_args(argv)
    if os.name == "nt":
        # До первого вызова захвата и создания окон: согласованные физические координаты.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    from PySide6.QtWidgets import QApplication

    from streetjev.infrastructure.catalog import load_geography
    from streetjev.infrastructure.storage import SqliteRoundRepository
    from streetjev.presentation.style import STYLE
    from streetjev.presentation.window import MainWindow

    args.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=args.data_dir / "streetjev.log",
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = QApplication(sys.argv[:1])
    app.setApplicationName("StreetJev")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    catalog, geography = load_geography()
    repository = SqliteRoundRepository(args.data_dir / "rounds.sqlite3")
    window = MainWindow(catalog, geography, repository, args.data_dir, args.demo)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
