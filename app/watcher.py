"""
Tableside meeting watcher.

Windows keeps a record of which apps are using the microphone right now (it's what
powers the microphone icon in the taskbar). When Zoom, Teams, Webex, etc. start
using it, a call has almost certainly started - Tableside uses that to offer to record.
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"
MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

# (text found in Windows' record of the app, friendly name, is_browser)
MEETING_APPS = [
    ("zoom.exe", "Zoom", False),
    ("msteams_", "Teams", False),          # new Teams (Store app)
    ("ms-teams.exe", "Teams", False),
    ("#teams.exe", "Teams", False),        # classic Teams
    ("webex", "Webex", False),
    ("ciscocollabhost", "Webex", False),
    ("atmgr.exe", "Webex", False),
    ("slack", "Slack", False),
    ("skype", "Skype", False),
    ("discord", "Discord", False),
    ("gotomeeting", "GoTo Meeting", False),
    ("g2mcomm", "GoTo Meeting", False),
    ("goto.exe", "GoTo", False),
    ("ringcentral", "RingCentral", False),
    ("bluejeans", "BlueJeans", False),
    ("whatsapp", "WhatsApp", False),
    ("chrome.exe", "your browser", True),
    ("msedge.exe", "your browser", True),
    ("firefox.exe", "your browser", True),
    ("brave.exe", "your browser", True),
    ("opera.exe", "your browser", True),
    ("vivaldi.exe", "your browser", True),
]
OWN_APP = ("tableside.exe", "#python.exe", "#pythonw.exe")


def _apps_using_mic() -> list[str]:
    """Registry names of apps whose microphone use has started but not stopped."""
    if not IS_WINDOWS:
        return []
    import winreg

    found = []

    def scan(path):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    scan(path + "\\NonPackaged")
                    continue
                try:
                    with winreg.OpenKey(key, name) as sub:
                        start = winreg.QueryValueEx(sub, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(sub, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                if start and not stop:
                    found.append(name)

    scan(MIC_KEY)
    return found


def classify(registry_name: str, include_browsers: bool = True) -> str | None:
    low = registry_name.lower()
    if any(own in low for own in OWN_APP):
        return None
    for needle, label, is_browser in MEETING_APPS:
        if needle in low:
            if is_browser and not include_browsers:
                return None
            return label
    return None


def meeting_apps_in_call(include_browsers: bool = True) -> frozenset:
    """Friendly names of meeting apps that are using the microphone right now."""
    try:
        names = _apps_using_mic()
    except Exception:
        return frozenset()
    return frozenset(filter(None, (classify(n, include_browsers) for n in names)))


# --------------------------------------------------------------------------- #
# Start with Windows
# --------------------------------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Tableside"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    from pathlib import Path
    pyw = Path(sys.executable).with_name("pythonw.exe")
    script = Path(__file__).resolve().with_name("tableside.py")
    return f'"{pyw if pyw.exists() else sys.executable}" "{script}" --background'


def set_start_with_windows(on: bool) -> None:
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass


def starts_with_windows() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False
