from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Dict

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QIcon, QTextCursor
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __app_name__, __version__
from .config import (
    ENGINE_BAIDU,
    ENGINE_GOOGLE,
    ENGINE_LABELS,
    ENGINE_OLLAMA,
    ENGINE_OPENAI,
    ENGINE_TENCENT,
    LANGUAGES,
    OCR_RESOLUTION_LABELS,
    TENCENT_REGIONS,
    TRANSLATION_FONT_SIZE_MAX,
    TRANSLATION_FONT_SIZE_MIN,
    TRANSLATION_SCOPE_FULLSCREEN,
    TRANSLATION_SCOPE_LABELS,
    TRANSLATION_SCOPE_REGION,
    TRANSLATION_THEME_LABELS,
    TRIGGER_MODE_LABELS,
    TRIGGER_MODE_ONCE,
    TRIGGER_MODE_REALTIME,
    AppConfig,
    load_config,
    reset_config,
    save_config,
)
from .controller import TranslationController
from .hotkeys import HotkeyManager
from .ocr import create_ocr
from .overlay import OverlayManager
from .screen import RegionSelector, list_capture_windows, window_capture_dependency_error


APP_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
APP_ICON_PATH = APP_ROOT / "assets" / "gameocr.ico"


class HotkeyCaptureEdit(QLineEdit):
    hotkey_captured = pyqtSignal(str)
    capture_cancelled = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._previous_text = ""
        self.setPlaceholderText("例如 f8 或 ctrl+alt+1")

    def start_capture(self) -> None:
        self._capturing = True
        self._previous_text = self.text()
        self.setFocus(Qt.OtherFocusReason)
        self.selectAll()
        self.setText("请按下快捷键...")

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if not self._capturing:
            super().keyPressEvent(event)
            return

        if event.key() == Qt.Key_Escape:
            self.cancel_capture()
            return

        hotkey = self._event_to_hotkey(event)
        if hotkey:
            self._capturing = False
            self.setText(hotkey)
            self.hotkey_captured.emit(hotkey)
            self.clearFocus()

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        if self._capturing:
            self.cancel_capture()
        super().focusOutEvent(event)

    def cancel_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        self.setText(self._previous_text)
        self.capture_cancelled.emit()
        self.clearFocus()

    def _event_to_hotkey(self, event) -> str:
        key = event.key()
        modifiers = event.modifiers()
        parts = []
        if modifiers & Qt.ControlModifier:
            parts.append("ctrl")
        if modifiers & Qt.AltModifier:
            parts.append("alt")
        if modifiers & Qt.ShiftModifier:
            parts.append("shift")
        if modifiers & Qt.MetaModifier:
            parts.append("cmd")

        key_name = self._key_name(key)
        if not key_name:
            return ""
        if key_name not in {"ctrl", "alt", "shift", "cmd"}:
            parts.append(key_name)
        return "+".join(parts)

    def _key_name(self, key: int) -> str:
        function_keys = {
            Qt.Key_F1: "f1",
            Qt.Key_F2: "f2",
            Qt.Key_F3: "f3",
            Qt.Key_F4: "f4",
            Qt.Key_F5: "f5",
            Qt.Key_F6: "f6",
            Qt.Key_F7: "f7",
            Qt.Key_F8: "f8",
            Qt.Key_F9: "f9",
            Qt.Key_F10: "f10",
            Qt.Key_F11: "f11",
            Qt.Key_F12: "f12",
        }
        special_keys = {
            Qt.Key_Return: "enter",
            Qt.Key_Enter: "enter",
            Qt.Key_Space: "space",
            Qt.Key_Tab: "tab",
            Qt.Key_Backspace: "backspace",
            Qt.Key_Delete: "delete",
            Qt.Key_Insert: "insert",
            Qt.Key_Home: "home",
            Qt.Key_End: "end",
            Qt.Key_PageUp: "page_up",
            Qt.Key_PageDown: "page_down",
            Qt.Key_Up: "up",
            Qt.Key_Down: "down",
            Qt.Key_Left: "left",
            Qt.Key_Right: "right",
        }
        if key in function_keys:
            return function_keys[key]
        if key in special_keys:
            return special_keys[key]
        if Qt.Key_A <= key <= Qt.Key_Z:
            return chr(ord("a") + key - Qt.Key_A)
        if Qt.Key_0 <= key <= Qt.Key_9:
            return chr(ord("0") + key - Qt.Key_0)
        return ""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.app_icon = QIcon(str(APP_ICON_PATH))
        if not self.app_icon.isNull():
            self.setWindowIcon(self.app_icon)
            QApplication.instance().setWindowIcon(self.app_icon)
        self.resize(860, 720)

        self.overlay = OverlayManager()
        self.overlay.set_translation_theme(self.config.translation_theme)
        self.overlay.set_translation_font_size(self.config.translation_font_size)
        self.controller = TranslationController(self.config, self.overlay)
        self.controller.log.connect(self.append_log)
        self.controller.request_region_selection.connect(self.begin_region_selection)

        self.region_selector = RegionSelector()
        self.region_selector.selected.connect(self.controller.on_region_selected)
        self.region_selector.cancelled.connect(lambda: self.append_log("选区已取消"))

        self.hotkeys = HotkeyManager()
        self.hotkeys.hotkey_pressed.connect(self.on_hotkey)
        self.hotkeys.error.connect(self.on_hotkey_error)

        self.engine_pages: Dict[str, int] = {}
        self._loading_ui = False
        self._auto_apply_timer = QTimer(self)
        self._auto_apply_timer.setSingleShot(True)
        self._auto_apply_timer.setInterval(300)
        self._auto_apply_timer.timeout.connect(self._apply_realtime_change_if_active)

        self._build_ui()
        self._connect_realtime_auto_apply_signals()
        self._build_tray()
        self.load_values_to_ui()

        self._preload_ocr()
        self.apply_and_save(show_message=False)
        self.append_log("程序已启动。默认 F8 触发翻译；在 GUI 中选择整页/选区与单次/实时模式。")

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)

        root.addWidget(self._build_engine_group())
        root.addWidget(self._build_capture_group())
        root.addWidget(self._build_hotkey_group())
        root.addWidget(self._build_realtime_group())
        root.addWidget(self._build_buttons_group())
        root.addWidget(self._build_log_group(), stretch=1)

        self.setCentralWidget(central)

    def _build_engine_group(self) -> QGroupBox:
        group = QGroupBox("翻译引擎设置")
        layout = QVBoxLayout(group)

        top_form = QFormLayout()
        self.engine_combo = QComboBox()
        for engine, label in ENGINE_LABELS.items():
            self.engine_combo.addItem(label, engine)
        self.engine_combo.currentIndexChanged.connect(self.on_engine_changed)

        self.source_lang_combo = QComboBox()
        self.target_lang_combo = QComboBox()
        for code, label in LANGUAGES.items():
            self.source_lang_combo.addItem(label, code)
            if code != "auto":
                self.target_lang_combo.addItem(label, code)

        self.translation_theme_combo = QComboBox()
        for theme, label in TRANSLATION_THEME_LABELS.items():
            self.translation_theme_combo.addItem(label, theme)

        self.translation_font_size_spin = QSpinBox()
        self.translation_font_size_spin.setRange(TRANSLATION_FONT_SIZE_MIN, TRANSLATION_FONT_SIZE_MAX)
        self.translation_font_size_spin.setSingleStep(1)
        self.translation_font_size_spin.setSuffix(" pt")

        top_form.addRow("翻译引擎", self.engine_combo)
        top_form.addRow("源语言", self.source_lang_combo)
        top_form.addRow("目标语言", self.target_lang_combo)
        top_form.addRow("译文框配色", self.translation_theme_combo)
        top_form.addRow("译文框字号", self.translation_font_size_spin)
        layout.addLayout(top_form)

        self.engine_stack = QStackedWidget()
        self.engine_stack.addWidget(self._google_page())
        self.engine_pages[ENGINE_GOOGLE] = 0
        self.engine_stack.addWidget(self._baidu_page())
        self.engine_pages[ENGINE_BAIDU] = 1
        self.engine_stack.addWidget(self._tencent_page())
        self.engine_pages[ENGINE_TENCENT] = 2
        self.engine_stack.addWidget(self._openai_page())
        self.engine_pages[ENGINE_OPENAI] = 3
        self.engine_stack.addWidget(self._ollama_page())
        self.engine_pages[ENGINE_OLLAMA] = 4
        layout.addWidget(self.engine_stack)
        return group

    def _google_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        hint = QLabel("谷歌翻译无需密钥；需要当前网络环境能够访问 Google 翻译服务才能使用。")
        hint.setWordWrap(True)
        form.addRow("使用提示", hint)
        return page

    def _baidu_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        link = QLabel('<a href="https://fanyi-api.baidu.com/">百度翻译开放平台：https://fanyi-api.baidu.com/</a>')
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        self.baidu_app_id_edit = QLineEdit()
        self.baidu_secret_edit = QLineEdit()
        self.baidu_secret_edit.setEchoMode(QLineEdit.Password)
        form.addRow("申请地址", link)
        form.addRow("APP ID", self.baidu_app_id_edit)
        form.addRow("密钥", self.baidu_secret_edit)
        return page

    def _tencent_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        link = QLabel('<a href="https://cloud.tencent.com/product/tmt">腾讯云机器翻译：https://cloud.tencent.com/product/tmt</a>')
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        self.tencent_secret_id_edit = QLineEdit()
        self.tencent_secret_key_edit = QLineEdit()
        self.tencent_secret_key_edit.setEchoMode(QLineEdit.Password)
        self.tencent_region_combo = QComboBox()
        for region, label in TENCENT_REGIONS.items():
            self.tencent_region_combo.addItem(label, region)
        form.addRow("申请地址", link)
        form.addRow("SecretId", self.tencent_secret_id_edit)
        form.addRow("SecretKey", self.tencent_secret_key_edit)
        form.addRow("Region", self.tencent_region_combo)
        return page

    def _openai_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.openai_base_url_edit = QLineEdit()
        self.openai_api_key_edit = QLineEdit()
        self.openai_api_key_edit.setEchoMode(QLineEdit.Password)
        self.openai_model_edit = QLineEdit()
        self.openai_timeout_spin = QDoubleSpinBox()
        self.openai_timeout_spin.setRange(1.0, 300.0)
        self.openai_timeout_spin.setSingleStep(1.0)
        self.openai_timeout_spin.setSuffix(" s")
        form.addRow("接口地址", self.openai_base_url_edit)
        form.addRow("API Key", self.openai_api_key_edit)
        form.addRow("模型名称", self.openai_model_edit)
        form.addRow("请求超时", self.openai_timeout_spin)
        return page

    def _ollama_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        link = QLabel('<a href="https://ollama.com/">Ollama 官网：https://ollama.com/</a>')
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        self.ollama_base_url_edit = QLineEdit()
        self.ollama_model_edit = QLineEdit()
        self.ollama_context_edit = QTextEdit()
        self.ollama_context_edit.setMaximumHeight(70)
        self.ollama_context_edit.setPlaceholderText("可选：游戏专有名词、角色名、翻译风格等上下文")
        form.addRow("官网地址", link)
        form.addRow("本地服务地址", self.ollama_base_url_edit)
        form.addRow("模型名", self.ollama_model_edit)
        form.addRow("上下文参数", self.ollama_context_edit)
        return page

    def _build_capture_group(self) -> QGroupBox:
        group = QGroupBox("截图目标设置")
        form = QFormLayout(group)

        self.target_window_combo = QComboBox()
        self.target_window_combo.setMinimumWidth(520)
        self.target_window_combo.addItem("全屏桌面截图（默认）", "")
        self.refresh_windows_button = QPushButton("刷新窗口列表")
        self.refresh_windows_button.clicked.connect(self.refresh_window_list)

        window_row = QHBoxLayout()
        window_row.addWidget(self.target_window_combo, stretch=1)
        window_row.addWidget(self.refresh_windows_button)
        form.addRow("目标游戏窗口", window_row)

        self.ocr_resolution_combo = QComboBox()
        for value, label in OCR_RESOLUTION_LABELS.items():
            self.ocr_resolution_combo.addItem(label, value)
        form.addRow("OCR 输入分辨率", self.ocr_resolution_combo)

        self.merge_context_check = QCheckBox("启用跨行上下文合并")
        self.merge_context_check.setToolTip("开启后会把紧密相邻且对齐的 OCR 文本行合并为一句再翻译；关闭时每个 OCR 文本框独立翻译。")
        form.addRow("合并上下文", self.merge_context_check)

        self.show_region_box_check = QCheckBox("显示红色选区边框")
        self.show_region_box_check.setToolTip("开启后，选区翻译会在已选择区域仅显示红色边框；关闭时不显示选框。")
        form.addRow("选区显示", self.show_region_box_check)
        return group

    def _build_hotkey_group(self) -> QGroupBox:
        group = QGroupBox("快捷键配置")
        form = QFormLayout(group)
        self.trigger_hotkey_edit = HotkeyCaptureEdit()
        self.trigger_hotkey_edit.setPlaceholderText("例如 f8 或 ctrl+alt+1")
        self.trigger_hotkey_edit.hotkey_captured.connect(lambda _: self.apply_and_save(show_message=False))
        self.trigger_hotkey_edit.capture_cancelled.connect(lambda: self.apply_and_save(show_message=False))

        trigger_row = QHBoxLayout()
        trigger_capture_button = QPushButton("按键录入")
        trigger_capture_button.clicked.connect(lambda: self.begin_hotkey_capture(self.trigger_hotkey_edit))
        trigger_row.addWidget(self.trigger_hotkey_edit)
        trigger_row.addWidget(trigger_capture_button)

        self.font_increase_hotkey_edit = HotkeyCaptureEdit()
        self.font_increase_hotkey_edit.setPlaceholderText("例如 ctrl+up")
        self.font_increase_hotkey_edit.hotkey_captured.connect(lambda _: self.apply_and_save(show_message=False))
        self.font_increase_hotkey_edit.capture_cancelled.connect(lambda: self.apply_and_save(show_message=False))

        font_increase_row = QHBoxLayout()
        font_increase_capture_button = QPushButton("按键录入")
        font_increase_capture_button.clicked.connect(lambda: self.begin_hotkey_capture(self.font_increase_hotkey_edit))
        font_increase_row.addWidget(self.font_increase_hotkey_edit)
        font_increase_row.addWidget(font_increase_capture_button)

        self.font_decrease_hotkey_edit = HotkeyCaptureEdit()
        self.font_decrease_hotkey_edit.setPlaceholderText("例如 ctrl+down")
        self.font_decrease_hotkey_edit.hotkey_captured.connect(lambda _: self.apply_and_save(show_message=False))
        self.font_decrease_hotkey_edit.capture_cancelled.connect(lambda: self.apply_and_save(show_message=False))

        font_decrease_row = QHBoxLayout()
        font_decrease_capture_button = QPushButton("按键录入")
        font_decrease_capture_button.clicked.connect(lambda: self.begin_hotkey_capture(self.font_decrease_hotkey_edit))
        font_decrease_row.addWidget(self.font_decrease_hotkey_edit)
        font_decrease_row.addWidget(font_decrease_capture_button)

        form.addRow("翻译触发热键", trigger_row)
        form.addRow("译文放大热键", font_increase_row)
        form.addRow("译文缩小热键", font_decrease_row)
        return group

    def _build_realtime_group(self) -> QGroupBox:
        group = QGroupBox("触发模式与实时参数")
        layout = QVBoxLayout(group)

        mode_form = QFormLayout()

        self.translation_scope_fullscreen_radio = QRadioButton(TRANSLATION_SCOPE_LABELS[TRANSLATION_SCOPE_FULLSCREEN])
        self.translation_scope_region_radio = QRadioButton(TRANSLATION_SCOPE_LABELS[TRANSLATION_SCOPE_REGION])
        self.translation_scope_fullscreen_radio.setChecked(True)
        self.translation_scope_group = QButtonGroup(self)
        self.translation_scope_group.addButton(self.translation_scope_fullscreen_radio)
        self.translation_scope_group.addButton(self.translation_scope_region_radio)

        translation_scope_row = QHBoxLayout()
        translation_scope_row.addWidget(self.translation_scope_fullscreen_radio)
        translation_scope_row.addWidget(self.translation_scope_region_radio)
        translation_scope_row.addStretch(1)

        self.trigger_mode_realtime_radio = QRadioButton(TRIGGER_MODE_LABELS[TRIGGER_MODE_REALTIME])
        self.trigger_mode_once_radio = QRadioButton(TRIGGER_MODE_LABELS[TRIGGER_MODE_ONCE])
        self.trigger_mode_realtime_radio.setChecked(True)
        self.trigger_mode_group = QButtonGroup(self)
        self.trigger_mode_group.addButton(self.trigger_mode_realtime_radio)
        self.trigger_mode_group.addButton(self.trigger_mode_once_radio)

        trigger_mode_row = QHBoxLayout()
        trigger_mode_row.addWidget(self.trigger_mode_realtime_radio)
        trigger_mode_row.addWidget(self.trigger_mode_once_radio)
        trigger_mode_row.addStretch(1)

        mode_form.addRow("翻译范围", translation_scope_row)
        mode_form.addRow("执行模式", trigger_mode_row)
        layout.addLayout(mode_form)

        row = QHBoxLayout()
        row.addWidget(QLabel("实时刷新间隔"))
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.1, 5.0)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSuffix(" s")
        row.addWidget(self.interval_spin)
        row.addStretch(1)
        layout.addLayout(row)

        self.status_hint_check = QCheckBox("实时翻译开启时显示右上角提示")
        self.latency_hint_check = QCheckBox("显示 OCR / 翻译 / 总耗时性能提示")
        layout.addWidget(self.status_hint_check)
        layout.addWidget(self.latency_hint_check)
        return group

    def _build_buttons_group(self) -> QGroupBox:
        group = QGroupBox("辅助控制")
        layout = QHBoxLayout(group)
        self.save_button = QPushButton("保存配置")
        self.reset_button = QPushButton("重置所有配置")
        self.tray_button = QPushButton("最小化到托盘")
        self.stop_button = QPushButton("停止并清空悬浮窗")
        self.exit_button = QPushButton("退出程序")

        self.save_button.clicked.connect(lambda: self.apply_and_save(show_message=False))
        self.reset_button.clicked.connect(self.reset_all)
        self.tray_button.clicked.connect(self.minimize_to_tray)
        self.stop_button.clicked.connect(self.controller.stop_all)
        self.exit_button.clicked.connect(self.exit_app)

        layout.addWidget(self.save_button)
        layout.addWidget(self.reset_button)
        layout.addWidget(self.tray_button)
        layout.addWidget(self.stop_button)
        layout.addStretch(1)
        layout.addWidget(self.exit_button)
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("运行日志")
        layout = QVBoxLayout(group)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.log_edit)
        return group

    def _build_tray(self) -> None:
        icon = self.app_icon if not self.app_icon.isNull() else self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu(self)
        show_action = QAction("显示主界面", self)
        quit_action = QAction("退出程序", self)
        show_action.triggered.connect(self.show_from_tray)
        quit_action.triggered.connect(self.exit_app)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setToolTip(f"{__app_name__} v{__version__}")
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def load_values_to_ui(self) -> None:
        self._loading_ui = True
        self._set_combo_by_data(self.engine_combo, self.config.engine)
        self._set_combo_by_data(self.source_lang_combo, self.config.source_lang)
        self._set_combo_by_data(self.target_lang_combo, self.config.target_lang)
        self._set_combo_by_data(self.translation_theme_combo, self.config.translation_theme)
        self.translation_font_size_spin.setValue(self.config.translation_font_size)

        self.baidu_app_id_edit.setText(self.config.baidu.app_id)
        self.baidu_secret_edit.setText(self.config.baidu.secret_key)

        self.tencent_secret_id_edit.setText(self.config.tencent.secret_id)
        self.tencent_secret_key_edit.setText(self.config.tencent.secret_key)
        self._set_combo_by_data(self.tencent_region_combo, self.config.tencent.region)

        self.openai_base_url_edit.setText(self.config.openai.base_url)
        self.openai_api_key_edit.setText(self.config.openai.api_key)
        self.openai_model_edit.setText(self.config.openai.model)
        self.openai_timeout_spin.setValue(self.config.openai.timeout)

        self.ollama_base_url_edit.setText(self.config.ollama.base_url)
        self.ollama_model_edit.setText(self.config.ollama.model)
        self.ollama_context_edit.setPlainText(self.config.ollama.context)

        self.refresh_window_list(selected_title=self.config.target_window_title, log=False)
        self._set_combo_by_data(self.ocr_resolution_combo, self.config.ocr.resolution)
        self.merge_context_check.setChecked(self.config.merge_context)
        self.show_region_box_check.setChecked(self.config.show_region_box)

        self.trigger_hotkey_edit.setText(self.config.trigger_hotkey)
        self.font_increase_hotkey_edit.setText(self.config.font_increase_hotkey)
        self.font_decrease_hotkey_edit.setText(self.config.font_decrease_hotkey)
        self.translation_scope_fullscreen_radio.setChecked(self.config.translation_scope == TRANSLATION_SCOPE_FULLSCREEN)
        self.translation_scope_region_radio.setChecked(self.config.translation_scope == TRANSLATION_SCOPE_REGION)
        self.trigger_mode_realtime_radio.setChecked(self.config.trigger_mode == TRIGGER_MODE_REALTIME)
        self.trigger_mode_once_radio.setChecked(self.config.trigger_mode == TRIGGER_MODE_ONCE)
        self.interval_spin.setValue(self.config.refresh_interval)
        self.status_hint_check.setChecked(self.config.show_realtime_status)
        self.latency_hint_check.setChecked(self.config.show_latency_status)

        self.on_engine_changed()
        self._loading_ui = False

    def collect_config_from_ui(self) -> AppConfig:
        cfg = self.config
        cfg.engine = self.engine_combo.currentData()
        cfg.source_lang = self.source_lang_combo.currentData()
        cfg.target_lang = self.target_lang_combo.currentData()
        cfg.translation_theme = self.translation_theme_combo.currentData() or "classic"
        cfg.translation_font_size = self.translation_font_size_spin.value()
        cfg.google.proxy = ""

        cfg.baidu.app_id = self.baidu_app_id_edit.text().strip()
        cfg.baidu.secret_key = self.baidu_secret_edit.text().strip()

        cfg.tencent.secret_id = self.tencent_secret_id_edit.text().strip()
        cfg.tencent.secret_key = self.tencent_secret_key_edit.text().strip()
        cfg.tencent.region = self.tencent_region_combo.currentData() or "ap-shanghai"

        cfg.openai.base_url = self.openai_base_url_edit.text().strip()
        cfg.openai.api_key = self.openai_api_key_edit.text().strip()
        cfg.openai.model = self.openai_model_edit.text().strip()
        cfg.openai.timeout = self.openai_timeout_spin.value()

        cfg.ollama.base_url = self.ollama_base_url_edit.text().strip()
        cfg.ollama.model = self.ollama_model_edit.text().strip()
        cfg.ollama.context = self.ollama_context_edit.toPlainText().strip()

        cfg.target_window_title = self.target_window_combo.currentData() or ""
        cfg.ocr.resolution = self.ocr_resolution_combo.currentData() or "original"
        cfg.merge_context = self.merge_context_check.isChecked()
        cfg.show_region_box = self.show_region_box_check.isChecked()

        cfg.trigger_hotkey = self.trigger_hotkey_edit.text().strip()
        cfg.font_increase_hotkey = self.font_increase_hotkey_edit.text().strip()
        cfg.font_decrease_hotkey = self.font_decrease_hotkey_edit.text().strip()
        cfg.translation_scope = (
            TRANSLATION_SCOPE_REGION
            if self.translation_scope_region_radio.isChecked()
            else TRANSLATION_SCOPE_FULLSCREEN
        )
        cfg.trigger_mode = TRIGGER_MODE_ONCE if self.trigger_mode_once_radio.isChecked() else TRIGGER_MODE_REALTIME
        cfg.fullscreen_hotkey = cfg.trigger_hotkey
        cfg.refresh_interval = self.interval_spin.value()
        cfg.fullscreen_realtime = cfg.trigger_mode == "realtime"
        cfg.region_realtime = cfg.trigger_mode == "realtime"
        cfg.show_realtime_status = self.status_hint_check.isChecked()
        cfg.show_latency_status = self.latency_hint_check.isChecked()
        cfg.normalize()
        return cfg

    def apply_and_save(self, show_message: bool = False, restart_realtime: bool = True) -> None:
        previous_hotkeys = {
            "trigger_hotkey": self.config.trigger_hotkey,
            "font_increase_hotkey": self.config.font_increase_hotkey,
            "font_decrease_hotkey": self.config.font_decrease_hotkey,
        }
        self.config = self.collect_config_from_ui()
        self.overlay.set_translation_theme(self.config.translation_theme)
        self.overlay.set_translation_font_size(self.config.translation_font_size)
        self.controller.update_config(self.config)
        ok = self.hotkeys.update_bindings(
            self.config.trigger_hotkey,
            "",
            self.config.font_increase_hotkey,
            self.config.font_decrease_hotkey,
        )
        if not ok:
            self.config.trigger_hotkey = previous_hotkeys["trigger_hotkey"]
            self.config.font_increase_hotkey = previous_hotkeys["font_increase_hotkey"]
            self.config.font_decrease_hotkey = previous_hotkeys["font_decrease_hotkey"]
            self.trigger_hotkey_edit.setText(self.config.trigger_hotkey)
            self.font_increase_hotkey_edit.setText(self.config.font_increase_hotkey)
            self.font_decrease_hotkey_edit.setText(self.config.font_decrease_hotkey)
            self.hotkeys.update_bindings(
                self.config.trigger_hotkey,
                "",
                self.config.font_increase_hotkey,
                self.config.font_decrease_hotkey,
            )
            self.controller.update_config(self.config)
            return
        save_config(self.config)
        self.trigger_hotkey_edit.setText(self.config.trigger_hotkey)
        self.font_increase_hotkey_edit.setText(self.config.font_increase_hotkey)
        self.font_decrease_hotkey_edit.setText(self.config.font_decrease_hotkey)
        self.append_log("配置已保存，热键已即时生效")
        if restart_realtime:
            self.controller.restart_active_realtime("配置已变更")
        if show_message:
            QMessageBox.information(self, "保存成功", "配置已保存并即时生效。")

    def reset_all(self) -> None:
        self.controller.stop_all()
        self.config = reset_config()
        self.load_values_to_ui()
        self.apply_and_save(show_message=False)
        self.append_log("配置已重置为默认值")

    def refresh_window_list(self, selected_title: str = "", log: bool = True) -> None:
        selected_title = selected_title or (self.target_window_combo.currentData() if hasattr(self, "target_window_combo") else "") or ""
        self.target_window_combo.blockSignals(True)
        self.target_window_combo.clear()
        self.target_window_combo.addItem("全屏桌面截图（默认）", "")

        dependency_error = window_capture_dependency_error()
        windows = [] if dependency_error else list_capture_windows()
        for window in windows:
            self.target_window_combo.addItem(window.label, window.selector)

        index = self.target_window_combo.findData(selected_title)
        if index >= 0:
            self.target_window_combo.setCurrentIndex(index)
        elif selected_title:
            self.target_window_combo.addItem(f"{selected_title}（当前未找到，保存后按标题重试）", selected_title)
            self.target_window_combo.setCurrentIndex(self.target_window_combo.count() - 1)
        else:
            self.target_window_combo.setCurrentIndex(0)
        self.target_window_combo.blockSignals(False)

        if log:
            self.append_log(f"窗口列表已刷新，可选窗口 {len(windows)} 个")
            if dependency_error:
                self.append_log(dependency_error)
            elif not windows:
                self.append_log("未枚举到可选窗口：请确认游戏不是最小化状态；若游戏以管理员权限运行，也需要以管理员身份运行本程序。")

    def on_engine_changed(self) -> None:
        if not hasattr(self, "engine_stack"):
            return
        engine = self.engine_combo.currentData()
        self.engine_stack.setCurrentIndex(self.engine_pages.get(engine, 0))
        self._schedule_realtime_auto_apply()

    def _connect_realtime_auto_apply_signals(self) -> None:
        combo_boxes = [
            self.engine_combo,
            self.source_lang_combo,
            self.target_lang_combo,
            self.translation_theme_combo,
            self.tencent_region_combo,
            self.target_window_combo,
            self.ocr_resolution_combo,
        ]
        for combo in combo_boxes:
            combo.currentIndexChanged.connect(self._schedule_realtime_auto_apply)

        radio_buttons = [
            self.translation_scope_fullscreen_radio,
            self.translation_scope_region_radio,
            self.trigger_mode_realtime_radio,
            self.trigger_mode_once_radio,
        ]
        for radio in radio_buttons:
            radio.toggled.connect(self._schedule_realtime_auto_apply)

        check_boxes = [
            self.merge_context_check,
            self.show_region_box_check,
            self.status_hint_check,
            self.latency_hint_check,
        ]
        for checkbox in check_boxes:
            checkbox.stateChanged.connect(self._schedule_realtime_auto_apply)

        for edit in [
            self.baidu_app_id_edit,
            self.baidu_secret_edit,
            self.tencent_secret_id_edit,
            self.tencent_secret_key_edit,
            self.openai_base_url_edit,
            self.openai_api_key_edit,
            self.openai_model_edit,
            self.ollama_base_url_edit,
            self.ollama_model_edit,
        ]:
            edit.editingFinished.connect(self._schedule_realtime_auto_apply)

        self.openai_timeout_spin.valueChanged.connect(self._schedule_realtime_auto_apply)
        self.translation_font_size_spin.valueChanged.connect(self._schedule_realtime_auto_apply)
        self.interval_spin.valueChanged.connect(self._schedule_realtime_auto_apply)
        self.ollama_context_edit.textChanged.connect(self._schedule_realtime_auto_apply)

    def _schedule_realtime_auto_apply(self) -> None:
        if self._loading_ui:
            return
        if self.controller.active_realtime_mode() is None:
            return
        self._auto_apply_timer.start()

    def _apply_realtime_change_if_active(self) -> None:
        if self._loading_ui or self.controller.active_realtime_mode() is None:
            return
        self.apply_and_save(show_message=False, restart_realtime=True)

    def on_hotkey(self, mode: str) -> None:
        if not self.controller.target_window_accepts_hotkey():
            return

        if mode == "font_increase":
            self.adjust_translation_font_size(1)
            return
        if mode == "font_decrease":
            self.adjust_translation_font_size(-1)
            return

        self.apply_and_save(show_message=False, restart_realtime=False)
        self.controller.handle_trigger_hotkey()

    def adjust_translation_font_size(self, delta: int) -> None:
        current = self.translation_font_size_spin.value()
        new_value = max(TRANSLATION_FONT_SIZE_MIN, min(TRANSLATION_FONT_SIZE_MAX, current + delta))
        if new_value == current:
            self.append_log(f"译文框字号已到边界: {current} pt")
            return

        self.translation_font_size_spin.setValue(new_value)
        self.config.translation_font_size = new_value
        self.overlay.set_translation_font_size(new_value)
        self.controller.update_config(self.config)
        save_config(self.config)
        self.append_log(f"译文框字号已调整为 {new_value} pt")

    def on_hotkey_error(self, message: str) -> None:
        self.append_log(message)
        QMessageBox.warning(self, "热键错误", message)

    def begin_hotkey_capture(self, edit: HotkeyCaptureEdit) -> None:
        self.hotkeys.stop()
        edit.start_capture()
        self.append_log("进入快捷键按键录入模式，请直接按下目标快捷键；按 ESC 取消。")

    def begin_region_selection(self) -> None:
        self.append_log("进入选区框选模式，拖拽鼠标选择识别区域，ESC 取消")
        self.region_selector.begin()

    def append_log(self, message: str) -> None:
        now = dt.datetime.now().strftime("%H:%M:%S")
        self.log_edit.append(f"[{now}] {message}")
        self.log_edit.moveCursor(QTextCursor.End)

    def minimize_to_tray(self) -> None:
        self.hide()
        self.tray.showMessage(__app_name__, "程序已最小化到托盘，热键仍在后台生效。", QSystemTrayIcon.Information, 1800)

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_from_tray()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        event.ignore()
        self.minimize_to_tray()

    def exit_app(self) -> None:
        self.controller.stop_all()
        self.hotkeys.stop()
        self.tray.hide()
        QApplication.instance().quit()

    def _preload_ocr(self) -> None:
        self.append_log("正在加载 OCR 模型（仅启动时加载一次）...")
        ocr = create_ocr(self.config.ocr)
        if ocr.load_error:
            self.append_log(ocr.load_error)
        else:
            self.append_log(f"OCR 模型加载完成，后端: {ocr.backend_name}")

    def _set_combo_by_data(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)


def run_app() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    return app.exec_()