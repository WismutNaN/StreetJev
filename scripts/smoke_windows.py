"""Реальный PrintWindow и mss проверяются только на собственном тестовом окне."""

import ctypes
import io
import os
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from streetjev.infrastructure.capture import CaptureTarget, ScreenCapture, window_rectangle
from streetjev.presentation.hotkeys import GlobalHotkeys


def main():
    if os.name != "nt":
        raise SystemExit("Этот smoke предназначен для Windows")
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    app = QApplication([])
    widget = QLabel("StreetJev · тест захвата\nЭто собственное тестовое окно приложения")
    widget.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    widget.setStyleSheet("background: #158060; color: white; font: 18px 'Segoe UI';")
    widget.setFixedSize(480, 320)
    widget.move(80, 80)
    widget.show()
    for _ in range(15):
        app.processEvents()
        time.sleep(0.02)
    output = Path("artifacts/windows")
    output.mkdir(parents=True, exist_ok=True)
    try:
        native = ScreenCapture(CaptureTarget("Собственное тестовое окно", hwnd=int(widget.winId()))).capture()
        image = Image.open(io.BytesIO(native.jpeg))
        pixel = image.getpixel((20, 20))
        assert pixel[1] > 90 and pixel[1] > pixel[0] * 2, pixel
        (output / "window.jpg").write_bytes(native.jpeg)
        x, y, _w, _h = window_rectangle(int(widget.winId()))
        rectangle = (x + 10, y + 10, 80, 80)
        area = ScreenCapture(CaptureTarget("Внутри тестового окна", rectangle=rectangle)).capture()
        pixel = Image.open(io.BytesIO(area.jpeg)).getpixel((20, 20))
        assert pixel[1] > 90 and pixel[1] > pixel[0] * 2, pixel
        hotkeys = GlobalHotkeys(app)
        events = []
        hotkeys.register(
            [("Ctrl+Alt+F11", lambda: events.append("capture")), ("Ctrl+Alt+F12", lambda: events.append("reset"))]
        )
        try:
            thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            assert ctypes.windll.user32.PostThreadMessageW(thread_id, 0x0312, 1, 0)
            assert ctypes.windll.user32.PostThreadMessageW(thread_id, 0x0312, 2, 0)
            for _ in range(5):
                app.processEvents()
            assert events == ["capture", "reset"], events
        finally:
            hotkeys.close()
        print("Windows smoke: PrintWindow, область mss, физические координаты и WM_HOTKEY — OK")
    finally:
        widget.close()
        app.processEvents()


if __name__ == "__main__":
    main()
