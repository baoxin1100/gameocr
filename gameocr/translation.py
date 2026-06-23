from __future__ import annotations

import abc
import hashlib
import hmac
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Type
from urllib.parse import urlencode

import requests

from .config import (
    AppConfig,
    ENGINE_BAIDU,
    ENGINE_GOOGLE,
    ENGINE_OLLAMA,
    ENGINE_OPENAI,
    ENGINE_TENCENT,
)


LANG_MAP_BAIDU = {
    "auto": "auto",
    "zh-CN": "zh",
    "zh-TW": "cht",
    "en": "en",
    "ja": "jp",
    "ko": "kor",
    "fr": "fra",
    "de": "de",
    "es": "spa",
    "ru": "ru",
}

LANG_MAP_TENCENT = {
    "auto": "auto",
    "zh-CN": "zh",
    "zh-TW": "zh-TW",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "ru": "ru",
}

LANG_NAME = {
    "auto": "自动检测",
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "ru": "Русский",
}


@dataclass
class TranslationOutput:
    translations: List[str]
    error: Optional[str] = None
    backend: str = ""
    elapsed_ms: float = 0.0
    total_elapsed_ms: float = 0.0


class BaseTranslator(abc.ABC):
    backend_name = "base"

    def __init__(self, config: AppConfig):
        self.config = config

    def translate(self, texts: Sequence[str], source_lang: str, target_lang: str) -> TranslationOutput:
        start = time.perf_counter()
        clean_texts = [text.strip() for text in texts]
        if not clean_texts:
            return TranslationOutput([], backend=self.backend_name)
        try:
            result = self._translate(clean_texts, source_lang, target_lang)
            if len(result) != len(clean_texts):
                result = (result + [""] * len(clean_texts))[: len(clean_texts)]
            elapsed_ms = (time.perf_counter() - start) * 1000
            return TranslationOutput(result, backend=self.backend_name, elapsed_ms=elapsed_ms)
        except Exception as exc:  # noqa: BLE001 - API failures must not interrupt OCR loop
            elapsed_ms = (time.perf_counter() - start) * 1000
            message = f"{self.backend_name} 翻译失败: {exc}"
            return TranslationOutput(["" for _ in clean_texts], error=message, backend=self.backend_name, elapsed_ms=elapsed_ms)

    @abc.abstractmethod
    def _translate(self, texts: Sequence[str], source_lang: str, target_lang: str) -> List[str]:
        raise NotImplementedError


class GoogleTranslator(BaseTranslator):
    backend_name = "google"

    def _translate(self, texts: Sequence[str], source_lang: str, target_lang: str) -> List[str]:
        proxies = None
        if self.config.google.proxy:
            proxies = {"http": self.config.google.proxy, "https": self.config.google.proxy}

        source = "auto" if source_lang == "auto" else source_lang
        target = target_lang
        query = _build_indexed_batch_text(texts)
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": source,
                "tl": target,
                "dt": "t",
                "q": query,
            },
            proxies=proxies,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        translated = "".join(part[0] for part in data[0] if part and part[0])
        return _parse_indexed_batch_text(translated, len(texts))


class BaiduTranslator(BaseTranslator):
    backend_name = "baidu"

    def _translate(self, texts: Sequence[str], source_lang: str, target_lang: str) -> List[str]:
        cfg = self.config.baidu
        if not cfg.app_id or not cfg.secret_key:
            raise RuntimeError("百度翻译 APP ID 或密钥为空")

        query = "\n".join(texts)
        salt = str(random.randint(32768, 65536))
        sign_raw = f"{cfg.app_id}{query}{salt}{cfg.secret_key}"
        sign = hashlib.md5(sign_raw.encode("utf-8")).hexdigest()
        payload = {
            "q": query,
            "from": LANG_MAP_BAIDU.get(source_lang, source_lang),
            "to": LANG_MAP_BAIDU.get(target_lang, target_lang),
            "appid": cfg.app_id,
            "salt": salt,
            "sign": sign,
        }
        response = requests.post("https://fanyi-api.baidu.com/api/trans/vip/translate", data=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if "error_code" in data:
            raise RuntimeError(f"{data.get('error_code')}: {data.get('error_msg')}")
        translated = [item.get("dst", "") for item in data.get("trans_result", [])]
        return _fit_lines(translated, len(texts))


class TencentTranslator(BaseTranslator):
    backend_name = "tencent"

    SERVICE = "tmt"
    HOST = "tmt.tencentcloudapi.com"
    ENDPOINT = "https://tmt.tencentcloudapi.com"
    ACTION = "TextTranslateBatch"
    VERSION = "2018-03-21"

    def _translate(self, texts: Sequence[str], source_lang: str, target_lang: str) -> List[str]:
        cfg = self.config.tencent
        if not cfg.secret_id or not cfg.secret_key:
            raise RuntimeError("腾讯云 SecretId 或 SecretKey 为空")

        timestamp = int(time.time())
        payload = {
            "Source": LANG_MAP_TENCENT.get(source_lang, source_lang),
            "Target": LANG_MAP_TENCENT.get(target_lang, target_lang),
            "ProjectId": 0,
            "SourceTextList": list(texts),
        }
        payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = self._sign_v3(payload_json, timestamp, cfg.secret_id, cfg.secret_key)
        response = requests.post(self.ENDPOINT, data=payload_json.encode("utf-8"), headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        if "Error" in data.get("Response", {}):
            err = data["Response"]["Error"]
            raise RuntimeError(f"{err.get('Code')}: {err.get('Message')}")
        translated = data.get("Response", {}).get("TargetTextList", [])
        return _fit_lines(translated, len(texts))

    def _sign_v3(self, payload: str, timestamp: int, secret_id: str, secret_key: str) -> Dict[str, str]:
        algorithm = "TC3-HMAC-SHA256"
        date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                f"content-type:application/json; charset=utf-8\nhost:{self.HOST}\n",
                "content-type;host",
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            ]
        )
        credential_scope = f"{date}/{self.SERVICE}/tc3_request"
        string_to_sign = "\n".join(
            [
                algorithm,
                str(timestamp),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = _hmac_sha256(secret_date, self.SERVICE)
        secret_signing = _hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"{algorithm} Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders=content-type;host, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.HOST,
            "X-TC-Action": self.ACTION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": self.VERSION,
            "X-TC-Region": self.config.tencent.region,
        }


class OpenAITranslator(BaseTranslator):
    backend_name = "openai"

    def _translate(self, texts: Sequence[str], source_lang: str, target_lang: str) -> List[str]:
        cfg = self.config.openai
        if not cfg.base_url or not cfg.api_key or not cfg.model:
            raise RuntimeError("OpenAI 兼容 API 地址、API Key 或模型名为空")

        prompt = _build_llm_prompt(texts, source_lang, target_lang)
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": _llm_system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "temperature": cfg.temperature,
            "thinking": {"type": "disabled"},
        }
        response = requests.post(_openai_chat_completions_url(cfg.base_url), headers=headers, json=payload, timeout=cfg.timeout)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_llm_json(content, len(texts))


class OllamaTranslator(BaseTranslator):
    backend_name = "ollama"

    def _translate(self, texts: Sequence[str], source_lang: str, target_lang: str) -> List[str]:
        cfg = self.config.ollama
        if not cfg.base_url or not cfg.model:
            raise RuntimeError("Ollama 服务地址或模型名为空")

        prompt = _build_llm_prompt(texts, source_lang, target_lang)
        if cfg.context:
            prompt = f"{cfg.context}\n\n{prompt}"
        response = requests.post(
            f"{cfg.base_url.rstrip('/')}/api/chat",
            json={
                "model": cfg.model,
                "messages": [
                    {"role": "system", "content": _llm_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": cfg.temperature},
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content", "")
        return _parse_llm_json(content, len(texts))


TRANSLATOR_CLASSES: Dict[str, Type[BaseTranslator]] = {
    ENGINE_GOOGLE: GoogleTranslator,
    ENGINE_BAIDU: BaiduTranslator,
    ENGINE_TENCENT: TencentTranslator,
    ENGINE_OPENAI: OpenAITranslator,
    ENGINE_OLLAMA: OllamaTranslator,
}


def create_translator(config: AppConfig) -> BaseTranslator:
    cls = TRANSLATOR_CLASSES.get(config.engine, GoogleTranslator)
    return cls(config)


def _fit_lines(lines: List[str], expected: int) -> List[str]:
    if len(lines) == expected:
        return lines
    if len(lines) == 1 and expected > 1:
        return lines[0].splitlines()[:expected] + [""] * max(0, expected - len(lines[0].splitlines()))
    return (lines + [""] * expected)[:expected]


def _build_indexed_batch_text(texts: Sequence[str]) -> str:
    """Pack multiple OCR snippets into one translation request.

    Google Translate's free endpoint has no formal batch JSON API. Sending one
    indexed multiline payload keeps one network round-trip per OCR frame while
    preserving item order well enough for HUD/subtitle text.
    """

    return "\n".join(f"[[GOC_{index:03d}]] {text}" for index, text in enumerate(texts))


def _parse_indexed_batch_text(text: str, expected: int) -> List[str]:
    marker_pattern = r"\[\[\s*GOC[_\s-]*(\d{3})\s*\]\]"
    matches = list(re.finditer(marker_pattern, text, flags=re.IGNORECASE))
    if matches:
        results = ["" for _ in range(expected)]
        for index, match in enumerate(matches):
            item_index = int(match.group(1))
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            if 0 <= item_index < expected:
                results[item_index] = text[start:end].strip(" \t\r\n:：-—")
        return results

    # Fallback for cases where Google rewrites marker brackets but keeps
    # line breaks. This still avoids N HTTP calls and preserves most UI rows.
    return _fit_lines([line.strip() for line in text.splitlines() if line.strip()], expected)


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _openai_chat_completions_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def _llm_system_prompt() -> str:
    return (
        "You are a precise game text translation engine. "
        "Use non-thinking mode. Do not think step-by-step. "
        "Do not output reasoning, analysis, chain-of-thought, markdown, or <think> tags. "
        "Return the final translation result as JSON only."
    )


def _build_llm_prompt(texts: Sequence[str], source_lang: str, target_lang: str) -> str:
    source = LANG_NAME.get(source_lang, source_lang)
    target = LANG_NAME.get(target_lang, target_lang)
    lines = "\n".join(f"{idx}. {text}" for idx, text in enumerate(texts))
    return (
        "/no_think\n"
        f"Translate the following game OCR texts from {source} to {target}. "
        "Use non-thinking mode and output only the final answer. "
        "Do not include reasoning, analysis, explanations, markdown, or <think> tags. "
        "Keep names, commands and UI placeholders natural. "
        "Return strictly valid JSON in this shape: {\"translations\":[\"...\"]}. "
        "The translations array length must equal the input count and preserve order.\n\n"
        f"{lines}"
    )


def _parse_llm_json(content: str, expected: int) -> List[str]:
    text = _strip_think_blocks(content.strip())
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    data = json.loads(text)
    translations = data.get("translations", [])
    if not isinstance(translations, list):
        raise RuntimeError("LLM 返回 JSON 中 translations 不是数组")
    return _fit_lines([str(item) for item in translations], expected)


def _strip_think_blocks(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
