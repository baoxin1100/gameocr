from __future__ import annotations

import importlib
import inspect
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np
from PIL import Image

from .config import OCRConfig


@dataclass
class OCRItem:
    text: str
    box: Tuple[int, int, int, int]
    confidence: float = 1.0


@dataclass
class OCRRunInfo:
    items: List[OCRItem]
    elapsed_ms: float
    backend: str
    error: Optional[str] = None


class OCRProcessor:
    """PaddleOCR ONNX wrapper with OpenVINO acceleration.

    The Python package named `onnxocr` has had several public APIs across
    releases/forks. This wrapper detects common class names and constructor
    signatures at runtime, so the GUI/business layer stays stable.

    OCR model is loaded once at application startup and reused for all frames.
    """

    _shared: Optional["OCRProcessor"] = None
    _shared_lock = threading.Lock()

    def __init__(self, config: OCRConfig):
        self.config = config
        self.engine: Any = None
        self.backend_name = "unloaded"
        self.openvino_devices: List[str] = []
        self.load_error: Optional[str] = None
        self._onnxruntime_openvino_active = False
        self._onnxruntime_openvino_disabled = False
        self._dll_dir_handles: list[Any] = []
        self._load()

    @classmethod
    def shared(cls, config: OCRConfig) -> "OCRProcessor":
        # QThreadPool workers may race during the first OCR trigger if startup
        # preloading failed/was skipped. Guard creation so the OpenVINO/PaddleOCR
        # model is still loaded exactly once and then reused for every frame.
        if cls._shared is None:
            with cls._shared_lock:
                if cls._shared is None:
                    cls._shared = cls(config)
        return cls._shared

    def _load(self) -> None:
        if self.config.use_openvino:
            self._add_runtime_dll_directories()
            os.environ.setdefault("ORT_OPENVINO_DEVICE_TYPE", self.config.device)
            os.environ.setdefault("ORT_OPENVINO_ENABLE_VPU_FAST_COMPILE", "1")

        model_dir = self._resource_path(self.config.model_dir)
        # Import/configure ONNXRuntime before directly touching OpenVINO runtime
        # APIs. In PyInstaller one-file bundles this keeps the ORT OpenVINO
        # provider's native dependency loading path deterministic.
        self._configure_onnxocr_runtime()

        if self.config.use_openvino:
            try:
                try:
                    from openvino.runtime import Core  # type: ignore
                except Exception:
                    from openvino import Core  # type: ignore
                self.openvino_devices = list(Core().available_devices)
            except Exception:
                self.openvino_devices = []
        candidates = [
            ("onnxocr.onnx_paddleocr", "ONNXPaddleOcr"),
            ("onnxocr.onnx_paddleocr", "PaddleOcrONNX"),
            ("onnxocr.paddleocr", "ONNXPaddleOcr"),
            ("onnxocr", "ONNXPaddleOcr"),
            ("onnxocr", "PaddleOcrONNX"),
            ("onnxocr", "OCR"),
        ]

        errors: List[str] = []
        for module_name, class_name in candidates:
            try:
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name)
                self.engine = self._instantiate_engine(cls, model_dir)
                if self.config.use_openvino and self._onnxruntime_openvino_active:
                    ov_suffix = f" + OpenVINO({','.join(self.openvino_devices) or self.config.device})"
                elif self.config.use_openvino:
                    ov_suffix = " + CPU fallback(OpenVINO EP unavailable)"
                else:
                    ov_suffix = ""
                self.backend_name = f"{module_name}.{class_name}{ov_suffix}"
                return
            except Exception as exc:  # noqa: BLE001 - try next compatible public API
                errors.append(f"{module_name}.{class_name}: {exc}")

        self.load_error = (
            "无法加载 onnxocr PaddleOCR 模型。程序已尝试 onnxocr 内置模型与 models/paddleocr 自定义模型；"
            "请确认 onnxocr 包完整、内置 models/fonts 资源已打包，或 models/paddleocr 下存在 det.onnx/rec.onnx/cls.onnx。\n"
            + "\n".join(errors)
        )
        self.backend_name = "load_failed"

    def _resource_path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path

        candidates = [Path.cwd() / path]
        frozen_base = getattr(sys, "_MEIPASS", None)
        if frozen_base:
            candidates.append(Path(frozen_base) / path)
        candidates.append(Path(__file__).resolve().parent.parent / path)

        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[1] if frozen_base and len(candidates) > 1 else candidates[0]

    def _onnxocr_package_dir(self) -> Optional[Path]:
        try:
            import onnxocr.utils as onnxocr_utils  # type: ignore

            module_dir = getattr(onnxocr_utils, "module_dir", None)
            if module_dir:
                return Path(module_dir)
            return Path(onnxocr_utils.__file__).resolve().parent
        except Exception:
            return None

    def _onnxocr_resource(self, *parts: str) -> Optional[Path]:
        package_dir = self._onnxocr_package_dir()
        if not package_dir:
            return None
        candidate = package_dir.joinpath(*parts)
        return candidate if candidate.exists() else None

    def _add_runtime_dll_directories(self) -> None:
        """Expose OpenVINO/ONNXRuntime native DLL directories on Windows.

        PyInstaller one-file extraction preserves many DLLs under package-like
        folders such as ``openvino/libs`` and ``onnxruntime/capi``. Windows does
        not always search those folders when ONNXRuntime later loads
        ``onnxruntime_providers_openvino.dll``, so add them explicitly and keep
        the returned handles alive for the process lifetime.
        """

        if os.name != "nt" or not hasattr(os, "add_dll_directory"):
            return

        candidates: list[Path] = []
        frozen_base = getattr(sys, "_MEIPASS", None)
        if frozen_base:
            base = Path(frozen_base)
            candidates.extend(
                [
                    base / "onnxruntime" / "capi",
                    base / "openvino" / "libs",
                    base / "PyQt5" / "Qt5" / "bin",
                    base,
                ]
            )

        for module_name in ("onnxruntime", "openvino"):
            try:
                spec = importlib.util.find_spec(module_name)
            except Exception:
                spec = None
            if spec and spec.origin:
                package_dir = Path(spec.origin).resolve().parent
                candidates.extend(
                    [
                        package_dir,
                        package_dir / "libs",
                        package_dir / "capi",
                    ]
                )

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate.exists():
                continue
            key = str(candidate.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                self._dll_dir_handles.append(os.add_dll_directory(str(candidate)))
                os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                continue

    def _resolve_onnxocr_model_kwargs(self, model_dir: Path) -> dict[str, str]:
        """Build explicit model/font paths for onnxocr's ONNXPaddleOcr(**kwargs).

        The bundled onnxocr version exposes ONNXPaddleOcr(**kwargs), not a
        top-level OCR class. Passing explicit paths avoids relying on package
        defaults after PyInstaller extracts resources to a temporary _MEIPASS
        directory. If users place det/rec/cls ONNX files under models/paddleocr,
        those files override the built-in onnxocr ppocrv5 models.
        """

        kwargs: dict[str, str] = {}
        defaults = {
            "det_model_dir": self._onnxocr_resource("models", "ppocrv5", "det", "det.onnx"),
            "rec_model_dir": self._onnxocr_resource("models", "ppocrv5", "rec", "rec.onnx"),
            "cls_model_dir": self._onnxocr_resource("models", "ppocrv5", "cls", "cls.onnx")
            or self._onnxocr_resource("models", "ppocrv4", "cls", "cls.onnx"),
            "rec_char_dict_path": self._onnxocr_resource("models", "ppocrv5", "ppocrv5_dict.txt")
            or self._onnxocr_resource("models", "ch_ppocr_server_v2.0", "ppocr_keys_v1.txt"),
            "vis_font_path": self._onnxocr_resource("fonts", "simfang.ttf"),
        }
        for key, value in defaults.items():
            if value:
                kwargs[key] = str(value)

        custom_paths = {
            "det_model_dir": model_dir / "det.onnx",
            "rec_model_dir": model_dir / "rec.onnx",
            "cls_model_dir": model_dir / "cls.onnx",
        }
        for key, value in custom_paths.items():
            if value.exists():
                kwargs[key] = str(value)

        for dict_name in ("ppocrv5_dict.txt", "ppocr_keys_v1.txt", "dict.txt"):
            value = model_dir / dict_name
            if value.exists():
                kwargs["rec_char_dict_path"] = str(value)
                break

        font_path = model_dir / "simfang.ttf"
        if font_path.exists():
            kwargs["vis_font_path"] = str(font_path)

        return kwargs

    def _configure_onnxocr_runtime(self) -> None:
        """Patch onnxocr's ONNXRuntime provider selection to prefer OpenVINO.

        The current onnxocr release hard-codes CUDA/CPU providers in
        PredictBase.get_onnx_session(). On machines without CUDA this causes
        warnings and prevents OpenVINO acceleration from being used. The patch is
        intentionally small and only changes provider selection; all preprocessing
        and postprocessing still come from onnxocr.
        """

        try:
            import onnxruntime as ort  # type: ignore
            import onnxocr.predict_base as predict_base  # type: ignore
        except Exception:
            return

        available = ort.get_available_providers()
        openvino_requested = self.config.use_openvino and "OpenVINOExecutionProvider" in available

        base_cls = predict_base.PredictBase
        if not hasattr(base_cls, "_gameocr_original_get_onnx_session"):
            setattr(base_cls, "_gameocr_original_get_onnx_session", base_cls.get_onnx_session)

        def build_providers() -> list[Any]:
            providers: list[Any] = []
            if openvino_requested and not self._onnxruntime_openvino_disabled:
                providers.append("OpenVINOExecutionProvider")
            if "CPUExecutionProvider" in available:
                providers.append("CPUExecutionProvider")
            return providers or available or ["CPUExecutionProvider"]

        def get_onnx_session(_instance: Any, model_path: str, _use_gpu: bool) -> Any:
            session = ort.InferenceSession(str(model_path), providers=build_providers())
            active_providers = session.get_providers()
            if "OpenVINOExecutionProvider" in active_providers:
                self._onnxruntime_openvino_active = True
            elif openvino_requested:
                # ONNXRuntime can silently fall back to CPU after provider DLL
                # load errors. Disable further OpenVINO attempts in this process
                # to avoid repeating slow failing provider initialization.
                self._onnxruntime_openvino_disabled = True
            return session

        base_cls.get_onnx_session = get_onnx_session

    def _instantiate_engine(self, cls: type, model_dir: Path) -> Any:
        signature = inspect.signature(cls)
        params = signature.parameters
        accepts_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())

        kwargs: dict[str, Any] = {}

        def set_kwarg(name: str, value: Any) -> None:
            if accepts_var_kwargs or name in params:
                kwargs[name] = value

        # onnxocr.ONNXPaddleOcr accepts arbitrary PaddleOCR-style kwargs.
        for key, value in self._resolve_onnxocr_model_kwargs(model_dir).items():
            set_kwarg(key, value)

        set_kwarg("use_angle_cls", True)
        set_kwarg("use_gpu", False)
        set_kwarg("use_onnx", True)
        set_kwarg("show_log", False)
        set_kwarg("cpu_threads", max(1, min(8, os.cpu_count() or 4)))

        # Compatibility with other onnxocr forks/wrappers that expose explicit
        # constructor parameters instead of ONNXPaddleOcr(**kwargs).
        if "model_dir" in params:
            kwargs["model_dir"] = str(model_dir)
        if "det_model" in params and (model_dir / "det.onnx").exists():
            kwargs["det_model"] = str(model_dir / "det.onnx")
        if "rec_model" in params and (model_dir / "rec.onnx").exists():
            kwargs["rec_model"] = str(model_dir / "rec.onnx")
        if "cls_model" in params and (model_dir / "cls.onnx").exists():
            kwargs["cls_model"] = str(model_dir / "cls.onnx")
        if "backend" in params:
            kwargs["backend"] = "openvino" if self.config.use_openvino else "onnxruntime"
        if "provider" in params:
            kwargs["provider"] = "OpenVINOExecutionProvider" if self.config.use_openvino else "CPUExecutionProvider"
        if "providers" in params:
            kwargs["providers"] = ["OpenVINOExecutionProvider"] if self.config.use_openvino else ["CPUExecutionProvider"]
        if "device" in params:
            kwargs["device"] = self.config.device if self.config.use_openvino else "CPU"

        if kwargs:
            return cls(**kwargs)

        # Last-resort constructors used by lightweight wrappers.
        try:
            return cls(str(model_dir))
        except TypeError:
            return cls()

    def recognize(self, image: Image.Image | np.ndarray, offset: Tuple[int, int] = (0, 0)) -> OCRRunInfo:
        start = time.perf_counter()
        if self.engine is None:
            return OCRRunInfo([], 0.0, self.backend_name, self.load_error or "OCR 引擎未加载")

        try:
            np_image = self._to_numpy(image)
            raw = self._call_engine(np_image)
            items = self._normalize_result(raw, offset)
            items = [item for item in items if item.text and item.confidence >= self.config.min_confidence]
            elapsed_ms = (time.perf_counter() - start) * 1000
            return OCRRunInfo(items, elapsed_ms, self.backend_name)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - start) * 1000
            return OCRRunInfo([], elapsed_ms, self.backend_name, f"OCR 推理失败: {exc}")

    def _call_engine(self, np_image: np.ndarray) -> Any:
        for method_name in ("ocr", "recognize", "infer", "predict", "__call__"):
            method = self.engine if method_name == "__call__" else getattr(self.engine, method_name, None)
            if callable(method):
                try:
                    if method_name == "ocr":
                        return method(np_image, cls=True)
                    return method(np_image)
                except TypeError:
                    return method(np_image)
        raise RuntimeError("onnxocr 对象未提供 ocr/recognize/infer/predict 调用接口")

    def _to_numpy(self, image: Image.Image | np.ndarray) -> np.ndarray:
        if isinstance(image, np.ndarray):
            array = image
            if array.ndim == 3 and array.shape[2] > 3:
                array = array[:, :, :3]
            if array.dtype != np.uint8:
                array = array.astype(np.uint8, copy=False)
            return np.ascontiguousarray(array)

        rgb = image.convert("RGB")
        return np.asarray(rgb, dtype=np.uint8)

    def _normalize_result(self, raw: Any, offset: Tuple[int, int]) -> List[OCRItem]:
        rows = self._flatten_rows(raw)
        result: List[OCRItem] = []
        for row in rows:
            parsed = self._parse_row(row, offset)
            if parsed:
                result.append(parsed)
        return result

    def _flatten_rows(self, raw: Any) -> List[Any]:
        if raw is None:
            return []
        if isinstance(raw, dict):
            if "results" in raw:
                return self._flatten_rows(raw["results"])
            if "data" in raw:
                return self._flatten_rows(raw["data"])
            if {"text", "box"}.issubset(raw.keys()):
                return [raw]
            return []
        if isinstance(raw, tuple):
            raw = list(raw)
        if not isinstance(raw, list):
            return []

        # PaddleOCR often returns [[line1, line2, ...]] for one image.
        if len(raw) == 1 and isinstance(raw[0], list) and raw[0] and self._looks_like_row(raw[0][0]):
            return raw[0]
        if raw and self._looks_like_row(raw[0]):
            return raw
        flattened: List[Any] = []
        for item in raw:
            if isinstance(item, list):
                flattened.extend(self._flatten_rows(item))
        return flattened

    def _looks_like_row(self, value: Any) -> bool:
        if isinstance(value, dict) and ("text" in value or "transcription" in value):
            return True
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return True
        return False

    def _parse_row(self, row: Any, offset: Tuple[int, int]) -> Optional[OCRItem]:
        ox, oy = offset
        if isinstance(row, dict):
            text = str(row.get("text") or row.get("transcription") or row.get("label") or "").strip()
            confidence = float(row.get("confidence") or row.get("score") or 1.0)
            box_raw = row.get("box") or row.get("points") or row.get("bbox")
            box = self._box_to_rect(box_raw, ox, oy)
            return OCRItem(text, box, confidence) if text and box else None

        if isinstance(row, (list, tuple)) and len(row) >= 2:
            box_raw = row[0]
            text_part = row[1]
            if isinstance(text_part, (list, tuple)):
                text = str(text_part[0]).strip() if text_part else ""
                confidence = float(text_part[1]) if len(text_part) > 1 else 1.0
            elif isinstance(text_part, dict):
                text = str(text_part.get("text") or text_part.get("transcription") or "").strip()
                confidence = float(text_part.get("confidence") or text_part.get("score") or 1.0)
            else:
                text = str(text_part).strip()
                confidence = float(row[2]) if len(row) > 2 and _is_number(row[2]) else 1.0
            box = self._box_to_rect(box_raw, ox, oy)
            return OCRItem(text, box, confidence) if text and box else None
        return None

    def _box_to_rect(self, box_raw: Any, ox: int, oy: int) -> Optional[Tuple[int, int, int, int]]:
        if box_raw is None:
            return None
        try:
            arr = np.array(box_raw, dtype=float)
            if arr.ndim == 1 and arr.size >= 4:
                values = arr.flatten().tolist()
                x1, y1, x2, y2 = values[:4]
                if x2 < x1 or y2 < y1:
                    xs = values[0::2]
                    ys = values[1::2]
                    x1, x2 = min(xs), max(xs)
                    y1, y2 = min(ys), max(ys)
            else:
                points = arr.reshape(-1, 2)
                x1, y1 = points.min(axis=0)
                x2, y2 = points.max(axis=0)
            return int(x1 + ox), int(y1 + oy), int(x2 + ox), int(y2 + oy)
        except Exception:
            return None


def create_ocr(config: OCRConfig) -> OCRProcessor:
    """Return the shared ONNX-PaddleOCR processor singleton."""
    return OCRProcessor.shared(config)


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False
