from __future__ import annotations

from typing import Dict

from PyQt5.QtCore import QObject, pyqtSignal

from .config import normalize_hotkey


class HotkeyManager(QObject):
    hotkey_pressed = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._listener = None
        self._bindings: Dict[str, str] = {}

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
        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"无法导入 pynput: {exc}")
            return False

        self._bindings = non_empty_bindings
        pynput_map = {
            to_pynput_hotkey(hotkey): _emit_action(self, action)
            for action, hotkey in non_empty_bindings.items()
        }
        try:
            self._listener = keyboard.GlobalHotKeys(pynput_map)
            self._listener.start()
            return True
        except Exception as exc:  # noqa: BLE001
            self._listener = None
            self.error.emit(f"注册全局热键失败，可能存在冲突: {exc}")
            return False


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