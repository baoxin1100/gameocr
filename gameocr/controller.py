from __future__ import annotations

import re
import time
import traceback
from copy import deepcopy
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal, pyqtSlot

from .config import AppConfig, OCR_RESOLUTION_ORIGINAL, OCR_TARGET_HEIGHTS, TRANSLATION_SCOPE_FULLSCREEN, TRANSLATION_SCOPE_REGION, TRIGGER_MODE_REALTIME
from .ocr import OCRItem, OCRRunInfo, create_ocr
from .overlay import OverlayManager
from .screen import (
    RectTuple,
    capture_fullscreen,
    capture_region,
    capture_window_by_title,
    capture_window_region_by_title,
    get_window_rect_by_title,
    is_target_window_foreground,
)
from .translation import TranslationOutput, create_translator


class WorkerSignals(QObject):
    captured = pyqtSignal(str, int)
    finished = pyqtSignal(str, int, object, object)
    log = pyqtSignal(str)


def merge_sentence_lines(items: Sequence[OCRItem]) -> List[OCRItem]:
    """Merge vertically adjacent OCR lines that look like one wrapped sentence.

    Games often render one sentence across two or more subtitle/dialogue rows.
    OCR returns those rows independently, which causes broken translations. This
    heuristic keeps unrelated UI labels separate while joining close, aligned
    rows when the previous row does not already end a sentence.
    """

    remaining = sorted(items, key=lambda item: (item.box[1], item.box[0]))
    groups: List[List[OCRItem]] = []

    while remaining:
        group = [remaining.pop(0)]
        while True:
            next_index = _find_next_sentence_line(group[-1], remaining)
            if next_index is None:
                break
            group.append(remaining.pop(next_index))
        groups.append(group)

    merged = [_merge_ocr_group(group) for group in groups]
    return sorted(merged, key=lambda item: (item.box[1], item.box[0]))


def _find_next_sentence_line(previous: OCRItem, candidates: Sequence[OCRItem]) -> Optional[int]:
    matches = [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if _should_merge_sentence_line(previous, candidate)
    ]
    if not matches:
        return None

    px1, _, _, py2 = previous.box
    index, _ = min(
        matches,
        key=lambda match: (
            max(0, match[1].box[1] - py2),
            abs(match[1].box[0] - px1),
            match[1].box[1],
        ),
    )
    return index


def _should_merge_sentence_line(previous: OCRItem, candidate: OCRItem) -> bool:
    previous_text = previous.text.strip()
    candidate_text = candidate.text.strip()
    if not previous_text or not candidate_text:
        return False
    if _ends_sentence(previous_text):
        return False
    if len(previous_text) <= 3 and len(candidate_text) <= 3:
        return False

    px1, py1, px2, py2 = previous.box
    cx1, cy1, cx2, cy2 = candidate.box
    previous_width = max(1, px2 - px1)
    candidate_width = max(1, cx2 - cx1)
    previous_height = max(1, py2 - py1)
    candidate_height = max(1, cy2 - cy1)
    average_height = (previous_height + candidate_height) / 2

    previous_center_y = (py1 + py2) / 2
    candidate_center_y = (cy1 + cy2) / 2
    if candidate_center_y <= previous_center_y + average_height * 0.45:
        return False

    vertical_gap = cy1 - py2
    max_vertical_gap = max(3, average_height * 0.22)
    if vertical_gap > max_vertical_gap:
        return False
    if vertical_gap < -average_height * 0.12:
        return False

    horizontal_overlap = max(0, min(px2, cx2) - max(px1, cx1))
    overlap_ratio = horizontal_overlap / max(1, min(previous_width, candidate_width))
    left_delta = abs(px1 - cx1)
    center_delta = abs((px1 + px2) / 2 - (cx1 + cx2) / 2)
    left_aligned = left_delta <= max(12, int(average_height * 0.75))
    center_close = center_delta <= max(previous_width, candidate_width) * 0.18
    width_ratio = min(previous_width, candidate_width) / max(previous_width, candidate_width)

    # Be very conservative: only merge lines that are almost touching and have
    # strong horizontal evidence of being the same wrapped sentence. Wider
    # paragraph/menu spacing or merely nearby context should remain separate.
    strong_overlap_aligned = overlap_ratio >= 0.8 and (left_aligned or center_close)
    return strong_overlap_aligned or (left_aligned and center_close and width_ratio >= 0.65)


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip().rstrip("\"'”’」』）)]】》〉、，,")
    return bool(stripped) and stripped[-1] in "。.!！？?…"


def _merge_ocr_group(group: Sequence[OCRItem]) -> OCRItem:
    if len(group) == 1:
        return group[0]

    x1 = min(item.box[0] for item in group)
    y1 = min(item.box[1] for item in group)
    x2 = max(item.box[2] for item in group)
    y2 = max(item.box[3] for item in group)
    confidence = sum(item.confidence for item in group) / len(group)

    text = group[0].text.strip()
    for item in group[1:]:
        text = _join_sentence_parts(text, item.text.strip())
    return OCRItem(text=text, box=(x1, y1, x2, y2), confidence=confidence)


def _join_sentence_parts(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-"):
        return left[:-1] + right
    if _contains_cjk(left[-1]) or _contains_cjk(right[0]):
        return left + right
    return left + " " + right


def _contains_cjk(text: str) -> bool:
    return any(
        "\u3040" <= char <= "\u30ff"
        or "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or "\uac00" <= char <= "\ud7af"
        for char in text
    )


def filter_translatable_items(items: Sequence[OCRItem]) -> List[OCRItem]:
    return [item for item in items if should_translate_text(item.text)]


def prepare_translation_items(items: Sequence[OCRItem], merge_context: bool) -> Tuple[List[OCRItem], List[OCRItem]]:
    processed_items = merge_sentence_lines(items) if merge_context else list(items)
    return processed_items, filter_translatable_items(processed_items)


def should_translate_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    compact = re.sub(r"[\W_]+", "", stripped, flags=re.UNICODE)
    if not compact:
        return False
    if compact.isdigit():
        return False
    if len(compact) == 1 and compact.isascii() and compact.isalpha():
        return False
    return True


def prepare_ocr_image(image: np.ndarray, resolution: str) -> Tuple[np.ndarray, float, float]:
    """Resize screenshot before OCR and return coordinate scale-back factors.

    The resolution option is interpreted as target image height while preserving
    aspect ratio. OCR boxes are produced in the resized image coordinate space,
    so callers must multiply x/y by the returned scale factors before adding the
    original screen/window offset.
    """

    if resolution == OCR_RESOLUTION_ORIGINAL or resolution not in OCR_TARGET_HEIGHTS:
        return np.ascontiguousarray(image), 1.0, 1.0
    if image.ndim < 2:
        return np.ascontiguousarray(image), 1.0, 1.0

    original_height, original_width = int(image.shape[0]), int(image.shape[1])
    target_height = int(OCR_TARGET_HEIGHTS[resolution])
    if original_width <= 0 or original_height <= 0 or target_height <= 0 or original_height == target_height:
        return np.ascontiguousarray(image), 1.0, 1.0

    target_width = max(1, int(round(original_width * target_height / original_height)))
    resized = Image.fromarray(image).resize((target_width, target_height), Image.Resampling.BILINEAR)
    resized_array = np.ascontiguousarray(np.asarray(resized, dtype=np.uint8))
    return resized_array, original_width / target_width, original_height / target_height


def map_ocr_items_to_original_coords(items: Sequence[OCRItem], offset: Tuple[int, int], scale_x: float, scale_y: float) -> List[OCRItem]:
    ox, oy = offset
    mapped: List[OCRItem] = []
    for item in items:
        x1, y1, x2, y2 = item.box
        mapped.append(
            OCRItem(
                text=item.text,
                box=(
                    int(round(x1 * scale_x + ox)),
                    int(round(y1 * scale_y + oy)),
                    int(round(x2 * scale_x + ox)),
                    int(round(y2 * scale_y + oy)),
                ),
                confidence=item.confidence,
            )
        )
    return mapped


class OCRTranslateWorker(QRunnable):
    def __init__(self, mode: str, config: AppConfig, generation: int, region: Optional[RectTuple] = None):
        super().__init__()
        self.mode = mode
        self.config = config
        self.generation = generation
        self.region = region
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self) -> None:
        task_start = time.perf_counter()
        try:
            target_window_title = self.config.target_window_title.strip()
            if self.mode == "fullscreen":
                if target_window_title:
                    screenshot = capture_window_by_title(target_window_title)
                    self.signals.log.emit(f"后台窗口截图: {target_window_title}，范围: {screenshot.rect}")
                else:
                    screenshot = capture_fullscreen()
                offset = (screenshot.rect[0], screenshot.rect[1])
            else:
                if self.region is None:
                    raise RuntimeError("选区为空")
                if target_window_title:
                    screenshot = capture_window_region_by_title(target_window_title, self.region)
                    self.signals.log.emit(f"后台窗口选区截图: {target_window_title}，范围: {screenshot.rect}")
                else:
                    screenshot = capture_region(self.region)
                offset = (screenshot.rect[0], screenshot.rect[1])

            self.signals.captured.emit(self.mode, self.generation)

            ocr_image, scale_x, scale_y = prepare_ocr_image(screenshot.image, self.config.ocr.resolution)
            self.signals.log.emit(
                f"{self._mode_label()} OCR 输入分辨率: {screenshot.image.shape[1]}x{screenshot.image.shape[0]}"
                f" → {ocr_image.shape[1]}x{ocr_image.shape[0]}"
            )
            ocr = create_ocr(self.config.ocr)
            ocr_info = ocr.recognize(ocr_image)
            if not ocr_info.error:
                ocr_info = OCRRunInfo(
                    map_ocr_items_to_original_coords(ocr_info.items, offset, scale_x, scale_y),
                    ocr_info.elapsed_ms,
                    ocr_info.backend,
                    ocr_info.error,
                )
            if ocr_info.error:
                self.signals.log.emit(ocr_info.error)
                output = TranslationOutput(
                    [],
                    error=ocr_info.error,
                    total_elapsed_ms=(time.perf_counter() - task_start) * 1000,
                )
                self.signals.finished.emit(self.mode, self.generation, ocr_info, output)
                return

            merged_items, translatable_items = prepare_translation_items(ocr_info.items, self.config.merge_context)
            texts = [item.text for item in translatable_items]
            self.signals.log.emit(f"{self._mode_label()} OCR 数量: {len(ocr_info.items)}，耗时 {ocr_info.elapsed_ms:.1f} ms")
            if self.config.merge_context and len(merged_items) != len(ocr_info.items):
                self.signals.log.emit(f"{self._mode_label()} 已合并跨行句子: {len(ocr_info.items)} → {len(merged_items)}")
            if len(translatable_items) != len(merged_items):
                self.signals.log.emit(f"{self._mode_label()} 已跳过纯数字/单字母文本: {len(merged_items) - len(translatable_items)}")
            if not texts:
                output = TranslationOutput([], total_elapsed_ms=(time.perf_counter() - task_start) * 1000)
                self.signals.finished.emit(self.mode, self.generation, OCRRunInfo([], ocr_info.elapsed_ms, ocr_info.backend), output)
                return

            display_info = OCRRunInfo(translatable_items, ocr_info.elapsed_ms, ocr_info.backend)
            translator = create_translator(self.config)
            output = translator.translate(texts, self.config.source_lang, self.config.target_lang)
            output.total_elapsed_ms = (time.perf_counter() - task_start) * 1000
            if output.error:
                self.signals.log.emit(output.error)
            else:
                self.signals.log.emit(f"{self._mode_label()} 翻译完成，引擎: {output.backend}，条数: {len(output.translations)}")
            self.signals.finished.emit(self.mode, self.generation, display_info, output)
        except Exception as exc:  # noqa: BLE001
            message = f"{self._mode_label()} 任务异常: {exc}\n{traceback.format_exc(limit=3)}"
            self.signals.log.emit(message)
            output = TranslationOutput([], error=message, total_elapsed_ms=(time.perf_counter() - task_start) * 1000)
            self.signals.finished.emit(self.mode, self.generation, OCRRunInfo([], 0.0, "exception", message), output)

    def _mode_label(self) -> str:
        return "全屏" if self.mode == "fullscreen" else "选区"


class TranslationController(QObject):
    log = pyqtSignal(str)
    request_region_selection = pyqtSignal()

    def __init__(self, config: AppConfig, overlay: OverlayManager):
        super().__init__()
        self.config = config
        self.overlay = overlay
        self.thread_pool = QThreadPool.globalInstance()
        self.fullscreen_timer = QTimer(self)
        self.region_timer = QTimer(self)
        self.fullscreen_timer.timeout.connect(self._run_fullscreen_tick)
        self.region_timer.timeout.connect(self._run_region_tick)
        self.fullscreen_busy = False
        self.region_busy = False
        self.fullscreen_refresh_count = 0
        self.region_refresh_count = 0
        self.last_region: Optional[RectTuple] = None
        self.overlay_mode: Optional[str] = None
        self.target_window_paused = False
        self._generation = 0
        self._sync_overlay_capture_exclusion()
        self._sync_status_anchor_rect()

    def update_config(self, config: AppConfig) -> None:
        self.config = config
        self._sync_overlay_capture_exclusion()
        self.overlay.set_translation_theme(self.config.translation_theme)
        self.overlay.set_translation_font_size(self.config.translation_font_size)
        interval_ms = int(self.config.refresh_interval * 1000)
        self.fullscreen_timer.setInterval(interval_ms)
        self.region_timer.setInterval(interval_ms)
        self._update_realtime_status()
        self._sync_region_box()

    def handle_trigger_hotkey(self) -> None:
        if not self.target_window_accepts_hotkey():
            return

        if self.fullscreen_timer.isActive() or self.region_timer.isActive() or self.overlay_mode or self.fullscreen_busy or self.region_busy:
            self.stop_all()
            return

        if self.config.translation_scope == TRANSLATION_SCOPE_REGION:
            self._start_selected_region_mode()
            return

        if self.config.trigger_mode == TRIGGER_MODE_REALTIME:
            self.start_fullscreen_loop()
        else:
            self.run_once(TRANSLATION_SCOPE_FULLSCREEN)

    def handle_fullscreen_hotkey(self) -> None:
        self.config.translation_scope = TRANSLATION_SCOPE_FULLSCREEN
        self.handle_trigger_hotkey()

    def handle_region_hotkey(self) -> None:
        self.config.translation_scope = TRANSLATION_SCOPE_REGION
        self.handle_trigger_hotkey()

    def _start_selected_region_mode(self) -> None:
        if self.config.trigger_mode == TRIGGER_MODE_REALTIME and self.last_region is not None:
            self.start_region_loop(self.last_region)
            return
        self.request_region_selection.emit()

    def on_region_selected(self, rect: RectTuple) -> None:
        self.last_region = rect
        self._sync_region_box()
        if self.config.trigger_mode == TRIGGER_MODE_REALTIME:
            self.start_region_loop(rect)
        else:
            self.run_once("region", rect)

    def start_fullscreen_loop(self) -> None:
        self.stop_region(clear=False)
        self._generation += 1
        self.overlay.clear()
        self.overlay.clear_region_box()
        self.fullscreen_refresh_count = 0
        self.overlay_mode = "fullscreen"
        interval_ms = int(self.config.refresh_interval * 1000)
        self.fullscreen_timer.start(interval_ms)
        self._update_realtime_status()
        self.log.emit(f"全屏实时翻译启动，间隔 {self.config.refresh_interval:.2f}s")
        self._run_fullscreen_tick()

    def start_region_loop(self, rect: RectTuple) -> None:
        self.stop_fullscreen(clear=False)
        self._generation += 1
        self.overlay.clear()
        self.last_region = rect
        self.region_refresh_count = 0
        self.overlay_mode = "region"
        self._sync_region_box()
        interval_ms = int(self.config.refresh_interval * 1000)
        self.region_timer.start(interval_ms)
        self._update_realtime_status()
        self.log.emit(f"选区实时翻译启动，间隔 {self.config.refresh_interval:.2f}s，选区 {rect}")
        self._run_region_tick()

    def stop_fullscreen(self, clear: bool = True) -> None:
        self._generation += 1
        self.fullscreen_timer.stop()
        self.fullscreen_busy = False
        if clear:
            self.overlay.clear()
            if self.overlay_mode == "fullscreen":
                self.overlay_mode = None
        self._update_realtime_status()
        self.log.emit("全屏翻译已停止，悬浮窗已清空")

    def stop_region(self, clear: bool = True) -> None:
        self._generation += 1
        self.region_timer.stop()
        self.region_busy = False
        if clear:
            self.overlay.clear()
            self.overlay.clear_region_box()
            if self.overlay_mode == "region":
                self.overlay_mode = None
        self._update_realtime_status()
        self.log.emit("选区翻译已停止，悬浮窗已清空")

    def stop_all(self) -> None:
        self._generation += 1
        self.fullscreen_timer.stop()
        self.region_timer.stop()
        self.fullscreen_busy = False
        self.region_busy = False
        self.overlay.clear()
        self.overlay.clear_region_box()
        self.overlay_mode = None
        self._update_realtime_status()
        self.log.emit("全部任务已停止")

    def run_once(self, mode: str, region: Optional[RectTuple] = None) -> None:
        self._generation += 1
        if mode == "fullscreen":
            self.overlay_mode = "fullscreen"
            self._sync_region_box()
            self._start_worker("fullscreen", None)
        else:
            self.overlay_mode = "region"
            self._sync_region_box()
            self._start_worker("region", region or self.last_region)

    def _run_fullscreen_tick(self) -> None:
        if self._pause_if_target_window_background("fullscreen"):
            return
        if not self.fullscreen_busy:
            self.fullscreen_refresh_count += 1
            self.log.emit(f"全屏实时刷新 #{self.fullscreen_refresh_count}")
            self._start_worker("fullscreen", None)

    def _run_region_tick(self) -> None:
        if self._pause_if_target_window_background("region"):
            return
        if not self.region_busy and self.last_region is not None:
            self.region_refresh_count += 1
            self.log.emit(f"选区实时刷新 #{self.region_refresh_count}")
            self._start_worker("region", self.last_region)

    def _start_worker(self, mode: str, region: Optional[RectTuple]) -> None:
        if self._pause_if_target_window_background(mode):
            return
        generation = self._generation
        if mode == "fullscreen":
            self.fullscreen_busy = True
        else:
            self.region_busy = True

        if self._capture_uses_target_window() or self.overlay.capture_exclusion_supported():
            self._launch_worker_after_overlay_hidden(mode, region, generation)
        else:
            self.overlay.hide_all()
            QTimer.singleShot(
                80,
                lambda mode=mode, region=region, generation=generation: self._launch_worker_after_overlay_hidden(
                    mode, region, generation
                ),
            )

    def _launch_worker_after_overlay_hidden(self, mode: str, region: Optional[RectTuple], generation: int) -> None:
        if generation != self._generation:
            return
        if self._pause_if_target_window_background(mode):
            return
        if self.overlay_mode != mode:
            return
        if mode == "fullscreen" and not self.fullscreen_busy:
            return
        if mode == "region" and not self.region_busy:
            return

        worker = OCRTranslateWorker(mode, deepcopy(self.config), generation, region)
        worker.signals.captured.connect(self._on_worker_captured)
        worker.signals.log.connect(self.log.emit)
        worker.signals.finished.connect(self._on_worker_finished)
        self.thread_pool.start(worker)

    def _on_worker_captured(self, mode: str, generation: int) -> None:
        if generation != self._generation:
            return
        if self._pause_if_target_window_background(mode):
            return
        if self.overlay_mode != mode or self._capture_uses_target_window() or self.overlay.capture_exclusion_supported():
            return

        # Desktop capture must hide overlays briefly to avoid OCR reading its own
        # translation bubbles when Windows capture exclusion is unavailable.
        # Restore the previous overlays immediately after the screenshot is captured,
        # then replace them only when the new OCR/translation result is ready.
        self.overlay.show_all()

    def _capture_uses_target_window(self) -> bool:
        return bool(self.config.target_window_title.strip())

    def _sync_overlay_capture_exclusion(self) -> None:
        set_capture_exclusion_enabled = getattr(self.overlay, "set_capture_exclusion_enabled", None)
        if callable(set_capture_exclusion_enabled):
            set_capture_exclusion_enabled(not self._capture_uses_target_window())

    def _sync_status_anchor_rect(self) -> None:
        set_status_anchor_rect = getattr(self.overlay, "set_status_anchor_rect", None)
        if not callable(set_status_anchor_rect):
            return

        target_window_title = self.config.target_window_title.strip()
        if not target_window_title:
            set_status_anchor_rect(None)
            return

        try:
            set_status_anchor_rect(get_window_rect_by_title(target_window_title))
        except Exception:
            set_status_anchor_rect(None)

    def target_window_accepts_hotkey(self) -> bool:
        target_window_title = self.config.target_window_title.strip()
        if not target_window_title:
            return True

        try:
            if is_target_window_foreground(target_window_title):
                return True
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"目标窗口前台检测失败，快捷键已忽略: {exc}")
            return False

        self.log.emit("目标窗口不在前台，快捷键已忽略")
        return False

    def _pause_if_target_window_background(self, mode: str) -> bool:
        target_window_title = self.config.target_window_title.strip()
        if not target_window_title:
            if self.target_window_paused:
                self.target_window_paused = False
                self._update_realtime_status()
            return False

        try:
            foreground = is_target_window_foreground(target_window_title)
        except Exception as exc:  # noqa: BLE001
            self._pause_target_window(mode, f"目标窗口前台检测失败: {exc}")
            return True

        if foreground:
            if self.target_window_paused:
                self.target_window_paused = False
                self.log.emit("目标窗口已回到前台，自动恢复 OCR 翻译")
                self._update_realtime_status()
                self._sync_region_box()
            return False

        self._pause_target_window(mode, "目标窗口不在前台，已暂停 OCR 翻译并隐藏悬浮窗")
        return True

    def _pause_target_window(self, mode: str, message: str) -> None:
        if mode == "fullscreen":
            self.fullscreen_busy = False
        else:
            self.region_busy = False
        self.overlay.clear()
        self.overlay.clear_region_box()
        self.overlay.set_realtime_status(False, False)
        self.overlay.set_latency_status(0.0, 0.0, 0.0, False)
        if not self.target_window_paused:
            self.log.emit(message)
        self.target_window_paused = True

    def _sync_region_box(self) -> None:
        if self.config.show_region_box and self.overlay_mode == "region" and self.last_region is not None:
            self.overlay.set_region_box(self.last_region, True)
        else:
            self.overlay.clear_region_box()

    def restart_active_realtime(self, reason: str = "配置已变更") -> bool:
        mode = self.active_realtime_mode()
        if mode is None:
            self._sync_region_box()
            self._update_realtime_status()
            return False

        self._generation += 1
        self.target_window_paused = False
        self.overlay.clear()

        if mode == "fullscreen":
            self.fullscreen_timer.stop()
            self.fullscreen_busy = False
            self.overlay.clear_region_box()
            self.fullscreen_refresh_count = 0
            self.overlay_mode = "fullscreen"
            self.fullscreen_timer.start(int(self.config.refresh_interval * 1000))
            self._update_realtime_status()
            self.log.emit(f"{reason}，已自动重启全屏实时翻译")
            self._run_fullscreen_tick()
            return True

        if self.last_region is None:
            self.region_timer.stop()
            self.region_busy = False
            self.overlay.clear_region_box()
            self.overlay_mode = None
            self._update_realtime_status()
            self.log.emit(f"{reason}，但缺少上次选区，无法自动重启选区实时翻译")
            return False

        self.region_timer.stop()
        self.region_busy = False
        self.region_refresh_count = 0
        self.overlay_mode = "region"
        self._sync_region_box()
        self.region_timer.start(int(self.config.refresh_interval * 1000))
        self._update_realtime_status()
        self.log.emit(f"{reason}，已自动重启选区实时翻译，选区 {self.last_region}")
        self._run_region_tick()
        return True

    def active_realtime_mode(self) -> Optional[str]:
        if self.fullscreen_timer.isActive():
            return "fullscreen"
        if self.region_timer.isActive():
            return "region"
        return None

    def _update_realtime_status(self) -> None:
        self._sync_status_anchor_rect()
        active = (self.fullscreen_timer.isActive() or self.region_timer.isActive()) and not self.target_window_paused
        self.overlay.set_realtime_status(active, self.config.show_realtime_status)
        if not active or not self.config.show_latency_status:
            self.overlay.set_latency_status(0.0, 0.0, 0.0, False)

    def _on_worker_finished(self, mode: str, generation: int, ocr_info: OCRRunInfo, output: TranslationOutput) -> None:
        if generation != self._generation:
            self.log.emit(f"{'全屏' if mode == 'fullscreen' else '选区'}过期任务结果已忽略")
            return

        if mode == "fullscreen":
            self.fullscreen_busy = False
        else:
            self.region_busy = False

        if self.overlay_mode != mode:
            self.log.emit(f"{'全屏' if mode == 'fullscreen' else '选区'}过期任务结果已忽略")
            return

        if self._pause_if_target_window_background(mode):
            return

        self.overlay.show_all()
        self._sync_status_anchor_rect()
        self.overlay.set_latency_status(
            ocr_info.elapsed_ms,
            output.elapsed_ms,
            output.total_elapsed_ms,
            self.config.show_latency_status,
        )

        if ocr_info.error:
            self.log.emit(ocr_info.error)
            return

        if not ocr_info.items:
            self.overlay.clear()
            self.log.emit("未识别到文字")
            return

        self.overlay.show_translations(ocr_info.items, output.translations)
        self.log.emit(f"悬浮译文已刷新: {self.overlay.active_count()} 个")