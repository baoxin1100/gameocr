from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Callable, Dict, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

from .config import normalize_hotkey


class HotkeyManager(QObject):
    hotkey_pressed = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._listener = None
        self._bindings: Dict[str, str] = {}
        self._backend = ""

    def start(
        self,
        trigger_hotkey: str,
        region_hotkey: str = "",
        font_increase_hotkey: str = "",
        font_decrease_hotkey: str = "",
    ) -> bool:
        return self.update_bindings(trigger_hotkey, region_hotkey, font_increase_hotkey, font_decrease_hotkey)

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._backend = ""

    def update_bindings(
        self,
        trigger_hotkey: str,
        region_hotkey: str = "",
        font_increase_hotkey: str = "",
        font_decrease_hotkey: str = "",
    ) -> bool:
        bindings = {
            "trigger": normalize_hotkey(trigger_hotkey),
            "font_increase": normalize_hotkey(font_increase_hotkey),
            "font_decrease": normalize_hotkey(font_decrease_hotkey),
        }
        if region_hotkey:
            bindings["region"] = normalize_hotkey(region_hotkey)

        if not bindings["trigger"]:
            self.error.emit("翻译触发热键不能为空")
            return False

        non_empty_bindings = {action: hotkey for action, hotkey in bindings.items() if hotkey}
        duplicates = _duplicate_hotkeys(non_empty_bindings)
        if duplicates:
            self.error.emit(f"热键冲突，请为不同功能设置不同快捷键: {', '.join(duplicates)}")
            return False

        self.stop()
        self._bindings = non_empty_bindings

        if sys.platform.startswith("win"):
            try:
                listener = Win32HotkeyListener(non_empty_bindings, self._emit_action)
                listener.start()
                self._listener = listener
                self._backend = "win32"
                return True
            except Exception as exc:  # noqa: BLE001
                self._listener = None
                self._backend = ""
                self.error.emit(f"Windows 原生热键注册失败，正在尝试兼容监听: {exc}")

        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"无法导入 pynput: {exc}")
            return False

        pynput_map = {
            to_pynput_hotkey(hotkey): _emit_action(self, action)
            for action, hotkey in non_empty_bindings.items()
        }
        try:
            self._listener = keyboard.GlobalHotKeys(pynput_map)
            self._listener.start()
            self._backend = "pynput"
            return True
        except Exception as exc:  # noqa: BLE001
            self._listener = None
            self._backend = ""
            self.error.emit(f"注册全局热键失败，可能存在冲突: {exc}")
            return False

    def _emit_action(self, action: str) -> None:
        self.hotkey_pressed.emit(action)


class Win32HotkeyListener:
    """Register process-wide hotkeys through the Windows message queue.

    pynput hooks can be blocked or behave inconsistently in some fullscreen game
    input stacks. RegisterHotKey is the native system-wide API and works without a
    focused application window, so it is the preferred backend on Windows.
    """

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000

    def __init__(self, bindings: Dict[str, str], callback: Callable[[str], None]) -> None:
        self.bindings = dict(bindings)
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._thread_id = 0
        self._error: BaseException | None = None
        self._id_to_action: Dict[int, str] = {}

    def start(self) -> None:
        if not self.bindings:
            raise ValueError("没有可注册的热键")
        self._thread = threading.Thread(target=self._run, name="GameOCRWin32Hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3.0):
            self.stop()
            raise RuntimeError("等待 Windows 热键监听线程启动超时")
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive() and self._thread_id:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass
            thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = 0
        self._ready.clear()

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        registered_ids: list[int] = []

        try:
            for index, (action, hotkey) in enumerate(self.bindings.items(), start=1):
                modifiers, vk_code = parse_win32_hotkey(hotkey)
                hotkey_id = index
                ok = user32.RegisterHotKey(None, hotkey_id, modifiers | self.MOD_NOREPEAT, vk_code)
                if not ok:
                    err = ctypes.get_last_error()
                    raise OSError(err, f"注册热键 {hotkey!r} 失败，可能已被其他程序占用")
                registered_ids.append(hotkey_id)
                self._id_to_action[hotkey_id] = action

            self._ready.set()
            msg = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    err = ctypes.get_last_error()
                    raise OSError(err, "Windows 热键消息循环异常")
                if msg.message == self.WM_HOTKEY:
                    action = self._id_to_action.get(int(msg.wParam))
                    if action:
                        self.callback(action)
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._ready.set()
        finally:
            for hotkey_id in registered_ids:
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:
                    pass
            self._id_to_action.clear()


def _duplicate_hotkeys(bindings: Dict[str, str]) -> list[str]:
    seen: Dict[str, str] = {}
    duplicates = []
    for action, hotkey in bindings.items():
        previous = seen.get(hotkey)
        if previous is not None:
            duplicates.append(hotkey)
        else:
            seen[hotkey] = action
    return duplicates


def _emit_action(manager: HotkeyManager, action: str):
    return lambda: manager.hotkey_pressed.emit(action)


def to_pynput_hotkey(value: str) -> str:
    parts = [part.strip().lower() for part in normalize_hotkey(value).split("+") if part.strip()]
    converted = []
    modifiers = {"ctrl", "alt", "shift", "cmd"}
    special = {
        "enter",
        "space",
        "tab",
        "escape",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "page_up",
        "page_down",
        "up",
        "down",
        "left",
        "right",
    }
    for part in parts:
        if part in modifiers or part in special or (part.startswith("f") and part[1:].isdigit()):
            converted.append(f"<{part}>")
        elif len(part) == 1:
            converted.append(part)
        else:
            converted.append(f"<{part}>")
    return "+".join(converted)


def parse_win32_hotkey(value: str) -> Tuple[int, int]:
    parts = [part.strip().lower() for part in normalize_hotkey(value).split("+") if part.strip()]
    if not parts:
        raise ValueError("热键不能为空")

    modifiers = 0
    key_parts: list[str] = []
    for part in parts:
        if part == "ctrl":
            modifiers |= Win32HotkeyListener.MOD_CONTROL
        elif part == "alt":
            modifiers |= Win32HotkeyListener.MOD_ALT
        elif part == "shift":
            modifiers |= Win32HotkeyListener.MOD_SHIFT
        elif part == "cmd":
            modifiers |= Win32HotkeyListener.MOD_WIN
        else:
            key_parts.append(part)

    if len(key_parts) != 1:
        raise ValueError(f"Windows 热键必须包含且仅包含一个主按键: {value!r}")

    key = key_parts[0]
    if len(key) == 1 and "a" <= key <= "z":
        return modifiers, ord(key.upper())
    if len(key) == 1 and "0" <= key <= "9":
        return modifiers, ord(key)

    vk_code = _WIN32_KEY_CODES.get(key)
    if vk_code is None:
        raise ValueError(f"不支持的 Windows 热键按键: {key}")
    return modifiers, vk_code


_WIN32_KEY_CODES = {
    **{f"f{index}": 0x70 + index - 1 for index in range(1, 25)},
    "enter": 0x0D,
    "space": 0x20,
    "tab": 0x09,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "page_up": 0x21,
    "page_down": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}