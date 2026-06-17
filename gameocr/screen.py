from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import List, Optional, Tuple

import mss
import numpy as np
from PyQt5.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QApplication, QWidget

try:
    import win32con
    import win32gui
    import win32process
    import win32ui
except ImportError:  # pragma: no cover - exercised only when pywin32 is unavailable
    win32con = None
    win32gui = None
    win32process = None
    win32ui = None


RectTuple = Tuple[int, int, int, int]


def window_capture_dependency_error() -> str:
    missing_modules = []
    if win32gui is None:
        missing_modules.append("win32gui")
    if win32ui is None:
        missing_modules.append("win32ui")
    if win32con is None:
        missing_modules.append("win32con")
    if missing_modules:
        return f"缺少 pywin32 组件（{', '.join(missing_modules)}），无法枚举/截取目标窗口；请执行 python -m pip install pywin32"
    return ""


@dataclass
class Screenshot:
    image: np.ndarray
    rect: RectTuple


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    rect: RectTuple
    class_name: str = ""
    process_id: int = 0

    @property
    def selector(self) -> str:
        """Stable-enough value stored in config for later capture.

        Prefer a real title for readability/backward compatibility. Untitled game
        windows are represented by hwnd; the handle is session-scoped and may need
        reselecting after the game restarts.
        """

        return self.title or f"hwnd:{self.hwnd}"

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.class_name:
            return f"[无标题] {self.class_name} #{self.hwnd}"
        return f"[无标题窗口] #{self.hwnd}"

    @property
    def label(self) -> str:
        x, y, w, h = self.rect
        meta = []
        if self.class_name:
            meta.append(self.class_name)
        if self.process_id:
            meta.append(f"PID {self.process_id}")
        suffix = f" | {' / '.join(meta)}" if meta else ""
        return f"{self.display_title}  ({w}x{h} @ {x},{y}){suffix}"


def capture_fullscreen() -> Screenshot:
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        raw = sct.grab(monitor)
        image = _mss_to_rgb_array(raw)
        return Screenshot(image=image, rect=(monitor["left"], monitor["top"], monitor["width"], monitor["height"]))


def capture_region(rect: RectTuple) -> Screenshot:
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        raise ValueError("选区为空")
    with mss.mss() as sct:
        raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
        image = _mss_to_rgb_array(raw)
        return Screenshot(image=image, rect=rect)


def list_capture_windows() -> List[WindowInfo]:
    """Return visible top-level windows that are reasonable OCR capture targets.

    Some games expose no normal window title, especially DirectX/Vulkan wrappers,
    launchers, or anti-cheat protected windows. Keep those candidates by using the
    window class name and hwnd, otherwise the GUI may show zero selectable windows
    even when the game is already open.
    """

    if win32gui is None:
        return []

    windows: List[WindowInfo] = []
    seen: set[int] = set()

    def callback(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindow(hwnd):
                return True
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            rect = _window_rect(hwnd)
            if rect is None:
                return True
            _, _, width, height = rect
            if width < 40 or height < 40:
                return True
            title = _safe_window_text(hwnd)
            class_name = _safe_class_name(hwnd)
            if not title and not class_name:
                return True
            if hwnd in seen:
                return True
            seen.add(hwnd)
            windows.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    rect=rect,
                    class_name=class_name,
                    process_id=_safe_process_id(hwnd),
                )
            )
        except Exception:
            return True
        return True

    win32gui.EnumWindows(callback, None)
    windows.sort(key=lambda item: (0 if item.title else 1, item.display_title.lower()))
    return windows


def capture_window_by_title(title: str) -> Screenshot:
    title = (title or "").strip()
    if not title:
        raise ValueError("未选择目标窗口")
    hwnd = find_window_by_title(title)
    if hwnd is None:
        raise RuntimeError(f"未找到目标窗口: {title}")
    return capture_window(hwnd)


def capture_window_region_by_title(title: str, region: RectTuple) -> Screenshot:
    window_shot = capture_window_by_title(title)
    ix, iy, iw, ih = _intersect_rect(window_shot.rect, region)
    if iw <= 0 or ih <= 0:
        raise ValueError("选区与目标窗口无交集")
    wx, wy, _, _ = window_shot.rect
    crop = window_shot.image[iy - wy : iy - wy + ih, ix - wx : ix - wx + iw]
    return Screenshot(image=np.ascontiguousarray(crop), rect=(ix, iy, iw, ih))


def get_window_rect_by_title(title: str) -> Optional[RectTuple]:
    title = (title or "").strip()
    if not title:
        return None
    hwnd = find_window_by_title(title)
    if hwnd is None:
        return None
    return _window_rect(hwnd)


def find_window_by_title(title: str) -> Optional[int]:
    if win32gui is None:
        raise RuntimeError("后台窗口截图需要安装 pywin32")
    title = title.strip()
    if not title:
        return None
    if title.lower().startswith("hwnd:"):
        try:
            hwnd = int(title.split(":", 1)[1])
        except ValueError:
            return None
        if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            return hwnd
        return None

    exact_match: Optional[int] = None
    partial_match: Optional[int] = None
    lowered_title = title.lower()

    def callback(hwnd: int, _: object) -> bool:
        nonlocal exact_match, partial_match
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            window_title = _safe_window_text(hwnd)
            class_name = _safe_class_name(hwnd)
            candidate_names = [
                window_title,
                class_name,
                f"[无标题] {class_name} #{hwnd}" if class_name else "",
                f"[无标题窗口] #{hwnd}",
            ]
            for candidate in candidate_names:
                if not candidate:
                    continue
                if candidate == title:
                    exact_match = hwnd
                    return False
                if partial_match is None and lowered_title in candidate.lower():
                    partial_match = hwnd
        except Exception:
            return True
        return True

    win32gui.EnumWindows(callback, None)
    return exact_match or partial_match


def is_target_window_foreground(title: str) -> bool:
    """Return whether the configured capture target is currently foreground.

    Real-time translation should pause when the game/window is not the active
    foreground window, otherwise stale overlays may remain visible while the user
    is interacting with another application.
    """

    title = (title or "").strip()
    if not title:
        return True
    hwnd = find_window_by_title(title)
    if hwnd is None:
        return False
    return is_window_foreground(hwnd)


def is_window_foreground(hwnd: int) -> bool:
    if win32gui is None:
        raise RuntimeError("前台窗口检测需要安装 pywin32")
    if not hwnd:
        return False

    try:
        foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        return False
    if not foreground_hwnd:
        return False
    if foreground_hwnd == hwnd:
        return True

    try:
        root_flag = getattr(win32con, "GA_ROOT", 2) if win32con is not None else 2
        if int(win32gui.GetAncestor(foreground_hwnd, root_flag) or 0) == int(win32gui.GetAncestor(hwnd, root_flag) or 0):
            return True
    except Exception:
        pass

    foreground_pid = _safe_process_id(foreground_hwnd)
    target_pid = _safe_process_id(hwnd)
    return bool(foreground_pid and target_pid and foreground_pid == target_pid)


def capture_window(hwnd: int) -> Screenshot:
    if win32gui is None or win32ui is None or win32con is None:
        raise RuntimeError("后台窗口截图需要安装 pywin32")
    if not win32gui.IsWindow(hwnd):
        raise RuntimeError("目标窗口句柄无效")

    rect = _window_rect(hwnd)
    if rect is None:
        raise RuntimeError("目标窗口尺寸无效")
    left, top, width, height = rect

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise RuntimeError("无法获取目标窗口 DC")

    src_dc = None
    mem_dc = None
    bitmap = None
    try:
        src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(src_dc, width, height)
        mem_dc.SelectObject(bitmap)

        result = _print_window(hwnd, mem_dc.GetSafeHdc())
        if result != 1:
            raise RuntimeError("PrintWindow 截图失败，目标窗口可能使用了受保护渲染或最小化")

        bmp_info = bitmap.GetInfo()
        bmp_bytes = bitmap.GetBitmapBits(True)
        image = _bitmap_bytes_to_rgb_array(bmp_bytes, int(bmp_info["bmWidth"]), int(bmp_info["bmHeight"]))
        return Screenshot(image=image, rect=(left, top, width, height))
    finally:
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if mem_dc is not None:
            mem_dc.DeleteDC()
        if src_dc is not None:
            src_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def _mss_to_rgb_array(raw) -> np.ndarray:
    """Convert an mss screenshot to a contiguous RGB numpy array without PIL/disk IO."""

    return np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)


def _bitmap_bytes_to_rgb_array(bmp_bytes: bytes, width: int, height: int) -> np.ndarray:
    """Convert Windows BGRX bitmap memory directly to a contiguous RGB array."""

    bgrx = np.frombuffer(bmp_bytes, dtype=np.uint8).reshape(height, width, 4)
    return np.ascontiguousarray(bgrx[:, :, :3][:, :, ::-1])


def _print_window(hwnd: int, hdc: int) -> int:
    flags = 0x00000002  # PW_RENDERFULLCONTENT, available on Windows 8+
    try:
        return int(ctypes.windll.user32.PrintWindow(hwnd, hdc, flags))
    except Exception:
        return int(ctypes.windll.user32.PrintWindow(hwnd, hdc, 0))


def _window_rect(hwnd: int) -> Optional[RectTuple]:
    if win32gui is None:
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = max(0, right - left)
    height = max(0, bottom - top)
    if width <= 0 or height <= 0:
        return None
    return (left, top, width, height)


def _safe_window_text(hwnd: int) -> str:
    if win32gui is None:
        return ""
    try:
        return win32gui.GetWindowText(hwnd).strip()
    except Exception:
        return ""


def _safe_class_name(hwnd: int) -> str:
    if win32gui is None:
        return ""
    try:
        return win32gui.GetClassName(hwnd).strip()
    except Exception:
        return ""


def _safe_process_id(hwnd: int) -> int:
    if win32process is None:
        return 0
    try:
        _, process_id = win32process.GetWindowThreadProcessId(hwnd)
        return int(process_id or 0)
    except Exception:
        return 0


def _intersect_rect(first: RectTuple, second: RectTuple) -> RectTuple:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    return (left, top, max(0, right - left), max(0, bottom - top))


class RegionSelector(QWidget):
    selected = pyqtSignal(tuple)
    cancelled = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self.start_point: Optional[QPoint] = None
        self.current_rect = QRect()
        desktop_rect = QApplication.desktop().geometry()
        self.setGeometry(desktop_rect)

    def begin(self) -> None:
        self.start_point = None
        self.current_rect = QRect()
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self.start_point = event.globalPos()
            self.current_rect = QRect(self.start_point, self.start_point)
            self.update()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self.start_point is not None:
            self.current_rect = QRect(self.start_point, event.globalPos()).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self.start_point is not None:
            rect = QRect(self.start_point, event.globalPos()).normalized()
            self.hide()
            if rect.width() < 4 or rect.height() < 4:
                self.cancelled.emit()
            else:
                self.selected.emit((rect.x(), rect.y(), rect.width(), rect.height()))
            self.start_point = None
            self.current_rect = QRect()
            self.update()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.cancelled.emit()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        painter.setPen(QPen(QColor(255, 255, 255), 1, Qt.SolidLine))
        painter.setFont(QFont("Microsoft YaHei UI", 18, QFont.Bold))
        painter.drawText(30, 48, "拖拽鼠标框选 OCR 翻译区域；按 ESC 取消")
        painter.setFont(QFont("Microsoft YaHei UI", 11))
        painter.drawText(32, 78, "每次触发选区翻译都会重新框选；可在主界面控制是否显示红色选区边框。")
        if not self.current_rect.isNull():
            local_rect = QRect(
                self.current_rect.x() - self.geometry().x(),
                self.current_rect.y() - self.geometry().y(),
                self.current_rect.width(),
                self.current_rect.height(),
            )
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(local_rect, QColor(0, 0, 0, 0))
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor(0, 180, 255), 2, Qt.SolidLine))
            painter.drawRect(local_rect)
            painter.setPen(QPen(QColor(255, 255, 255), 1, Qt.DashLine))
            painter.drawText(local_rect.topLeft() + QPoint(8, 18), f"{local_rect.width()} x {local_rect.height()}")