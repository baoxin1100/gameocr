from __future__ import annotations

import json
import pathlib
import sys
import traceback


def main() -> int:
    report: dict[str, object] = {
        "ok": False,
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": str(getattr(sys, "_MEIPASS", "")),
    }
    try:
        import importlib.metadata as metadata

        import onnxruntime as ort

        report.update(
            {
                "ok": True,
                "onnxruntime_file": getattr(ort, "__file__", ""),
                "providers": ort.get_available_providers(),
                "onnxruntime-openvino": metadata.version("onnxruntime-openvino"),
                "openvino": metadata.version("openvino"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        report.update({"ok": False, "error": str(exc), "traceback": traceback.format_exc()})

    target = pathlib.Path("build/ort-smoke.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())