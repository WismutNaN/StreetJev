import ctypes
import hashlib
import io
import os
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime

import mss
from PIL import Image, ImageGrab, ImageOps

from streetjev.domain.evidence import Frame


@dataclass(frozen=True)
class CaptureTarget:
    name: str
    rectangle: tuple[int, int, int, int] | None = None
    hwnd: int | None = None


def prepare_image(image: Image.Image, source: str) -> Frame:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width < 32 or image.height < 32:
        raise ValueError("Область должна быть не меньше 32 × 32 пикселей")
    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    fingerprint = image.convert("L").resize((32, 32), Image.Resampling.BILINEAR).tobytes()
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    now = datetime.now(UTC).isoformat()
    identity = hashlib.sha256(output.getvalue() + now.encode()).hexdigest()
    return Frame(identity, output.getvalue(), fingerprint, now, source)


def windows_api():
    if os.name != "nt":
        raise RuntimeError("Захват отдельного окна доступен только в Windows")
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.IsWindow.argtypes = [wintypes.HWND]
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.IsIconic.argtypes = [wintypes.HWND]
    api.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    return api


def list_windows() -> list[CaptureTarget]:
    api = windows_api()
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        process = wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
        size = api.GetWindowTextLengthW(hwnd)
        if process.value != os.getpid() and size and api.IsWindowVisible(hwnd) and not api.IsIconic(hwnd):
            title = ctypes.create_unicode_buffer(size + 1)
            api.GetWindowTextW(hwnd, title, size + 1)
            result.append(CaptureTarget(title.value, hwnd=int(hwnd)))
        return True

    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows(visit, 0)
    return sorted(result, key=lambda target: target.name.casefold())


def window_rectangle(hwnd: int) -> tuple[int, int, int, int]:
    """Физическая клиентская область окна; не зависит от имени/масштаба монитора Qt."""
    api = windows_api()
    api.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    rect, origin = wintypes.RECT(), wintypes.POINT()
    if not api.GetClientRect(hwnd, ctypes.byref(rect)) or not api.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise RuntimeError("Не удалось определить координаты окна")
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def monitor_rectangles() -> dict[str, tuple[int, int, int, int]]:
    """Физические координаты по имени QScreen, включая mixed-DPI мониторы."""
    if os.name != "nt":
        return {}
    api = ctypes.WinDLL("user32", use_last_error=True)

    class MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("monitor", wintypes.RECT),
            ("work", wintypes.RECT),
            ("flags", wintypes.DWORD),
            ("device", wintypes.WCHAR * 32),
        ]

    result = {}
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )
    api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]

    @callback_type
    def visit(handle, _dc, _rect, _data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if api.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.monitor
            result[info.device] = (r.left, r.top, r.right - r.left, r.bottom - r.top)
        return True

    api.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, callback_type, wintypes.LPARAM]
    api.EnumDisplayMonitors(None, None, visit, 0)
    return result


class ScreenCapture:
    def __init__(self, target: CaptureTarget):
        self.target = target

    def capture(self) -> Frame:
        if self.target.hwnd is not None:
            api = windows_api()
            if not api.IsWindow(self.target.hwnd) or api.IsIconic(self.target.hwnd):
                raise RuntimeError("Выбранное окно закрыто или свёрнуто. Выберите его заново.")
            image = ImageGrab.grab(window=self.target.hwnd)
            if all(high == 0 for _, high in image.convert("RGB").getextrema()):
                raise RuntimeError("Окно вернуло чёрный кадр. Используйте захват области экрана.")
        elif self.target.rectangle:
            left, top, width, height = self.target.rectangle
            if width < 32 or height < 32:
                raise ValueError("Слишком маленькая область захвата")
            with mss.mss() as grabber:
                shot = grabber.grab({"left": left, "top": top, "width": width, "height": height})
                image = Image.frombytes("RGB", shot.size, shot.rgb)
        else:
            raise RuntimeError("Сначала выберите область или окно")
        return prepare_image(image, self.target.name)
