import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtGui import QKeySequence


def parse_hotkey(sequence: str) -> tuple[int, int]:
    parts = sequence.upper().replace(" ", "").split("+")
    modifiers = 0x4000  # MOD_NOREPEAT
    flags = {"CTRL": 2, "ALT": 1, "SHIFT": 4, "META": 8, "WIN": 8}
    for item in parts[:-1]:
        if item not in flags:
            raise ValueError("Поддерживаются Ctrl, Alt, Shift, Win")
        modifiers |= flags[item]
    key = parts[-1]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key)
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError("Выберите букву, цифру или F1–F24")
    return modifiers, vk


class GlobalHotkeys(QAbstractNativeEventFilter):
    def __init__(self, application):
        super().__init__()
        self.application = application
        self.callbacks = {}
        self.api = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
        if self.api:
            self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        application.installNativeEventFilter(self)

    def register(self, bindings):
        parsed = [
            (identifier, parse_hotkey(sequence), callback)
            for identifier, (sequence, callback) in enumerate(bindings, 1)
        ]
        if len({keys for _, keys, _ in parsed}) != len(parsed):
            raise ValueError("Действиям нужны разные сочетания клавиш")
        self.clear()
        if not self.api:
            raise RuntimeError("Глобальные горячие клавиши доступны только в Windows")
        for identifier, (modifiers, vk), callback in parsed:
            if not self.api.RegisterHotKey(None, identifier, modifiers, vk):
                self.clear()
                raise RuntimeError("Горячая клавиша занята другим приложением. Выберите другое сочетание.")
            self.callbacks[identifier] = callback

    def nativeEventFilter(self, event_type, message):
        if self.api and bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam in self.callbacks:
                self.callbacks[msg.wParam]()
                return True, 0
        return False, 0

    def clear(self):
        if self.api:
            for identifier in self.callbacks:
                self.api.UnregisterHotKey(None, identifier)
        self.callbacks.clear()

    def close(self):
        self.clear()
        self.application.removeNativeEventFilter(self)


def portable_text(editor):
    return editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
