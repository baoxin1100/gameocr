from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

_DLL_HANDLES: list[object] = []
_PRELOADED_DLLS: list[object] = []


def _add_dll_directory(path: Path) -> None:
    if not path.exists():
        return
    try:
        if hasattr(os, "add_dll_directory"):
            _DLL_HANDLES.append(os.add_dll_directory(str(path)))
    except OSError:
        pass


def _preload(path: Path) -> None:
    if not path.exists():
        return
    try:
        _PRELOADED_DLLS.append(ctypes.WinDLL(str(path)))
    except OSError:
        pass


if os.name == "nt" and getattr(sys, "frozen", False):
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))

    # Keep ORT/OpenVINO native folders ahead of broad extraction roots. In large
    # PyInstaller one-file bundles there may be multiple CRT/native DLL copies;
    # explicitly prioritizing the provider folders prevents Windows from picking
    # an incompatible DLL while importing onnxruntime_pybind11_state.
    ordered_dirs = [
        base / "onnxruntime" / "capi",
        base / "openvino" / "libs",
        base / "PyQt5" / "Qt5" / "bin",
        base,
    ]

    for directory in ordered_dirs:
        _add_dll_directory(directory)

    existing_path = os.environ.get("PATH", "")
    prepend = os.pathsep.join(str(directory) for directory in ordered_dirs if directory.exists())
    if prepend:
        os.environ["PATH"] = prepend + (os.pathsep + existing_path if existing_path else "")

    # Preload the two core libraries from their bundled locations. This is safe
    # if they are already loaded and makes the import deterministic for the ORT
    # OpenVINO provider in one-file mode.
    _preload(base / "openvino" / "libs" / "openvino.dll")
    _preload(base / "onnxruntime" / "capi" / "onnxruntime.dll")