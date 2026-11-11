# Tableside

Meeting notes for Zoom, Teams, or any call, on Windows.

Tableside records your PC's speakers and microphone, so no bot joins the call. It works out who said what (`Bob: ...`, `David: ...`) and saves a transcript and an optional AI summary with action items to Word and Excel. Speech recognition runs offline on your PC.

## Download

Go to the **Actions** tab, open the latest **Build Windows installer** run, and download **TablesideSetup** under *Artifacts*. Unzip it and run `TablesideSetup.exe`.

Windows may say "Windows protected your PC" because the app isn't signed with a paid certificate: click **More info → Run anyway**. No admin rights needed.

## Features

- **Auto-prompts to record** when Zoom, Teams, Webex, Slack, Google Meet, etc. start using your mic, and offers to stop when the call ends.
- **Who said what**: recognizes voices you've named before, and picks up names from the conversation ("Thanks, Bob.").
- **AI summary** (overview, decisions, action items) using a built-in offline model, Claude (your own API key), or Ollama.
- **Trim** silence at the start/end, import existing recordings, and stop automatically after 10 minutes of silence.
- **Back-to-back meetings**: transcription runs in a separate low-priority process, so you can start the next recording while the last one is still being processed.
- Notes are saved in `Documents\Tableside\`.

> Tell people you're recording. California requires everyone's consent.

## Build it yourself

Double-click `Build Tableside Installer.bat`. It installs Python and Inno Setup, builds the app, and opens `dist\` with `TablesideSetup.exe`. The first build takes 5–15 minutes.

Every push to GitHub also builds the installer automatically (`.github/workflows/build-windows.yml`).

## Files

| Path | What it is |
|---|---|
| `app/` | the program (`tableside.py` is the main window) |
| `Build Tableside Installer.bat` | builds `TablesideSetup.exe` |
| `tableside.spec` | PyInstaller recipe for `Tableside.exe` |
| `installer.iss` | Inno Setup recipe for the installer |
| `requirements.txt` | Python packages it uses |

See `README.txt` for the full user guide.
