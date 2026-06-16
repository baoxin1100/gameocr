# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

block_cipher = None
project_dir = Path.cwd()
model_dir = project_dir / "models"
assets_dir = project_dir / "assets"
icon_path = assets_dir / "gameocr.ico"
version_path = project_dir / "pyinstaller" / "version_info.txt"

datas = []
binaries = []
if model_dir.exists():
    datas.append((str(model_dir), "models"))
if assets_dir.exists():
    datas.append((str(assets_dir), "assets"))

# Bundle onnxocr's built-in PaddleOCR ONNX models and font files so the
# generated one-file EXE can run OCR on a clean Windows machine without asking
# the user to install Python packages or download model files separately.
datas += collect_data_files("onnxocr", includes=["models/**/*", "fonts/**/*"])

# OpenVINOExecutionProvider depends on native DLLs from both onnxruntime and
# openvino.libs. Collect them explicitly; otherwise the provider can appear in
# get_available_providers() but fail to load at runtime with Error 127.
for module_name in ("onnxruntime", "openvino"):
    try:
        binaries += collect_dynamic_libs(module_name)
    except Exception:
        pass

for dist_name in ("onnxocr", "openvino", "onnxruntime-openvino", "onnxruntime"):
    try:
        datas += copy_metadata(dist_name)
    except Exception:
        pass

hiddenimports = [
    "onnxocr",
    "onnxocr.onnx_paddleocr",
    "onnxocr.predict_system",
    "onnxocr.predict_det",
    "onnxocr.predict_rec",
    "onnxocr.predict_cls",
    "onnxocr.predict_base",
    "onnxocr.db_postprocess",
    "onnxocr.rec_postprocess",
    "onnxocr.cls_postprocess",
    "onnxocr.imaug",
    "onnxocr.operators",
    "onnxocr.utils",
    "onnxocr.logger",
    "openvino",
    "onnxruntime",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "pynput",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "mss",
    "win32con",
    "win32gui",
    "win32process",
    "win32ui",
    "PIL",
    "cv2",
    "numpy",
    "requests",
]

# Keep the one-file package focused on this application. Broadly collecting all
# OpenVINO/ONNXRuntime submodules pulls optional model-conversion, transformer,
# torch, audio/video and scientific stacks that are not used for OCR inference.
excludes = [
    "av",
    "diffusers",
    "IPython",
    "jupyter",
    "librosa",
    "llvmlite",
    "matplotlib",
    "numba",
    "onnx",
    "onnxruntime.quantization",
    "onnxruntime.tools",
    "onnxruntime.transformers",
    "pandas",
    "pytest",
    "scipy",
    "sklearn",
    "tensorflow",
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
    "yt_dlp",
]

a = Analysis(
    ["gameocr/main.py"],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_dir / "pyinstaller" / "rth_gameocr_dlls.py")],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="实时汉化工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # ONNXRuntime/OpenVINO native DLLs can fail during initialization after UPX
    # compression in a PyInstaller one-file build. Keep binaries uncompressed so
    # onnxruntime_pybind11_state.pyd and provider DLLs load reliably.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
    version=str(version_path) if version_path.exists() else None,
)