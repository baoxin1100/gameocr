from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path
from typing import Sequence


def _run_ocr_smoke(output_path: str) -> int:
    """Packaged-EXE OCR/OpenVINO smoke test.

    The release executable is built as a windowed app, so stdout/stderr are not a
    reliable verification channel. This hidden maintenance switch writes a JSON
    report that can be checked after PyInstaller packaging without opening the
    GUI.
    """

    report: dict[str, object] = {"ok": False}
    try:
        import importlib.metadata as metadata

        import numpy as np
        from PIL import Image, ImageDraw

        from gameocr.config import OCRConfig
        from gameocr.ocr import OCRProcessor

        image = Image.new("RGB", (320, 120), "white")
        draw = ImageDraw.Draw(image)
        draw.text((20, 40), "HELLO GAME OCR", fill="black")

        # Instantiate OCRProcessor before importing onnxruntime here. In a
        # PyInstaller one-file EXE, OCRProcessor adds extracted native DLL
        # directories (onnxruntime/capi, openvino/libs) before ONNXRuntime is
        # imported. Importing onnxruntime first can fail even though the normal
        # GUI path would be healthy.
        processor = OCRProcessor(OCRConfig())
        result = processor.recognize(image)

        import onnxruntime as ort

        report.update(
            {
                "ok": result.error is None and bool(result.items),
                "backend": result.backend,
                "error": result.error,
                "items": [
                    {"text": item.text, "box": item.box, "confidence": item.confidence}
                    for item in result.items
                ],
                "openvino_active": getattr(processor, "_onnxruntime_openvino_active", None),
                "openvino_disabled": getattr(processor, "_onnxruntime_openvino_disabled", None),
                "providers": ort.get_available_providers(),
                "numpy": np.__version__,
                "openvino": metadata.version("openvino"),
                "onnxruntime-openvino": metadata.version("onnxruntime-openvino"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        report.update({"ok": False, "error": str(exc), "traceback": traceback.format_exc(limit=5)})

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--ocr-smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-output", default="build/ocr-smoke.json", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.ocr_smoke:
        return _run_ocr_smoke(args.smoke_output)

    # Use an absolute package import so this file works both with
    # `python -m gameocr.main` and when PyInstaller executes it as a frozen script.
    from gameocr.gui import run_app

    return int(run_app())


if __name__ == "__main__":
    raise SystemExit(main())
