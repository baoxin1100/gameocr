from pathlib import Path

import numpy as np
from PyQt5.QtCore import QCoreApplication, QRect

from gameocr.config import (
    CONFIG_SCHEMA_VERSION,
    TRANSLATION_FONT_SIZE_DEFAULT,
    TRANSLATION_FONT_SIZE_MAX,
    TRANSLATION_FONT_SIZE_MIN,
    AppConfig,
    load_config,
    reset_config,
    save_config,
)
from gameocr.controller import (
    TranslationController,
    filter_translatable_items,
    map_ocr_items_to_original_coords,
    merge_sentence_lines,
    prepare_ocr_image,
    prepare_translation_items,
    should_translate_text,
)
from gameocr.hotkeys import Win32HotkeyListener, parse_win32_hotkey, to_pynput_hotkey
from gameocr.ocr import OCRItem
from gameocr.overlay import OverlayManager, _expanded_translation_width_limit, _place_without_overlap
from gameocr.screen import _mss_to_rgb_array
from gameocr.translation import (
    BaseTranslator,
    GoogleTranslator,
    OllamaTranslator,
    OpenAITranslator,
    _openai_chat_completions_url,
    _parse_indexed_batch_text,
    _parse_llm_json,
)


def test_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = AppConfig()
    assert cfg.ollama.model == "gemma4:31b-cloud"
    cfg.engine = "ollama"
    cfg.target_lang = "en"
    cfg.refresh_interval = 0.01
    cfg.show_latency_status = False
    cfg.merge_context = True
    cfg.show_region_box = True
    cfg.translation_theme = "purple"
    cfg.translation_font_size = 99
    cfg.font_increase_hotkey = "Ctrl+Up"
    cfg.font_decrease_hotkey = "Ctrl+Down"
    cfg.target_window_title = "Example Game"
    cfg.ocr.resolution = "720p"
    cfg.ollama.model = "qwen2.5:7b"
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.engine == "ollama"
    assert loaded.target_lang == "en"
    assert loaded.refresh_interval == 0.1
    assert not loaded.show_latency_status
    assert loaded.merge_context
    assert loaded.show_region_box
    assert loaded.translation_theme == "purple"
    assert loaded.translation_font_size == TRANSLATION_FONT_SIZE_MAX
    assert loaded.font_increase_hotkey == "ctrl+up"
    assert loaded.font_decrease_hotkey == "ctrl+down"
    assert loaded.target_window_title == "Example Game"
    assert loaded.ocr.resolution == "720p"
    assert loaded.ollama.model == "qwen2.5:7b"
    assert loaded.config_version == CONFIG_SCHEMA_VERSION


def test_legacy_default_f1_hotkey_migrates_to_f8(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        """
{
  "engine": "google",
  "trigger_hotkey": "f1",
  "fullscreen_hotkey": "f1",
  "target_lang": "zh-CN"
}
""",
        encoding="utf-8",
    )

    loaded = load_config(path)

    assert loaded.config_version == CONFIG_SCHEMA_VERSION
    assert loaded.trigger_hotkey == "f8"
    assert loaded.fullscreen_hotkey == "f8"


def test_legacy_alt_q_default_hotkey_migrates_to_f8(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        """
{
  "config_version": 2,
  "engine": "google",
  "trigger_hotkey": "alt+q",
  "fullscreen_hotkey": "alt+q",
  "target_lang": "zh-CN"
}
""",
        encoding="utf-8",
    )

    loaded = load_config(path)

    assert loaded.config_version == CONFIG_SCHEMA_VERSION
    assert loaded.trigger_hotkey == "f8"
    assert loaded.fullscreen_hotkey == "f8"


def test_custom_legacy_hotkey_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        """
{
  "engine": "google",
  "trigger_hotkey": "ctrl+alt+1",
  "fullscreen_hotkey": "ctrl+alt+1",
  "target_lang": "zh-CN"
}
""",
        encoding="utf-8",
    )

    loaded = load_config(path)

    assert loaded.config_version == CONFIG_SCHEMA_VERSION
    assert loaded.trigger_hotkey == "ctrl+alt+1"
    assert loaded.fullscreen_hotkey == "ctrl+alt+1"


def test_default_realtime_context_region_box_and_font_settings() -> None:
    cfg = AppConfig()

    assert cfg.trigger_hotkey == "f8"
    assert cfg.fullscreen_hotkey == "f8"
    assert cfg.fullscreen_realtime
    assert cfg.region_realtime
    assert cfg.merge_context
    assert cfg.show_region_box
    assert cfg.translation_font_size == TRANSLATION_FONT_SIZE_DEFAULT
    assert cfg.font_increase_hotkey == "ctrl+up"
    assert cfg.font_decrease_hotkey == "ctrl+down"


def test_hotkey_helpers_support_function_and_modifier_keys() -> None:
    modifiers, vk_code = parse_win32_hotkey("Ctrl+Shift+F8")
    assert modifiers == (Win32HotkeyListener.MOD_CONTROL | Win32HotkeyListener.MOD_SHIFT)
    assert vk_code == 0x77
    assert parse_win32_hotkey("F2") == (0, 0x71)
    assert parse_win32_hotkey("Ctrl+Up") == (Win32HotkeyListener.MOD_CONTROL, 0x26)
    assert to_pynput_hotkey("Ctrl+Shift+F8") == "<ctrl>+<shift>+<f8>"


def test_win32_hotkey_parser_rejects_missing_or_ambiguous_main_key() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_win32_hotkey("Ctrl+Shift")
    with pytest.raises(ValueError):
        parse_win32_hotkey("Ctrl+A+B")


def test_reset_config_preserves_service_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = AppConfig()
    cfg.engine = "openai"
    cfg.target_lang = "en"
    cfg.fullscreen_realtime = False
    cfg.region_realtime = False
    cfg.merge_context = False
    cfg.show_region_box = False
    cfg.translation_font_size = TRANSLATION_FONT_SIZE_MIN
    cfg.font_increase_hotkey = "ctrl+shift+up"
    cfg.font_decrease_hotkey = "ctrl+shift+down"
    cfg.google.proxy = "http://127.0.0.1:7890"
    cfg.baidu.app_id = "baidu-app-id"
    cfg.baidu.secret_key = "baidu-secret"
    cfg.tencent.secret_id = "tencent-secret-id"
    cfg.tencent.secret_key = "tencent-secret-key"
    cfg.tencent.region = "ap-tokyo"
    cfg.openai.base_url = "https://ark.example/v3"
    cfg.openai.api_key = "openai-api-key"
    cfg.openai.model = "ark-model"
    cfg.openai.timeout = 45
    cfg.ollama.base_url = "http://127.0.0.1:11435"
    cfg.ollama.model = "ollama-model"
    cfg.ollama.context = "custom context"
    save_config(cfg, path)

    reset = reset_config(path)
    loaded = load_config(path)

    for item in (reset, loaded):
        assert item.engine == "google"
        assert item.target_lang == "zh-CN"
        assert item.trigger_hotkey == "f8"
        assert item.fullscreen_hotkey == "f8"
        assert item.fullscreen_realtime
        assert item.region_realtime
        assert item.merge_context
        assert item.show_region_box
        assert item.translation_font_size == TRANSLATION_FONT_SIZE_DEFAULT
        assert item.font_increase_hotkey == "ctrl+up"
        assert item.font_decrease_hotkey == "ctrl+down"
        assert item.google.proxy == "http://127.0.0.1:7890"
        assert item.baidu.app_id == "baidu-app-id"
        assert item.baidu.secret_key == "baidu-secret"
        assert item.tencent.secret_id == "tencent-secret-id"
        assert item.tencent.secret_key == "tencent-secret-key"
        assert item.tencent.region == "ap-tokyo"
        assert item.openai.base_url == "https://ark.example/v3"
        assert item.openai.api_key == "openai-api-key"
        assert item.openai.model == "ark-model"
        assert item.openai.timeout == 45
        assert item.ollama.base_url == "http://127.0.0.1:11435"
        assert item.ollama.model == "ollama-model"
        assert item.ollama.context == "custom context"


def test_parse_llm_json_plain_fenced_and_think_blocks() -> None:
    assert _parse_llm_json('{"translations":["你好","世界"]}', 2) == ["你好", "世界"]
    assert _parse_llm_json('```json\n{"translations":["A"]}\n```', 2) == ["A", ""]
    assert _parse_llm_json('<think>reasoning should be ignored</think>{"translations":["最终译文"]}', 1) == ["最终译文"]


def test_openai_translator_requests_non_thinking_json_only_prompt(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"translations":["你好"]}'}}]}

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return FakeResponse()

    monkeypatch.setattr("gameocr.translation.requests.post", fake_post)

    config = AppConfig()
    config.openai.base_url = "https://llm.example/v1/chat/completions"
    config.openai.api_key = "test-key"
    config.openai.model = "test-model"

    output = OpenAITranslator(config).translate(["hello"], "en", "zh-CN")

    assert output.error is None
    assert output.translations == ["你好"]
    assert calls[0][0] == "https://llm.example/v1/chat/completions"
    payload = calls[0][2]
    assert "non-thinking mode" in payload["messages"][0]["content"]
    assert "chain-of-thought" in payload["messages"][0]["content"]
    assert "/no_think" in payload["messages"][1]["content"]
    assert "JSON" in payload["messages"][1]["content"]
    assert payload["thinking"] == {"type": "disabled"}


def test_openai_chat_completions_url_accepts_base_or_full_endpoint() -> None:
    assert _openai_chat_completions_url("https://llm.example/v1") == "https://llm.example/v1/chat/completions"
    assert (
        _openai_chat_completions_url("https://llm.example/v1/chat/completions")
        == "https://llm.example/v1/chat/completions"
    )
    assert (
        _openai_chat_completions_url("https://llm.example/v1/")
        == "https://llm.example/v1/chat/completions"
    )


def test_ollama_translator_sends_think_false(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"message": {"content": '{"translations":["你好"]}'}}

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse()

    monkeypatch.setattr("gameocr.translation.requests.post", fake_post)

    config = AppConfig()
    config.ollama.base_url = "http://127.0.0.1:11434"
    config.ollama.model = "gemma4:31b-cloud"

    output = OllamaTranslator(config).translate(["hello"], "en", "zh-CN")

    assert output.error is None
    assert output.translations == ["你好"]
    payload = calls[0][1]
    assert payload["think"] is False
    assert payload["stream"] is False
    assert "non-thinking mode" in payload["messages"][0]["content"]
    assert "/no_think" in payload["messages"][1]["content"]


def test_google_translator_batches_multiple_texts_in_one_request(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return [[["[[GOC_000]] 你好\n[[GOC_001]] 世界", None, None, None]]]

    def fake_get(url, params, proxies, timeout):
        calls.append((url, params, proxies, timeout))
        return FakeResponse()

    monkeypatch.setattr("gameocr.translation.requests.get", fake_get)

    output = GoogleTranslator(AppConfig()).translate(["hello", "world"], "en", "zh-CN")

    assert output.error is None
    assert output.translations == ["你好", "世界"]
    assert len(calls) == 1
    assert "hello" in calls[0][1]["q"]
    assert "world" in calls[0][1]["q"]


def test_google_batch_parser_falls_back_to_lines() -> None:
    assert _parse_indexed_batch_text("第一行\n第二行", 3) == ["第一行", "第二行", ""]


def test_mss_to_rgb_array_uses_numpy_memory_shape() -> None:
    class FakeRaw:
        width = 2
        height = 1
        rgb = bytes([1, 2, 3, 4, 5, 6])

    image = _mss_to_rgb_array(FakeRaw())

    assert isinstance(image, np.ndarray)
    assert image.shape == (1, 2, 3)
    assert image.dtype == np.uint8
    assert image.tolist() == [[[1, 2, 3], [4, 5, 6]]]


def test_translation_failure_returns_empty_translations_not_error_text() -> None:
    class FailingTranslator(BaseTranslator):
        backend_name = "failing"

        def _translate(self, texts, source_lang, target_lang):
            raise RuntimeError("network timeout")

    output = FailingTranslator(AppConfig()).translate(["hello", "world"], "en", "zh-CN")

    assert output.error == "failing 翻译失败: network timeout"
    assert output.translations == ["", ""]


def test_merge_sentence_lines_joins_wrapped_sentence() -> None:
    items = [
        OCRItem("これは長い台詞の", (100, 100, 260, 120)),
        OCRItem("続きです。", (102, 123, 220, 143)),
        OCRItem("MENU", (600, 100, 680, 120)),
    ]

    merged = merge_sentence_lines(items)

    assert [item.text for item in merged] == ["これは長い台詞の続きです。", "MENU"]
    assert merged[0].box == (100, 100, 260, 143)


def test_merge_sentence_lines_keeps_completed_sentences_separate() -> None:
    items = [
        OCRItem("Hello.", (100, 100, 180, 120)),
        OCRItem("World", (102, 123, 180, 143)),
    ]

    merged = merge_sentence_lines(items)

    assert [item.text for item in merged] == ["Hello.", "World"]


def test_merge_sentence_lines_rejects_loose_context_match() -> None:
    items = [
        OCRItem("Inventory", (100, 100, 260, 120)),
        OCRItem("Equipment", (190, 123, 490, 143)),
    ]

    merged = merge_sentence_lines(items)

    assert [item.text for item in merged] == ["Inventory", "Equipment"]


def test_merge_sentence_lines_rejects_wide_vertical_spacing() -> None:
    items = [
        OCRItem("これは長い台詞の", (100, 100, 260, 120)),
        OCRItem("別の段落です", (102, 130, 245, 150)),
    ]

    merged = merge_sentence_lines(items)

    assert [item.text for item in merged] == ["これは長い台詞の", "別の段落です"]


def test_merge_sentence_lines_rejects_moderate_line_gap() -> None:
    items = [
        OCRItem("これは長い台詞の", (100, 100, 260, 120)),
        OCRItem("少し離れた行です", (102, 125, 245, 145)),
    ]

    merged = merge_sentence_lines(items)

    assert [item.text for item in merged] == ["これは長い台詞の", "少し離れた行です"]


def test_merge_sentence_lines_rejects_only_moderate_overlap_context() -> None:
    items = [
        OCRItem("これは長い台詞の", (100, 100, 260, 120)),
        OCRItem("文脈が近いだけです", (150, 123, 330, 143)),
    ]

    merged = merge_sentence_lines(items)

    assert [item.text for item in merged] == ["これは長い台詞の", "文脈が近いだけです"]


def test_target_window_background_pauses_and_foreground_recovers(monkeypatch) -> None:
    class FakeOverlay:
        def __init__(self) -> None:
            self.clear_count = 0
            self.status_calls = []

        def clear(self) -> None:
            self.clear_count += 1

        def set_realtime_status(self, active: bool, visible: bool) -> None:
            self.status_calls.append((active, visible))

        def set_latency_status(self, ocr_ms: float, translate_ms: float, total_ms: float, visible: bool) -> None:
            self.status_calls.append(("latency", visible))

    config = AppConfig(target_window_title="Example Game")
    overlay = FakeOverlay()
    controller = TranslationController(config, overlay)  # type: ignore[arg-type]
    controller.fullscreen_busy = True
    logs = []
    controller.log.connect(logs.append)

    monkeypatch.setattr("gameocr.controller.is_target_window_foreground", lambda title: False)

    assert controller._pause_if_target_window_background("fullscreen")
    assert controller.target_window_paused
    assert not controller.fullscreen_busy
    assert overlay.clear_count == 1
    # The pause triggers set_realtime_status(False, False) then set_latency_status(…, False).
    assert overlay.status_calls[-2] == (False, False)
    assert overlay.status_calls[-1] == ("latency", False)
    assert logs == ["目标窗口不在前台，已暂停 OCR 翻译并隐藏悬浮窗"]

    assert controller._pause_if_target_window_background("fullscreen")
    assert overlay.clear_count == 2
    assert logs == ["目标窗口不在前台，已暂停 OCR 翻译并隐藏悬浮窗"]

    monkeypatch.setattr("gameocr.controller.is_target_window_foreground", lambda title: True)

    assert not controller._pause_if_target_window_background("fullscreen")
    assert not controller.target_window_paused
    assert logs[-1] == "目标窗口已回到前台，自动恢复 OCR 翻译"


def test_prepare_ocr_image_resizes_to_selected_height_and_maps_boxes() -> None:
    image = np.zeros((1080, 1920, 3), dtype=np.uint8)

    resized, scale_x, scale_y = prepare_ocr_image(image, "720p")
    mapped = map_ocr_items_to_original_coords([OCRItem("hello", (10, 20, 110, 60), 0.9)], (100, 200), scale_x, scale_y)

    assert resized.shape == (720, 1280, 3)
    assert scale_x == 1.5
    assert scale_y == 1.5
    assert mapped == [OCRItem("hello", (115, 230, 265, 290), 0.9)]


def test_prepare_ocr_image_original_keeps_image_size() -> None:
    image = np.zeros((360, 640, 3), dtype=np.uint8)

    resized, scale_x, scale_y = prepare_ocr_image(image, "original")

    assert resized.shape == image.shape
    assert scale_x == 1.0
    assert scale_y == 1.0


def test_prepare_translation_items_merges_context_only_when_enabled() -> None:
    items = [
        OCRItem("これは長い台詞の", (100, 100, 260, 120)),
        OCRItem("続きです。", (102, 123, 220, 143)),
        OCRItem("123", (300, 100, 340, 120)),
    ]

    unmerged_items, unmerged_translatable = prepare_translation_items(items, merge_context=False)
    merged_items, merged_translatable = prepare_translation_items(items, merge_context=True)

    assert [item.text for item in unmerged_items] == ["これは長い台詞の", "続きです。", "123"]
    assert [item.text for item in unmerged_translatable] == ["これは長い台詞の", "続きです。"]
    assert [item.text for item in merged_items] == ["これは長い台詞の続きです。", "123"]
    assert [item.text for item in merged_translatable] == ["これは長い台詞の続きです。"]


def test_filter_translatable_items_skips_numbers_and_single_letters() -> None:
    assert not should_translate_text("12345")
    assert not should_translate_text("  12,345  ")
    assert not should_translate_text("A")
    assert not should_translate_text("z.")
    assert should_translate_text("HP 100")
    assert should_translate_text("Hello")
    assert should_translate_text("あ")
    assert should_translate_text("中")

    items = [
        OCRItem("123", (0, 0, 10, 10)),
        OCRItem("A", (10, 0, 20, 10)),
        OCRItem("Hello", (20, 0, 60, 10)),
        OCRItem("あ", (60, 0, 70, 10)),
    ]

    filtered = filter_translatable_items(items)

    assert [item.text for item in filtered] == ["Hello", "あ"]


def test_overlay_manager_reuses_windows(monkeypatch) -> None:
    created = []
    destroyed = []

    class FakeBubble:
        def __init__(self, text: str, x: int, y: int):
            self.text = text
            self.x = x
            self.y = y
            self.theme = ""
            self.font_size = 0
            self.preferred_width = 0
            self.shown = 0
            self.hidden = 0
            self.raised = 0
            self.closed = False
            created.append(self)

        def set_theme(self, theme: str) -> None:
            self.theme = theme

        def set_font_size(self, font_size: int) -> None:
            self.font_size = font_size

        def set_preferred_width(self, width: int) -> None:
            self.preferred_width = width

        def update_text(self, text: str) -> None:
            self.text = text

        def update_content(self, text: str, x: int, y: int) -> None:
            self.update_text(text)
            self.move(x, y)

        def move(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

        def width(self) -> int:
            return 30

        def height(self) -> int:
            return 15

        def show(self) -> None:
            self.shown += 1

        def raise_(self) -> None:
            self.raised += 1

        def hide(self) -> None:
            self.hidden += 1

        def close(self) -> None:
            self.closed = True

        def deleteLater(self) -> None:
            destroyed.append(self)

    monkeypatch.setattr("gameocr.overlay.TranslationBubble", FakeBubble)

    manager = OverlayManager()
    manager.show_translations(
        [OCRItem("hello", (10, 20, 40, 35)), OCRItem("world", (50, 60, 80, 75))],
        ["你好", "世界"],
    )
    assert manager.active_count() == 2
    assert len(created) == 2
    assert [bubble.theme for bubble in created] == ["classic", "classic"]
    assert [bubble.font_size for bubble in created] == [TRANSLATION_FONT_SIZE_DEFAULT, TRANSLATION_FONT_SIZE_DEFAULT]

    manager.set_translation_font_size(18)
    assert [bubble.font_size for bubble in created] == [18, 18]

    manager.set_translation_theme("amber")
    assert [bubble.theme for bubble in created] == ["amber", "amber"]

    manager.set_translation_theme("unknown")
    assert [bubble.theme for bubble in created] == ["classic", "classic"]

    manager.set_translation_theme("blue")

    manager.show_translations(
        [OCRItem("hello2", (12, 22, 42, 37)), OCRItem("world2", (52, 62, 82, 77))],
        ["你好2", "世界2"],
    )
    assert manager.active_count() == 2
    assert len(created) == 2
    assert [bubble.text for bubble in created] == ["你好2", "世界2"]
    assert [bubble.theme for bubble in created] == ["blue", "blue"]
    assert [bubble.preferred_width for bubble in created] == [30, 30]
    assert [(bubble.x, bubble.y) for bubble in created] == [(12, 43), (52, 83)]

    manager.hide_all()
    assert [bubble.hidden for bubble in created] == [1, 1]
    manager.show_all()
    assert [bubble.shown for bubble in created] == [3, 3]
    assert [bubble.raised for bubble in created] == [3, 3]

    manager.show_translations([OCRItem("only", (1, 2, 3, 4))], ["仅一个"])
    assert manager.active_count() == 1
    assert len(created) == 2
    assert destroyed == [created[1]]
    assert created[1].closed


def test_restart_active_realtime_restarts_fullscreen_with_new_config(monkeypatch) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    config = AppConfig(fullscreen_realtime=True)
    overlay = FakeControllerOverlay()
    controller = TranslationController(config, overlay)  # type: ignore[arg-type]
    logs = []
    starts = []
    controller.log.connect(logs.append)
    monkeypatch.setattr(controller, "_start_worker", lambda mode, region: starts.append((mode, region, config.ocr.resolution)))

    controller.start_fullscreen_loop()
    assert controller.active_realtime_mode() == "fullscreen"
    assert starts[-1] == ("fullscreen", None, "original")

    config.ocr.resolution = "720p"
    config.engine = "ollama"
    restarted = controller.restart_active_realtime("配置已变更")

    assert restarted
    assert controller.active_realtime_mode() == "fullscreen"
    assert controller.fullscreen_refresh_count == 1
    assert starts[-1] == ("fullscreen", None, "720p")
    assert overlay.clear_count >= 2
    assert logs[-2] == "配置已变更，已自动重启全屏实时翻译"
    controller.stop_all()


def test_restart_active_realtime_restarts_region_and_syncs_region_box(monkeypatch) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    config = AppConfig(region_realtime=True, show_region_box=False)
    overlay = FakeControllerOverlay()
    controller = TranslationController(config, overlay)  # type: ignore[arg-type]
    starts = []
    monkeypatch.setattr(controller, "_start_worker", lambda mode, region: starts.append((mode, region, config.show_region_box)))

    controller.start_region_loop((10, 20, 300, 200))
    assert controller.active_realtime_mode() == "region"
    assert overlay.region_box_calls == []

    config.show_region_box = True
    restarted = controller.restart_active_realtime("配置已变更")

    assert restarted
    assert controller.active_realtime_mode() == "region"
    assert controller.region_refresh_count == 1
    assert starts[-1] == ("region", (10, 20, 300, 200), True)
    assert overlay.region_box_calls[-1] == ((10, 20, 300, 200), True)
    controller.stop_all()


class FakeControllerOverlay:
    def __init__(self) -> None:
        self.clear_count = 0
        self.clear_region_box_count = 0
        self.region_box_calls = []
        self.realtime_status_calls = []
        self.latency_status_calls = []
        self.translation_theme = ""
        self.translation_font_size = 0
        self.hidden = 0
        self.shown = 0

    def clear(self) -> None:
        self.clear_count += 1

    def clear_region_box(self) -> None:
        self.clear_region_box_count += 1

    def set_region_box(self, rect, visible: bool) -> None:
        self.region_box_calls.append((rect, visible))

    def set_translation_theme(self, theme: str) -> None:
        self.translation_theme = theme

    def set_translation_font_size(self, font_size: int) -> None:
        self.translation_font_size = font_size

    def set_realtime_status(self, active: bool, visible: bool) -> None:
        self.realtime_status_calls.append((active, visible))

    def set_latency_status(self, ocr_ms: float, translate_ms: float, total_ms: float, visible: bool) -> None:
        self.latency_status_calls.append((ocr_ms, translate_ms, total_ms, visible))

    def hide_all(self) -> None:
        self.hidden += 1

    def show_all(self) -> None:
        self.shown += 1

    def active_count(self) -> int:
        return 0


def test_expanded_translation_width_limit_allows_a_few_extra_characters() -> None:
    assert _expanded_translation_width_limit(40, 720, 12) == 88
    assert _expanded_translation_width_limit(40, 720, 4) == 72
    assert _expanded_translation_width_limit(700, 720, 12) == 720


def test_overlay_manager_region_box_toggle(monkeypatch) -> None:
    created = []
    destroyed = []

    class FakeRegionBox:
        def __init__(self, rect):
            self.rect = rect
            self.shown = 0
            self.hidden = 0
            self.raised = 0
            self.closed = False
            created.append(self)

        def update_rect(self, rect) -> None:
            self.rect = rect

        def show(self) -> None:
            self.shown += 1

        def raise_(self) -> None:
            self.raised += 1

        def hide(self) -> None:
            self.hidden += 1

        def close(self) -> None:
            self.closed = True

        def deleteLater(self) -> None:
            destroyed.append(self)

    monkeypatch.setattr("gameocr.overlay.RegionBoxOverlay", FakeRegionBox)

    manager = OverlayManager()
    manager.set_region_box((10, 20, 300, 200), True)

    assert len(created) == 1
    assert created[0].rect == (10, 20, 300, 200)
    assert created[0].shown == 1
    assert created[0].raised == 1

    manager.set_region_box((15, 25, 250, 180), True)
    assert len(created) == 1
    assert created[0].rect == (15, 25, 250, 180)
    assert created[0].shown == 2

    manager.hide_all()
    assert created[0].hidden == 1
    manager.show_all()
    assert created[0].shown == 3
    assert created[0].raised == 3

    manager.clear_region_box()
    assert destroyed == [created[0]]
    assert created[0].closed


def test_place_without_overlap_moves_bubble(monkeypatch) -> None:
    monkeypatch.setattr("gameocr.overlay._screen_geometry_for_point", lambda x, y: QRect(0, 0, 300, 200))

    first = QRect(50, 50, 80, 30).adjusted(-4, -4, 4, 4)
    x, y = _place_without_overlap(55, 55, 80, 30, [first])

    candidate = QRect(x, y, 80, 30)
    assert not candidate.intersects(first)
    assert (x, y) != (55, 55)
