TABLESIDE - meeting notes for Zoom, Teams, or any call
=======================================================

Tableside records your PC's speakers and microphone, so no bot joins the call.
It works out who said what (Bob: ..., David: ...) and saves a transcript and an
optional AI summary with action items to Word and Excel.


MAKE THE INSTALLER (you, once)
------------------------------
1. Unzip this folder.
2. Double-click  "Build Tableside Installer.bat".
   It installs what it needs (Python, Inno Setup) and builds the app.
   The first build takes 5-15 minutes.
3. When it finishes, the "dist" folder opens with  TablesideSetup.exe  in it.
   That one file is the app. Share it with anyone.

Optional: put this folder on GitHub and the included workflow builds
TablesideSetup.exe automatically on every change (Actions tab > Artifacts).


INSTALLING (anyone)
-------------------
1. Double-click TablesideSetup.exe.
   Windows may say "Windows protected your PC" because the app isn't signed
   with a paid certificate: click "More info" > "Run anyway".
2. No admin rights needed. Tableside appears in the Start menu.
3. Open it, then right-click its taskbar icon > "Pin to taskbar".
   Uninstall from Settings > Apps like any other program.


IT REMEMBERS TO RECORD FOR YOU
------------------------------
Tableside starts with Windows and sits in the system tray (bottom-right, by the
clock). When Zoom, Teams, Webex, Slack, Google Meet, etc. start using your
microphone, a pop-up asks "Zoom meeting started - record it?" with the
Audio / Transcript / AI summary boxes. Click Record, or Not now.
When the call ends it offers to stop, and stops on its own after 30 seconds.
Closing the window keeps it in the tray. Quit from the tray icon.
Turn any of this off in Settings > Meetings.


WHO SAID WHAT
-------------
Tableside tells voices apart, then names them three ways:
1. Voices you've named before are recognised automatically.
2. It listens for people calling each other by name:
   "Alan, how's the grading plan?"  -> the next person to talk is Alan
   "Thanks, Bob."                   -> the person who just spoke is Bob
   "Hi, this is Priya."             -> that speaker is Priya
3. If AI summary uses Claude or Ollama, the AI double-checks anyone still unnamed.
Guessed names show a "Check names" button with the line that gave it away.
Saving names teaches Tableside those voices for next time.


USING IT
--------
- Type a meeting name, tick what you want, press Start recording:
    [x] Audio recording   keep the audio file
    [x] Transcript        who said what, with timestamps
    [ ] AI summary        overview, decisions, action items
- Press Stop when the call ends. Tableside transcribes in the background.
- If it can't work out a voice, it asks you to name it (press the play
  button to hear each one).
- Notes are saved in  Documents\Tableside\  (Word + Excel, Times New Roman 11).
- Audio is saved as 16 kHz mono WAV - about 2 MB per minute for the meeting
  audio plus the same for your mic. Settings > General > "Shrink saved
  recordings" converts recordings made by older versions.
- "Import recording..." transcribes an existing audio/video file, such as a
  Zoom or Teams cloud recording.


FORGOT TO PRESS STOP?
---------------------
- If nobody makes a sound for 10 minutes, Tableside offers to stop and stops
  itself 30 seconds later (press "Keep recording" to continue).
- More than a minute of silence at the start or end is cut automatically
  before transcribing. The banner says what was cut - press Undo to get it back.
- To cut it yourself, open the meeting and press "Trim": drag the two handles
  on the audio wave, use "Play start"/"Play end" to check, or press
  "Cut silence automatically". The transcript is adjusted to match.
  More > "Undo trim" restores the full recording.

First use downloads the speech model (about 500 MB) and the voice models
(about 35 MB), plus a few GB for the built-in AI summary if you use it
(Settings > AI summary shows the exact size).
After that everything runs offline on your PC.


AI SUMMARY
----------
Three choices in Settings > AI summary:
- Built-in AI (default): free, no key, no account. Runs a small Microsoft Phi
  model on your PC. One-time download of a few GB - Tableside picks the
  smallest build and shows its size in Settings (press "Download now" or it
  downloads the first time you summarize). A one-hour meeting takes a few
  minutes on a typical laptop. Needs about 6 GB of free memory.
  If it can't run, Tableside writes a basic summary (action items, topics,
  open questions) without AI instead.
- Claude: best quality, a few cents per meeting, needs your own API key.
  Only the transcript text is sent, never the audio.
- Ollama: free, if you already use Ollama.


GOOD TO KNOW
------------
- Wear headphones for the cleanest transcript.
- Tell people you're recording. California requires everyone's consent.
- If something goes wrong, the log is at  %LOCALAPPDATA%\Tableside\tableside.log


WHAT'S IN THIS FOLDER
---------------------
app\                          the program (tableside.py is the main window)
Build Tableside Installer.bat builds TablesideSetup.exe
tableside.spec                packaging recipe for Tableside.exe
installer.iss                 recipe for the installer
requirements.txt              components it uses
.github\workflows\            optional automatic GitHub build
