import json
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict


CONFIG_DIR = Path.home() / ".gameocr"
CONFIG_FILE = CONFIG_DIR / "config.json"
CONFIG_SCHEMA_VERSION = 3


LANGUAGES = {
    "auto": "自动检测",
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
    "en": "英文",
    "ja": "日文",
    "ko": "韩文",
    "fr": "法文",
    "de": "德文",
    "es": "西班牙文",
    "ru": "俄文",
}

ENGINE_GOOGLE = "google"
ENGINE_BAIDU = "baidu"
ENGINE_TENCENT = "tencent"
ENGINE_OPENAI = "openai"
ENGINE_OLLAMA = "ollama"

ENGINE_LABELS = {
    ENGINE_GOOGLE: "谷歌翻译",
    ENGINE_BAIDU: "百度翻译 API",
    ENGINE_TENCENT: "腾讯翻译君 API",
    ENGINE_OPENAI: "OpenAI 兼容 LLM API",
    ENGINE_OLLAMA: "Ollama 本地大模型",
}

OCR_RESOLUTION_ORIGINAL = "original"
OCR_RESOLUTION_LABELS = {
    OCR_RESOLUTION_ORIGINAL: "原始大小",
    "1080p": "1080p",
    "720p": "720p",
    "360p": "360p",
}
OCR_TARGET_HEIGHTS = {
    "1080p": 1080,
    "720p": 720,
    "360p": 360,
}

TRANSLATION_THEME_DEFAULT = "classic"
TRANSLATION_THEME_LABELS = {
    TRANSLATION_THEME_DEFAULT: "经典深色（当前默认）",
    "amber": "琥珀黑金",
    "blue": "蓝色夜光",
    "green": "绿色护眼",
    "light": "浅色纸张",
    "purple": "紫色霓虹",
}

TRANSLATION_FONT_SIZE_MIN = 8
TRANSLATION_FONT_SIZE_MAX = 48
TRANSLATION_FONT_SIZE_DEFAULT = 11

TRANSLATION_SCOPE_FULLSCREEN = "fullscreen"
TRANSLATION_SCOPE_REGION = "region"
TRANSLATION_SCOPE_LABELS = {
    TRANSLATION_SCOPE_FULLSCREEN: "整页翻译",
    TRANSLATION_SCOPE_REGION: "选区翻译",
}

TRIGGER_MODE_ONCE = "once"
TRIGGER_MODE_REALTIME = "realtime"
TRIGGER_MODE_LABELS = {
    TRIGGER_MODE_ONCE: "单次翻译",
    TRIGGER_MODE_REALTIME: "实时翻译",
}


TENCENT_REGIONS = {
    "ap-shanghai": "上海 ap-shanghai",
    "ap-guangzhou": "广州 ap-guangzhou",
    "ap-beijing": "北京 ap-beijing",
    "ap-chengdu": "成都 ap-chengdu",
    "ap-chongqing": "重庆 ap-chongqing",
    "ap-hongkong": "中国香港 ap-hongkong",
    "ap-singapore": "新加坡 ap-singapore",
    "ap-tokyo": "东京 ap-tokyo",
    "ap-seoul": "首尔 ap-seoul",
    "ap-bangkok": "曼谷 ap-bangkok",
    "na-siliconvalley": "硅谷 na-siliconvalley",
    "na-ashburn": "弗吉尼亚 na-ashburn",
    "eu-frankfurt": "法兰克福 eu-frankfurt",
}


@dataclass
class GoogleConfig:
    proxy: str = ""


@dataclass
class BaiduConfig:
    app_id: str = ""
    secret_key: str = ""


@dataclass
class TencentConfig:
    secret_id: str = ""
    secret_key: str = ""
    region: str = "ap-shanghai"


@dataclass
class OpenAIConfig:
    base_url: str = "https://api.openai.com/v1/chat/completions"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout: float = 30.0


@dataclass
class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "gemma4:31b-cloud"
    context: str = ""


@dataclass
class OCRConfig:
    model_dir: str = "models/paddleocr"
    device: str = "AUTO"
    use_openvino: bool = True
    min_confidence: float = 0.35
    resolution: str = OCR_RESOLUTION_ORIGINAL


@dataclass
class AppConfig:
    config_version: int = CONFIG_SCHEMA_VERSION
    engine: str = ENGINE_GOOGLE
    source_lang: str = "auto"
    target_lang: str = "zh-CN"
    trigger_hotkey: str = "f8"
    translation_scope: str = TRANSLATION_SCOPE_FULLSCREEN
    trigger_mode: str = TRIGGER_MODE_REALTIME
    fullscreen_hotkey: str = "f8"
    region_hotkey: str = "f2"
    font_increase_hotkey: str = "ctrl+up"
    font_decrease_hotkey: str = "ctrl+down"
    refresh_interval: float = 0.5
    fullscreen_realtime: bool = True
    region_realtime: bool = True
    show_realtime_status: bool = True
    show_latency_status: bool = True
    merge_context: bool = True
    show_region_box: bool = True
    translation_theme: str = TRANSLATION_THEME_DEFAULT
    translation_font_size: int = TRANSLATION_FONT_SIZE_DEFAULT
    target_window_title: str = ""
    google: GoogleConfig = field(default_factory=GoogleConfig)
    baidu: BaiduConfig = field(default_factory=BaiduConfig)
    tencent: TencentConfig = field(default_factory=TencentConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)

    def normalize(self) -> None:
        self.config_version = CONFIG_SCHEMA_VERSION
        if self.engine not in ENGINE_LABELS:
            self.engine = ENGINE_GOOGLE
        if self.source_lang not in LANGUAGES:
            self.source_lang = "auto"
        if self.target_lang not in LANGUAGES or self.target_lang == "auto":
            self.target_lang = "zh-CN"
        if self.translation_scope not in TRANSLATION_SCOPE_LABELS:
            self.translation_scope = TRANSLATION_SCOPE_FULLSCREEN
        if self.trigger_mode not in TRIGGER_MODE_LABELS:
            self.trigger_mode = TRIGGER_MODE_REALTIME
        self.refresh_interval = max(0.1, min(5.0, float(self.refresh_interval or 0.5)))
        self.fullscreen_hotkey = normalize_hotkey(self.fullscreen_hotkey or "f8")
        self.region_hotkey = normalize_hotkey(self.region_hotkey or "f2")
        self.trigger_hotkey = normalize_hotkey(self.trigger_hotkey or self.fullscreen_hotkey or "f8")
        self.font_increase_hotkey = normalize_hotkey(self.font_increase_hotkey or "ctrl+up")
        self.font_decrease_hotkey = normalize_hotkey(self.font_decrease_hotkey or "ctrl+down")
        try:
            self.translation_font_size = int(self.translation_font_size)
        except (TypeError, ValueError):
            self.translation_font_size = TRANSLATION_FONT_SIZE_DEFAULT
        self.translation_font_size = max(
            TRANSLATION_FONT_SIZE_MIN,
            min(TRANSLATION_FONT_SIZE_MAX, self.translation_font_size),
        )
        self.target_window_title = str(self.target_window_title or "").strip()
        if self.ocr.resolution not in OCR_RESOLUTION_LABELS:
            self.ocr.resolution = OCR_RESOLUTION_ORIGINAL
        if self.translation_theme not in TRANSLATION_THEME_LABELS:
            self.translation_theme = TRANSLATION_THEME_DEFAULT
        if self.tencent.region not in TENCENT_REGIONS:
            self.tencent.region = "ap-shanghai"


def normalize_hotkey(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    aliases = {
        "esc": "escape",
        "ctrl": "ctrl",
        "control": "ctrl",
        "cmd": "cmd",
        "win": "cmd",
        "windows": "cmd",
        "alt": "alt",
        "shift": "shift",
        "return": "enter",
    }
    value = value.replace(" ", "")
    parts = [aliases.get(part, part) for part in value.replace("-", "+").split("+") if part]
    return "+".join(parts)


def _field_default(item: Any) -> Any:
    if item.default is not MISSING:
        return item.default
    if item.default_factory is not MISSING:  # type: ignore[attr-defined]
        return item.default_factory()  # type: ignore[misc]
    return None


def _dataclass_from_dict(cls: type, raw: Dict[str, Any]) -> Any:
    raw = raw or {}
    kwargs = {}
    for item in fields(cls):
        value = raw.get(item.name, _field_default(item))
        if is_dataclass(item.type):
            value = _dataclass_from_dict(item.type, value if isinstance(value, dict) else {})
        kwargs[item.name] = value
    return cls(**kwargs)


def _migrate_legacy_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate saved configs created before default trigger hotkey changed.

    Existing users may already have ``trigger_hotkey=f1`` persisted from the
    old application default. Without this migration the GUI will continue to
    display an obsolete default forever even though the code default is now F8.
    """

    if not isinstance(raw, dict):
        return {}
    migrated = dict(raw)
    version = int(migrated.get("config_version") or 1)
    if version < CONFIG_SCHEMA_VERSION:
        trigger_hotkey = normalize_hotkey(str(migrated.get("trigger_hotkey") or ""))
        fullscreen_hotkey = normalize_hotkey(str(migrated.get("fullscreen_hotkey") or ""))
        if trigger_hotkey in {"", "f1", "alt+q"} and fullscreen_hotkey in {"", "f1", "alt+q"}:
            migrated["trigger_hotkey"] = "f8"
            migrated["fullscreen_hotkey"] = "f8"
        migrated["config_version"] = CONFIG_SCHEMA_VERSION
    return migrated


def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    if not path.exists():
        cfg = AppConfig()
        cfg.normalize()
        return cfg
    try:
        raw = _migrate_legacy_defaults(json.loads(path.read_text(encoding="utf-8")))
        cfg = _dataclass_from_dict(AppConfig, raw)
        cfg.normalize()
        return cfg
    except Exception:
        cfg = AppConfig()
        cfg.normalize()
        return cfg


def save_config(config: AppConfig, path: Path = CONFIG_FILE) -> None:
    config.normalize()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")


def reset_config(path: Path = CONFIG_FILE, preserve_service_settings: bool = True) -> AppConfig:
    """Reset UI/runtime options while keeping user-entered service credentials.

    The reset button should restore behavioral defaults without erasing values
    that are tedious to re-enter, such as API keys, secret keys, endpoint URLs,
    model names, proxy addresses and Ollama/OpenAI connection settings.
    """

    previous = load_config(path) if preserve_service_settings and path.exists() else None
    cfg = AppConfig()
    if previous is not None:
        cfg.google = previous.google
        cfg.baidu = previous.baidu
        cfg.tencent = previous.tencent
        cfg.openai = previous.openai
        cfg.ollama = previous.ollama
    save_config(cfg, path)
    return cfg
