# Windows guide: Piltdown v3 voiceover, SRT and EDL

For Windows 10 or 11, Python 3.12 and FFmpeg. Every command goes in **PowerShell**. Paste one block at a time.

This makes real files from the real Edge TTS service. It does not render the documentary.

---

## 1. One-time setup (about 10 minutes)

**Install Python 3.12, FFmpeg and Git:**
```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Gyan.FFmpeg
winget install -e --id Git.Git
```
**Close PowerShell and open a new window** so it picks up the new PATH. Then check:
```powershell
py -3.12 --version
ffmpeg -version | Select-Object -First 1
ffprobe -version | Select-Object -First 1
```
You should see `Python 3.12.x` and two `ffmpeg version …` / `ffprobe version …` lines.

**Get the project:**
```powershell
cd $HOME\Documents
git clone https://github.com/infoulugbek/infoulugbek.git
cd infoulugbek
git checkout claude/tender-cray-n77xn8
```

**Create the Python environment:**
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install edge-tts
```
Your prompt now starts with `(.venv)`. Every later step assumes that. In a new window, run `cd $HOME\Documents\infoulugbek` and then `.\.venv\Scripts\Activate.ps1` again.

## 2. Check the installation (offline, about 2 minutes)

```powershell
python -m unittest discover -s tools\vo_pipeline\tests
```
The last line should be `OK` (48 tests). These tests use synthetic tones and silence. They prove the code works on your machine; they are not a voiceover.

## 3. Check that Edge TTS really works (about 1 minute, needs internet)

```powershell
python tools\vo_pipeline\edge_smoke.py --out smoke-test
```
This synthesizes only the first two scenes and checks Edge's timings against the audio. Look at the last lines:

- `"status": "VERIFIED"` or `"VERIFIED_AFTER_SNAP"`: the timings match the audio. Go to step 4. (`AFTER_SNAP` means sentence ends were moved to where the audio shows speech stops. Edge reports them up to about 0.9 s late; its original times are kept in `narration.engine.srt`.)
- `"status": "FORCED_ALIGNMENT_REQUIRED"`: the timings drift. Still go to step 4, and then do step 5.
- `SMOKE FAILED`: see Troubleshooting below.

Delete the test folder afterwards: `Remove-Item -Recurse -Force smoke-test`

## 4. Make the voiceover, SRT and EDL (about 5–10 minutes)

```powershell
python tools\vo_pipeline\vo_pipeline.py all --voice en-US-ChristopherNeural --target-wpm 137
```
When it finishes, the last line names the new folder, for example:
`done: production\piltdown-v3\vo-en-US-ChristopherNeural-20261010-153000`

Open it:
```powershell
explorer (Get-ChildItem production\piltdown-v3 -Directory | Sort-Object Name | Select-Object -Last 1).FullName
```

| File | Use it for |
|---|---|
| `narration.mp3` | Listening, and as the edit's audio track |
| `narration.wav` | The exact master. All timings are measured against this file |
| `narration.srt` | Subtitles |
| `edl.md` | The edit list to read (21 scenes) |
| `edl.csv` | The same edit list for Excel or Google Sheets |
| `edl-vs-outline.md` | Measured scene lengths compared with the production outline |
| `timing.json` | The timing check result |
| `run.log` | What happened, including the measured words per minute |

**Check the timing status** at the top of `edl.md`:
- `Timing status: VERIFIED` or `VERIFIED_AFTER_SNAP`: you can cut picture to these times.
- `Timing status: FORCED_ALIGNMENT_REQUIRED`: **don't** cut picture yet. Do step 5.

## 5. Forced alignment (only if step 4 says FORCED_ALIGNMENT_REQUIRED)

This measures every sentence from the audio itself. The first run downloads a speech model of about 500 MB.
```powershell
python -m pip install faster-whisper
$run = (Get-ChildItem production\piltdown-v3 -Directory | Where-Object Name -notlike '*FAILED' | Sort-Object Name | Select-Object -Last 1).FullName
python tools\vo_pipeline\vo_pipeline.py align --run $run
```
This writes `narration.aligned.srt` and `alignment.json` and rebuilds `edl.md`. Its header should then say `Timing status: ALIGNED`. If it says `PARTIAL_ALIGNMENT`, the sentences that couldn't be measured are listed in `alignment.json` under `unmatched`. Those are left out of the aligned SRT, never guessed.

## 6. Optional changes

- **A different voice:** list the voices with `edge-tts --list-voices | Select-String "en-US"`, then rerun step 4 with `--voice en-US-AndrewNeural` (for example).
- **Faster or slower narration:** `--target-wpm 140` or `--target-wpm 132`.
- **Longer or shorter dramatic pauses:** edit `tools\vo_pipeline\piltdown-v3-pauses.json`, then rebuild the EDL without new audio:
  `python tools\vo_pipeline\vo_pipeline.py edl --run $run`
  (A new pause length only reaches the audio if you rerun step 4.)
- **Re-check the timing of an existing run:** `python tools\vo_pipeline\vo_pipeline.py check --run $run`

Every run makes a new folder; nothing is ever overwritten.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `py` or `python` is not recognized | Reopen PowerShell after installing. Otherwise reinstall Python and tick **"Add python.exe to PATH"**. |
| `ffmpeg not found on PATH` | Reopen PowerShell after `winget install Gyan.FFmpeg`. Check with `where.exe ffmpeg`. |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, answer `Y`, and activate again. |
| `edge-tts is not installed` | The environment isn't active. Run `.\.venv\Scripts\Activate.ps1`, then `python -m pip install edge-tts`. |
| `SkewAdjustmentError: No server date in headers` or `Cannot connect to host speech.platform.bing.com` | Edge TTS can't be reached. Check your internet connection, VPN or firewall. On a corporate network, pass the proxy: `--proxy http://proxy-host:port`. The run folder is renamed `…-FAILED` and contains no audio. |
| `403` / `429` errors from Edge | The service is rate-limiting. Wait a few minutes and run again. Each clip is already retried twice. |
| `RuntimeError: Event loop is closed` printed at exit | Harmless aiohttp noise on Windows; the files are complete. The pipeline already selects the event loop that avoids it. |
| A `…-FAILED` folder appeared | Read its `run.log`; the last line gives the reason. Delete the folder once the problem is fixed. |
| Odd characters in the console | Run `chcp 65001` once, or use Windows Terminal. The output files are always UTF-8. |
| `edl.csv` shows odd characters in Excel | Open it with Data → From Text/CSV and choose UTF-8. The file includes a BOM, so Excel normally detects it. |
| Path errors in a OneDrive folder | Clone the project outside OneDrive, for example `C:\work\infoulugbek`. |
| `faster-whisper` installation fails | Use Python 3.12 (not 3.13), then `python -m pip install --upgrade pip` and try again. |
