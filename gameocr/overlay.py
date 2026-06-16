from __future__ import annotations

import ctypes
import math
import sys
from typing import List, Sequence, Tuple

from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen
from PyQt5.QtWidgets import QApplication, QLabel, QWidget

from .config import (
    TRANSLATION_FONT_SIZE_DEFAULT,
    TRANSLATION_FONT_SIZE_MAX,
    TRANSLATION_FONT_SIZE_MIN,
    TRANSLATION_THEME_DEFAULT,
    TRANSLATION_THEME_LABELS,
)
from .ocr import OCRItem


WDA_EXCLUDEFROMCAPTURE = 0x00000011
_capture_exclusion_supported: bool | None = None


def _set_excluded_from_capture(widget: QWidget) -> bool:
    """Exclude a top-level PyQt window from Windows screen capture if supported.

    Windows 10 2004+ and Windows 11 support WDA_EXCLUDEFROMCAPTURE via
    SetWindowDisplayAffinity(). When it works, desktop screenshots/recording can
    omit the overlay while the user still sees it, so realtime OCR no longer has
    to blink translation windows off before every capture.
    """

    global _capture_exclusion_supported

    if sys.platform != "win32":
        _capture_exclusion_supported = False
        return False

    try:
        hwnd = int(widget.winId())
        if hwnd <= 0:
            _capture_exclusion_supported = False
            return False
        result = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:  # noqa: BLE001
        _capture_exclusion_supported = False
        return False

    if result:
        _capture_exclusion_supported = True
        return True

    if _capture_exclusion_supported is None:
        _capture_exclusion_supported = False
    return False


def is_capture_exclusion_supported() -> bool:
    return _capture_exclusion_supported is True


TRANSLATION_WIDTH_EXTRA_CHARS = 4
TRANSLATION_WIDTH_MIN_EXTRA_PX = 32
TRANSLATION_WIDTH_MAX_EXTRA_PX = 96

TRANSLATION_THEME_STYLES = {
    "classic": {
        "text": "#ffffff",
        "background": "rgba(20, 20, 20, 178)",
        "border": "rgba(0, 190, 255, 190)",
    },
    "amber": {
        "text": "#fff4d6",
        "background": "rgba(34, 24, 8, 190)",
        "border": "rgba(255, 176, 0, 210)",
    },
    "blue": {
        "text": "#e8f4ff",
        "background": "rgba(5, 22, 45, 190)",
        "border": "rgba(80, 170, 255, 220)",
    },
    "green": {
        "text": "#eaffea",
        "background": "rgba(8, 36, 20, 188)",
        "border": "rgba(80, 220, 130, 210)",
    },
    "light": {
        "text": "#111111",
        "background": "rgba(255, 250, 230, 220)",
        "border": "rgba(120, 90, 40, 180)",
    },
    "purple": {
        "text": "#fff0ff",
        "background": "rgba(38, 12, 52, 190)",
        "border": "rgba(220, 105, 255, 220)",
    },
}


class TranslationBubble(QWidget):
    def __init__(self, text: str, x: int, y: int, max_width: int = 720) -> None:
        super().__init__(None)
        self._max_width = max_width
        self._preferred_source_width = 0
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        if hasattr(Qt, "WindowTransparentForInput"):
            self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        _set_excluded_from_capture(self)

        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setMargin(8)
        self.label.setMinimumWidth(0)
        self.label.setMaximumWidth(max_width)
        self.label.setFont(QFont("Microsoft YaHei UI", TRANSLATION_FONT_SIZE_DEFAULT))
        self.set_font_size(TRANSLATION_FONT_SIZE_DEFAULT)
        self.set_theme(TRANSLATION_THEME_DEFAULT)
        self._resize_to_content()
        self.move_to_fit(x, y)

    def set_font_size(self, font_size: int) -> None:
        font_size = max(TRANSLATION_FONT_SIZE_MIN, min(TRANSLATION_FONT_SIZE_MAX, int(font_size)))
        self.label.setFont(QFont("Microsoft YaHei UI", font_size))
        self._resize_to_content()

    def set_theme(self, theme: str) -> None:
        style = TRANSLATION_THEME_STYLES.get(theme, TRANSLATION_THEME_STYLES[TRANSLATION_THEME_DEFAULT])
        self.label.setStyleSheet(
            f"""
            QLabel {{
                color: {style["text"]};
                background-color: {style["background"]};
                border: 1px solid {style["border"]};
                border-radius: 6px;
            }}
            """
        )
        palette = self.label.palette()
        palette.setColor(QPalette.WindowText, QColor(style["text"]))
        self.label.setPalette(palette)

    def set_preferred_width(self, width: int) -> None:
        self._preferred_source_width = max(1, int(width))
        self._resize_to_content()

    def update_text(self, text: str) -> None:
        self.label.setText(text)
        self._resize_to_content()

    def _resize_to_content(self) -> None:
        width_limit = self._content_width_limit()
        natural_width = min(self._natural_text_width(), width_limit)
        target_width = max(1, natural_width)

        self.label.setMinimumWidth(0)
        self.label.setMaximumWidth(width_limit)
        self.label.setFixedWidth(target_width)
        self.label.adjustSize()
        self.resize(target_width, self.label.sizeHint().height())

    def _content_width_limit(self) -> int:
        if not self._preferred_source_width:
            return self._max_width

        metrics = QFontMetrics(self.label.font())
        char_width = max(metrics.averageCharWidth(), metrics.horizontalAdvance("测"))
        return _expanded_translation_width_limit(self._preferred_source_width, self._max_width, char_width)

    def _natural_text_width(self) -> int:
        metrics = QFontMetrics(self.label.font())
        lines = self.label.text().splitlines() or [""]
        text_width = max(metrics.horizontalAdvance(line) for line in lines)
        return text_width + self.label.margin() * 2 + 4

    def update_content(self, text: str, x: int, y: int) -> None:
        self.update_text(text)
        self.move_to_fit(x, y)

    def move_to_fit(self, x: int, y: int) -> None:
        self.move(*_fit_point_to_screen(x, y, self.width(), self.height()))


class RegionBoxOverlay(QWidget):
    def __init__(self, rect: Tuple[int, int, int, int]) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        if hasattr(Qt, "WindowTransparentForInput"):
            self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        _set_excluded_from_capture(self)
        self.update_rect(rect)

    def update_rect(self, rect: Tuple[int, int, int, int]) -> None:
        x, y, width, height = rect
        self.setGeometry(int(x), int(y), max(1, int(width)), max(1, int(height)))
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        local_rect = self.rect().adjusted(1, 1, -2, -2)
        painter.setPen(QPen(QColor(255, 0, 0, 230), 2, Qt.SolidLine))
        painter.drawRect(local_rect)


class RealtimeStatusBubble(QWidget):
    def __init__(self, text: str = "实时翻译开启中") -> None:
        super().__init__(None)
        self.text = text
        self.text_font = QFont("Microsoft YaHei UI", 9)
        self.padding_x = 5
        self.padding_y = 4
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        if hasattr(Qt, "WindowTransparentForInput"):
            self.setWindowFlag(Qt.WindowTransparentForInput, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        _set_excluded_from_capture(self)

        self._resize_to_text()
        self.move_to_top_right()

    def update_text(self, text: str) -> None:
        if self.text == text:
            return
        self.text = text
        self._resize_to_text()

    def _resize_to_text(self) -> None:
        metrics = QFontMetrics(self.text_font)
        lines = self.text.splitlines() or [""]
        width = max(metrics.horizontalAdvance(line) for line in lines) + self.padding_x * 2 + 4
        height = metrics.height() * len(lines) + self.padding_y * 2 + 4
        self.resize(width, height)

    def move_to_top_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(20, 20)
            return
        rect = screen.availableGeometry()
        margin = 12
        self.move(rect.left() + rect.width() - self.width() - margin, rect.top() + margin)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self.text_font)

        metrics = QFontMetrics(self.text_font)
        text_x = self.padding_x + 2
        for line_index, line in enumerate(self.text.splitlines() or [""]):
            baseline = self.padding_y + 2 + metrics.ascent() + line_index * metrics.height()
            # Draw a very light 1px shadow instead of a stroked text path.
            # A thick path outline can visually dominate small 9pt text and make it
            # look black; this keeps the status indicator unmistakably white.
            painter.setPen(QColor(0, 0, 0, 150))
            painter.drawText(text_x + 1, baseline + 1, line)

            painter.setPen(QColor(255, 255, 255, 255))
            painter.drawText(text_x, baseline, line)


class OverlayManager:
    def __init__(self) -> None:
        self._windows: List[TranslationBubble] = []
        self._status_window: RealtimeStatusBubble | None = None
        self._status_active = False
        self._status_enabled = True
        self._latency_text = ""
        self._latency_enabled = True
        self._region_box: RegionBoxOverlay | None = None
        self._region_box_rect: Tuple[int, int, int, int] | None = None
        self._region_box_enabled = False
        self._translation_theme = TRANSLATION_THEME_DEFAULT
        self._translation_font_size = TRANSLATION_FONT_SIZE_DEFAULT

    def clear(self) -> None:
        for window in self._windows:
            self._destroy_window(window)
        self._windows.clear()

    def hide_all(self) -> None:
        for window in self._windows:
            window.hide()
        if self._status_window is not None:
            self._status_window.hide()
        if self._region_box is not None:
            self._region_box.hide()

    def show_all(self) -> None:
        for window in self._windows:
            window.show()
            window.raise_()
        self._sync_status_window()
        self.show_region_box()

    def capture_exclusion_supported(self) -> bool:
        return is_capture_exclusion_supported()

    def set_translation_theme(self, theme: str) -> None:
        if theme not in TRANSLATION_THEME_LABELS:
            theme = TRANSLATION_THEME_DEFAULT
        self._translation_theme = theme
        for window in self._windows:
            if hasattr(window, "set_theme"):
                window.set_theme(theme)

    def set_translation_font_size(self, font_size: int) -> None:
        font_size = max(TRANSLATION_FONT_SIZE_MIN, min(TRANSLATION_FONT_SIZE_MAX, int(font_size)))
        self._translation_font_size = font_size
        for window in self._windows:
            if hasattr(window, "set_font_size"):
                window.set_font_size(font_size)

    def set_realtime_status(self, active: bool, enabled: bool = True) -> None:
        self._status_active = active
        self._status_enabled = enabled
        self._sync_status_window()

    def set_latency_status(self, ocr_ms: float, translate_ms: float, total_ms: float, enabled: bool = True) -> None:
        self._latency_enabled = enabled
        if enabled:
            self._latency_text = f"OCR {ocr_ms:.0f} ms / 翻译 {translate_ms:.0f} ms / 总 {total_ms:.0f} ms"
        else:
            self._latency_text = ""
        self._sync_status_window()

    def set_region_box(self, rect: Tuple[int, int, int, int], enabled: bool = True) -> None:
        self._region_box_rect = rect
        self._region_box_enabled = enabled
        self.show_region_box()

    def show_region_box(self) -> None:
        if not self._region_box_enabled or self._region_box_rect is None:
            self.clear_region_box()
            return

        if self._region_box is None:
            self._region_box = RegionBoxOverlay(self._region_box_rect)
        else:
            self._region_box.update_rect(self._region_box_rect)
        self._region_box.show()
        self._region_box.raise_()

    def clear_region_box(self) -> None:
        self._region_box_enabled = False
        self._region_box_rect = None
        if self._region_box is not None:
            self._destroy_window(self._region_box)
            self._region_box = None

    def _sync_status_window(self) -> None:
        lines = []
        if self._status_active and self._status_enabled:
            lines.append("实时翻译开启中")
        if self._latency_enabled and self._latency_text:
            lines.append(self._latency_text)

        if not lines:
            if self._status_window is not None:
                self._destroy_window(self._status_window)
                self._status_window = None
            return

        text = "\n".join(lines)
        if self._status_window is None:
            self._status_window = RealtimeStatusBubble(text)
        else:
            self._status_window.update_text(text)
        self._status_window.move_to_top_right()
        self._status_window.show()
        self._status_window.raise_()

    def show_translations(self, items: Sequence[OCRItem], translations: Sequence[str]) -> None:
        visible_count = 0
        placed_rects: List[QRect] = []
        for item, translation in zip(items, translations):
            if not translation:
                continue
            x1, y1, x2, y2 = item.box
            source_width = max(1, int(x2 - x1))
            initial_x = max(0, x1)
            initial_y = max(0, y2 + 6)

            if visible_count < len(self._windows):
                bubble = self._windows[visible_count]
                if hasattr(bubble, "set_theme"):
                    bubble.set_theme(self._translation_theme)
                if hasattr(bubble, "set_font_size"):
                    bubble.set_font_size(self._translation_font_size)
                if hasattr(bubble, "set_preferred_width"):
                    bubble.set_preferred_width(source_width)
                bubble.update_text(translation)
            else:
                bubble = TranslationBubble(translation, initial_x, initial_y)
                if hasattr(bubble, "set_theme"):
                    bubble.set_theme(self._translation_theme)
                if hasattr(bubble, "set_font_size"):
                    bubble.set_font_size(self._translation_font_size)
                if hasattr(bubble, "set_preferred_width"):
                    bubble.set_preferred_width(source_width)
                self._windows.append(bubble)

            desired_x = max(0, int(round(((x1 + x2) - bubble.width()) / 2)))
            desired_y = initial_y
            x, y = _place_without_overlap(desired_x, desired_y, bubble.width(), bubble.height(), placed_rects)
            bubble.move(x, y)
            placed_rects.append(QRect(x, y, bubble.width(), bubble.height()).adjusted(-4, -4, 4, 4))

            bubble.show()
            bubble.raise_()
            visible_count += 1

        for window in self._windows[visible_count:]:
            self._destroy_window(window)
        del self._windows[visible_count:]

    def _destroy_window(self, window: QWidget) -> None:
        window.hide()
        window.close()
        window.deleteLater()

    def active_count(self) -> int:
        return len(self._windows)


def _expanded_translation_width_limit(source_width: int, max_width: int, char_width: int) -> int:
    source_width = max(1, int(source_width))
    max_width = max(1, int(max_width))
    char_width = max(1, int(math.ceil(char_width)))
    extra_width = char_width * TRANSLATION_WIDTH_EXTRA_CHARS
    extra_width = max(TRANSLATION_WIDTH_MIN_EXTRA_PX, min(extra_width, TRANSLATION_WIDTH_MAX_EXTRA_PX))
    return min(max_width, source_width + extra_width)


def _place_without_overlap(
    desired_x: int,
    desired_y: int,
    width: int,
    height: int,
    placed_rects: Sequence[QRect],
    margin: int = 6,
) -> Tuple[int, int]:
    rect = _screen_geometry_for_point(desired_x, desired_y)
    if rect is None or not placed_rects:
        return _fit_point_to_screen(desired_x, desired_y, width, height, margin)

    fitted_desired = _fit_point_to_screen(desired_x, desired_y, width, height, margin)
    candidates = [fitted_desired]

    # Try positions under/above existing bubbles first. This keeps translations
    # close to their OCR line while avoiding unreadable overlap.
    for placed in placed_rects:
        candidates.append(_fit_point_to_screen(desired_x, placed.bottom() + margin, width, height, margin))
        candidates.append(_fit_point_to_screen(desired_x, placed.top() - height - margin, width, height, margin))
        candidates.append(_fit_point_to_screen(placed.right() + margin, desired_y, width, height, margin))
        candidates.append(_fit_point_to_screen(placed.left() - width - margin, desired_y, width, height, margin))

    step = max(14, min(36, height // 2 or 14))
    for offset in range(step, max(rect.height(), rect.width()), step):
        candidates.append(_fit_point_to_screen(desired_x, desired_y + offset, width, height, margin))
        candidates.append(_fit_point_to_screen(desired_x, desired_y - offset, width, height, margin))
        if offset <= rect.width():
            candidates.append(_fit_point_to_screen(desired_x + offset, desired_y, width, height, margin))
            candidates.append(_fit_point_to_screen(desired_x - offset, desired_y, width, height, margin))

    unique_candidates = list(dict.fromkeys(candidates))
    for x, y in sorted(unique_candidates, key=lambda point: math.hypot(point[0] - desired_x, point[1] - desired_y)):
        candidate_rect = QRect(x, y, width, height).adjusted(-margin, -margin, margin, margin)
        if not any(candidate_rect.intersects(placed) for placed in placed_rects):
            return x, y

    return fitted_desired


def _fit_point_to_screen(x: int, y: int, width: int, height: int, margin: int = 4) -> Tuple[int, int]:
    rect = _screen_geometry_for_point(x, y)
    if rect is None:
        return max(0, x), max(0, y)

    min_x = rect.left() + margin
    min_y = rect.top() + margin
    max_x = rect.left() + rect.width() - width - margin
    max_y = rect.top() + rect.height() - height - margin

    if max_x < min_x:
        adjusted_x = min_x
    else:
        adjusted_x = min(max(x, min_x), max_x)

    if max_y < min_y:
        adjusted_y = min_y
    else:
        adjusted_y = min(max(y, min_y), max_y)

    return adjusted_x, adjusted_y


def _screen_geometry_for_point(x: int, y: int) -> QRect | None:
    app = QApplication.instance()
    if app is None:
        return None

    screen = QApplication.screenAt(QPoint(x, y))
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return None
    return screen.availableGeometry()