# Tableside: handoff for Claude Code

Tableside is a Windows desktop app that takes meeting notes for Zoom, Teams, or any call, much like Otter. It records the PC's speaker output and microphone (no bot joins the call), transcribes offline, works out who said what, and writes the transcript plus an optional AI summary to Word and Excel. The owner (David) builds it and hands the installer to coworkers, so they need nothing installed.

- Repo: https://github.com/tycket1/Tableside (single commit `0052379`, 2026-11-04, "Initial commit")
- Built in a Claude chat session on 2026-10-21; this file hands it over to Claude Code.
- `README.txt` is the end-user guide; `README.md` is the GitHub landing page. Keep both in step with behavior changes.

## Owner preferences that apply here

- Excel output: Times New Roman 11 everywhere; no cell shading; headers in bold black font; thick outside border with dotted inside borders; all gridlines showing; negative numbers red in parentheses. `export_xlsx` follows all of these (helpers `style()` and `box()` inside it).
- Spell out "footnote"; never abbreviate it as "fn".
- File names: use spaces, never underscores, for anything a user sees.
- Write user-facing text plainly and keep it short. Users are engineers and office staff, not developers.

## Layout

| Path | What it is |
|---|---|
| `app/tableside.py` | Tk GUI: main window, dialogs, pop-ups, tray, job worker, single-instance handling, `--selftest` (about 2,550 lines) |
| `app/engine.py` | Everything that isn't GUI: transcription, speaker detection, voice memory, name guessing, trimming, summaries, Word/Excel export (about 1,600 lines) |
| `app/recorder.py` | WASAPI loopback and mic capture, saved as 16 kHz mono WAV |
| `app/watcher.py` | Detects a call starting (an app is using the mic); "Start with Windows" registry key |
| `app/first_names.txt` | Common US first names (SSA data, 1940+), used by name guessing |
| `app/tableside.ico`, `tableside.png`, `icon_*.png` | App icon: coral tile with two speech bubbles |
| `tableside.spec` | PyInstaller recipe (one-folder build, `dist/Tableside/Tableside.exe`) |
| `installer.iss` | Inno Setup script producing `dist/TablesideSetup.exe`, a per-user install with no admin rights |
| `Build Tableside Installer.bat` | One-click local build: installs Python 3.12 and Inno Setup through winget if missing |
| `.github/workflows/build-windows.yml` | CI: builds, runs `Tableside.exe --selftest`, builds the installer, uploads the `TablesideSetup` artifact |
| `version_info.txt` | Windows file-version resource (1.0.0) |
| `CLAUDE.md` | This handoff file |

## Running and building

```bat
:: from source on Windows (Python 3.12 has tkinter)
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app\tableside.py              :: normal launch
.venv\Scripts\python app\tableside.py --background :: start hidden in the tray (what Windows startup uses)
.venv\Scripts\python app\tableside.py --selftest   :: checks bundled imports; writes %LOCALAPPDATA%\Tableside\selftest.txt (PASS/FAIL)

:: installer
"Build Tableside Installer.bat"                    :: produces dist\TablesideSetup.exe
```

Every push also builds the installer on GitHub Actions (Actions tab, then the latest run, then Artifacts).

## Where data lives

- `Documents\Tableside\<YYYY-MM-DD HHMMSS Title>\` holds one folder per meeting:
  - `meeting.json` is the source of truth (see the schema below).
  - `meeting audio.wav` is the speaker output (other people). `my mic.wav` is the user's mic. Meetings recorded before 2026-11-11 use `meeting_audio.wav` and `my_mic.wav`; always use the names listed in `tracks`.
  - `<Title YYYY-MM-DD>.docx` and `.xlsx` are regenerated from `meeting.json` by `E.export_files()`.
  - `.original_audio\` is a hidden backup made by trimming, used for "Undo trim".
- `%LOCALAPPDATA%\Tableside\`:
  - `settings.json` (keys: `DEFAULT_SETTINGS` in `tableside.py`; `settings_version` = 2)
  - `voices.json` holds remembered voices (name to averaged 256-d voice fingerprint)
  - `models\whisper\<name>\`, `models\pyannote-segmentation-3-0.onnx`, `models\wespeaker_en_voxceleb_resnet34_LM.onnx`, `models\summary-model\`
  - `tableside.log` (stdout/stderr of the frozen app), `instance.port`, `selftest.txt`
- The Claude API key is stored in Windows Credential Manager through `keyring`, service `Tableside`, falling back to `settings.json`.

### `meeting.json`

```jsonc
{
  "title": "...", "started": "ISO datetime", "duration": 123.4,      // seconds
  "status": "recording|queued|processing|summarizing|ready|error|interrupted",
  "options": {"audio": true, "transcript": true, "summary": false},   // the 3 main-screen checkboxes
  "tracks": [{"file": "meeting audio.wav", "kind": "meeting"}, {"file": "my mic.wav", "kind": "me"}],
  "imported": true,                       // set when made by "Import recording" (track file is an absolute path; never trimmed or deleted)
  "segments": [[start, end, "S1"|"me", "text"], ...],   // speaker *keys*, not names
  "names": {"me": "David", "S1": "Bob"},                // key -> display name
  "fingerprints": {"S1": [256 floats]},                 // per-speaker voice fingerprint for this meeting
  "auto_named": ["S1"],                   // named by voice match
  "guessed": {"S2": {"name": "Alan", "score": 2.5, "from": "S1", "quote": "Alan, how's ...?"}},
  "names_confirmed": false,
  "summary": {"summary": "...", "decisions": [], "action_items": [{"owner","task","due"}], "open_questions": [],
              "basic": true, "note": "..."},           // basic/note only for the no-AI fallback
  "summary_pending": true,                // waiting for the user to name speakers first
  "untrimmed": {...}, "trimmed": {...}, "auto_trimmed": {"start": s, "end": s}, "auto_trim_seen": true,
  "docx": "file name", "xlsx": "file name", "error": "...", "audio_deleted": true
}
```

Use `E.palette_name(key, names)` to show a key; unnamed keys display as "Speaker N" and "Me". Always read and modify meetings through `E.read_meeting()` and `E.write_meeting()`, which writes atomically; `folder` is added on read and stripped on write.

## How a meeting is processed (`App._process` in tableside.py)

1. **Auto-trim** (live recordings only, setting `auto_trim`): `E.auto_trim_bounds()` cuts more than 60 s of silence at either end before transcribing. Undoing an automatic trim triggers a full re-transcribe.
2. **Whisper**: `E.load_whisper()` uses faster-whisper on CPU with int8; the default model is `small.en`.
3. For each meeting track:
   1. `E.load_audio()`
   2. `SpeakerEngine.diarize()`: sherpa-onnx with pyannote segmentation 3.0 and WeSpeaker ResNet34 embeddings, fast clustering at `CLUSTER_THRESHOLD`.
   3. `E.tidy_turns()` merges over-split speakers by fingerprint similarity (`SAME_VOICE`) and folds blips under `TINY_TALK` into the closest voice.
   4. Transcribe with word timestamps.
   5. `E.assign_speakers()` splits Whisper lines at speaker changes, word by word.
4. **Mic track**: transcribed separately as speaker `me`. `E.remove_echo()` drops mic lines that duplicate the speaker track, which happens when the user isn't wearing headphones.
5. **Naming**, in priority order:
   1. `VoiceBook.match()` matches saved voices (cosine ≥ `VOICE_MATCH` and beating the next-closest voice by `VOICE_MARGIN`).
   2. `E.guess_names()` uses forms of address: "Alan, …?" names the next speaker, "Thanks, Bob." names the previous one, "this is Priya" names the speaker. A candidate counts only if it's a known name, a common first name, or capitalized mid-sentence.
   3. `E.ai_guess_names()` runs only for the Claude or Ollama providers. It's never used with the built-in model, which is too slow. The name must literally appear in the transcript.
6. **Write and export**: write `meeting.json`, then `E.export_files()`. If the Audio box was unticked, delete the WAVs.
7. **Summary** (if the AI summary box was ticked): `E.summarize()`. For a live recording with unnamed voices, the summary waits (`summary_pending`) until the user names or skips them.

### Summary providers (`settings.ai_provider`)

- **`local`** (the default): onnxruntime-genai runs an ONNX Phi model.
  - `E.local_model_choice()` lists `microsoft/Phi-3.5-mini-instruct-onnx` and `microsoft/Phi-4-mini-instruct-onnx` through `HfApi.model_info(files_metadata=True)` and picks the **smallest** `cpu` + `int4` folder by real byte count.
  - The download goes to `models/summary-download` (marked with `choice.txt`) and is then moved to `models/summary-model`.
  - Long transcripts are summarized in chunks of about 3,500 tokens and then combined.
  - If the model can't run, it falls back to `E.basic_summary()`, which is rule-based and makes no AI call.
- **`claude`**: plain `urllib` POST to `https://api.anthropic.com/v1/messages` (`anthropic-version: 2023-06-01`). Models are listed in `E.CLAUDE_MODELS` (Sonnet 5.5 default, Haiku 5.5, Opus 5.5).
- **`ollama`**: `http://localhost:11434/api/chat`.

## Threads and the main event loop (important)

- Tk is single-threaded. **Background threads must never touch widgets.** Hand UI work back with `App.post(kind, ...)`; `App._poll()` drains the queue every 150 ms. Use `self.app.post("call", fn)` to run any function on the Tk thread. A dialog stuck on "Reading the recording…" turned out to be a background thread calling `widget.after()`; replacing those calls with `post("call", fn)` fixed it.
- The job worker thread consumes `self.jobs`, which holds `("process", folder, live)` and `("summary", folder, False)` jobs.
- The watcher thread polls `watcher.meeting_apps_in_call()` every 2 s and posts `("mics", frozenset)`, which drives `MeetingToast` (call started) and `CallEndedToast` (call ended or 10 minutes of silence).
- The tray icon runs through `pystray` (`run_detached`). Tray menu actions post `("tray", action)`.
- Single instance: a localhost TCP listener; its port is in `instance.port`. A second launch sends `show` (or `ping` for `--background`) and exits.
- `main()` ends with `os._exit(0)` so that tray and watcher threads can't keep a hidden `Tableside.exe` alive.

## Gotchas already hit (don't undo these)

- **`av` must stay below 19.** PyAV 19 breaks `faster_whisper.audio.decode_audio`. It's pinned in `requirements.txt`.
- **Import `onnxruntime` before `sherpa_onnx`** (`SpeakerEngine.__init__`) so the two share one runtime instead of clashing.
- **The Whisper model is downloaded into Tableside's own folder** with `faster_whisper.utils.download_model(name, output_dir=...)`, not the Hugging Face cache. On Windows the cache's symlink fallback left a broken `model.bin` ("Unable to open file 'model.bin'"). `load_whisper` checks the files are there and re-downloads once if loading fails.
- **The sv-ttk theme repaints classic Tk widgets** after it loads, and it only notifies the root window. Coloured classic widgets must be registered: `PillButton.instances` and `App.dots` are repainted in `App._recolor()`.
- **Avoid emoji above U+FFFF in Tk text**; Windows Tk can't draw them. Stick to symbols like ▶ ✂ ⚙ ● ■ ☐.
- **The recorder pads gaps with silence by wall clock.** WASAPI loopback delivers no callbacks while nothing is playing, so without padding the meeting and mic tracks drift apart. `StreamRecorder` also records `last_loud` (RMS > 0.01), which the silence auto-stop uses.
- **Audio is saved as 16 kHz mono**, downsampled while streaming with `av.AudioResampler`. Raw 48 kHz stereo was about 11.5 MB/min, which the user flagged as too large. "Shrink saved recordings" in Settings converts older files.
- **The installer force-closes a running Tableside** (`PrepareToInstall` runs `taskkill`, plus `CloseApplications=force`). Before that, the tray copy blocked installs with "Setup was unable to automatically close all applications".
- **Taskbar pinning depends on the AppUserModelID** being `Tableside.MeetingNotes` in both `SetCurrentProcessExplicitAppUserModelID` and the installer's shortcuts. Keep them identical.
- **The installer decides "Start with Windows"** (task `startup`, value `HKCU\...\Run\Tableside` set to `"...Tableside.exe" --background`). The app must not re-add it on its own. Settings reads the real registry state.
- **Settings migration:** at `settings_version` < 2, `ai_provider` changes from `claude` to `local` when no key is stored.

## What's been verified

Verified **in a Linux sandbox** (Xvfb, with Whisper and audio devices stubbed where needed):

- Diarization on a synthetic three-voice conversation, including recovering a deliberately seven-way over-split down to three people.
- Saved-voice matching in a second meeting (about 0.9 similarity for the same person, under 0.6 for others).
- Unit cases for name guessing.
- Word/Excel export.
- The trim editor and automatic trim with undo.
- The meeting pop-up, end-of-call countdown and silence stop.
- Shrinking old recordings.
- A PyInstaller build passing `--selftest`.

Seen working **on the owner's Windows PC**: the installer, recording, transcription, speaker separation, and mic labelling as the user's name.

**Never verified on real Windows** (most of these were written after the owner's last report):

- Mic-in-use detection from the registry (`CapabilityAccessManager\ConsentStore\microphone`; a key with `LastUsedTimeStop == 0` means in use), and therefore the "meeting started" pop-up.
- The tray icon, the silence stop, and `winsound` playback in the trim and naming dialogs.
- The 16 kHz recorder on real devices.
- The fixes for over-split speakers (owner saw 10 speakers for 4 people).
- Built-in AI model selection, size, speed and summary quality. The owner saw more than 7.4 GB downloading for Phi-4-mini before smallest-model selection was added; real Hugging Face sizes were never checked because Hugging Face was blocked in the sandbox.

There is **no test suite in the repo**. All tests were throwaway scripts in the chat session.

## Open items / suggested next steps

1. **Smoke-test the current build on Windows.** Install over the old version, record a short call in Zoom or Teams, and confirm the pop-up, transcript, speaker count, Trim and the 16 kHz files. Check `%LOCALAPPDATA%\Tableside\tableside.log` for errors.
2. ~~Make Excel export follow the owner's style~~ Done 2026-11-11.
3. **Built-in AI:** confirm which model `local_model_choice()` picks and its real size, and measure summary time on the owner's laptop. If it's still several GB or slow, consider a smaller ONNX GenAI model (for example a 1.5B–3B instruct model) and keep the smallest-by-bytes rule.
4. **Tune speaker detection on real meetings** (`CLUSTER_THRESHOLD`, `SAME_VOICE`, `TINY_TALK`). The thresholds were tuned on synthetic voices only. The "People in this meeting → Apply" control in the naming dialog (`E.merge_meeting_speakers`) is the manual fallback.
5. ~~File names without underscores~~ Done 2026-11-11: new recordings are `meeting audio.wav` and `my mic.wav`. Old meetings keep their names through `tracks`.
6. **Add a real test suite** (`tests/`, pytest): engine functions run headless. For GUI tests, stub `pyaudiowpatch` and `E.load_whisper`, and on Linux run under `xvfb-run`.
7. **Split `tableside.py`** into dialogs, toasts and the main window if it keeps growing.
8. **Code signing**: installs show "Windows protected your PC" (More info → Run anyway) until the installer is signed with a certificate.

## Conventions

- Keep `engine.py` free of GUI imports so it can be tested headless.
- User-facing wording should be plain English with no jargon ("Telling the voices apart…", not "Diarizing").
- After changing behavior, update `README.txt` (user guide) and `README.md`.
- Bump `AppVersion` in `installer.iss` and `version_info.txt` together for releases.
