# PyInstaller recipe for Tableside.exe  (build with:  pyinstaller tableside.spec)
import sys
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("faster_whisper", "ctranslate2", "sherpa_onnx", "onnxruntime", "sv_ttk",
            "docx", "openpyxl", "av", "tokenizers", "send2trash", "keyring", "darkdetect", "pystray", "onnxruntime_genai", "huggingface_hub"):
    try:
        d, b, h = collect_all(pkg)
    except Exception as e:  # e.g. a Windows-only package when test-building elsewhere
        print(f"skip {pkg}: {e}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

for f in ("first_names.txt", "tableside.ico", "tableside.png", "icon_24.png", "icon_32.png", "icon_64.png", "icon_128.png"):
    datas.append((f"app/{f}", "."))

hiddenimports += ["pyaudiowpatch", "winsound", "pystray._win32", "watcher", "keyring.backends.Windows", "engine", "recorder"]

a = Analysis(
    ["app/tableside.py"],
    pathex=["app"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["cryptography", "secretstorage", "jeepney", "torch", "torchaudio", "tensorflow", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Tableside",
    icon="app/tableside.ico",
    version="version_info.txt" if sys.platform == "win32" else None,
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Tableside", upx=False)
