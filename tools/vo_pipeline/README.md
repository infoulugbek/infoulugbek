# Voiceover → SRT → EDL pipeline (Piltdown v3)

> **On Windows, start with [WINDOWS-GUIDE.md](WINDOWS-GUIDE.md):** copy-paste commands and troubleshooting.

## Video Factory integration layer

These modules fix the problems found in the yt-edit integration. They do so **without modifying the skill**: `.claude/skills/yt-edit/` is untouched, and `deadair.py` still runs for the audit trail (`deadair.json`).

| Module | What it does |
|---|---|
| `factory_edit.py` | Makes the cut decisions. **Protected pauses** can never be cut: overlaps are subtracted and checked again before writing. **Retakes** keep digits and are rejected when two lines differ in a date, number, month or name, or are more than 15 s apart. **Duration** comes from the real audio (WAV sample count), including opening silence and the tail. |
| `timing_check.py` | Checks Edge TTS sentence timings against the generated audio with ffmpeg `silencedetect`: speech onset and offset per clip, sentence coverage, silence inside planned pauses, ordering. The result is `VERIFIED` or `FORCED_ALIGNMENT_REQUIRED`. It also maps faster-whisper word timestamps onto the script sentences for forced alignment. |
| `edge_smoke.py` | A one-minute REAL Edge TTS check of the timing chain (scenes S01–S02). |
| `tests/` | 40 offline tests (`python -m unittest discover -s tools/vo_pipeline/tests`), also run on Windows and Linux by `.github/workflows/vo-pipeline.yml`. |

New files in each run folder: `protect.json` (planned silences), `timing.json` (the timing check), `factory_edit.json` (cut decisions), and, after `align`, `whisper-words.json`, `alignment.json` and `narration.aligned.srt`. The EDL header always states its **timing status**. It warns not to cut picture when timings are unverified.

New command: `vo_pipeline.py check --run <folder>` re-verifies the timings of an existing run and rebuilds the EDL.


`vo_pipeline.py` turns `scripts/piltdown-man-documentary-v3.md` into:

| File | What it is |
|---|---|
| `narration.wav` / `narration.mp3` | The full voiceover, with the planned pauses written in as real silence |
| `narration.srt` | Sentence-level subtitles. Times come from Edge TTS's own sentence-boundary events, offset by the exact sample count of everything before them |
| `timeline.json` | Every clip and silence, with start and end times (the machine-readable source of truth) |
| `deadair.json` | Raw output of the `/yt-edit` tool (`.claude/skills/yt-edit/deadair.py`) on the SRT |
| `edl.md` / `edl.csv` | Edit decision list for all 21 scenes: in and out points, narration, visual changes, sound cues, PRESERVE pauses, optional trims and suggested cuts |
| `edl-vs-outline.md` | Measured scene durations compared with the estimates in `scripts/piltdown-man-v3-production-outline.md` |
| `run.log` | Voice, rate, calibration passes and the measured words per minute |

Each run writes to a **new** folder, `production/piltdown-v3/vo-<voice>-<timestamp>/`. Nothing existing is overwritten, and the scripts (V1–V3) are only read. If synthesis fails, the folder is renamed `…-FAILED`, keeps only `run.log`, and contains no audio or timings.

Planned pauses (the scripted beat, the title-card sting, the gaps around reveals) live in `piltdown-v3-pauses.json`. Edit that file to change the pacing, never the script.

## Why this wasn't run in the cloud session

The cloud environment's network policy blocks `speech.platform.bing.com`, the Edge TTS endpoint. The proxy answers the connection with HTTP 403, which `edge-tts` reports as `SkewAdjustmentError: No server date in headers`. The optional Whisper cross-check is blocked the same way: `huggingface.co` and `openaipublic.azureedge.net` are unreachable. Every other step is tested offline by `tests/` (40 tests); the real-service timing check is `edge_smoke.py`.

## Windows: step by step (PowerShell)

1. **Install the prerequisites** (once):
   ```powershell
   winget install Python.Python.3.12
   winget install Gyan.FFmpeg
   ```
   Close and reopen PowerShell, then check:
   ```powershell
   python --version   # 3.10 or newer
   ffmpeg -version
   ffprobe -version
   ```

2. **Get the repository** on branch `claude/tender-cray-n77xn8`:
   ```powershell
   git clone https://github.com/infoulugbek/infoulugbek.git
   cd infoulugbek
   git checkout claude/tender-cray-n77xn8
   ```

3. **Create a virtual environment and install edge-tts:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   # If activation is blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   pip install edge-tts
   ```

4. **Check the offline logic first** (synthetic tones; takes about 2 minutes):
   ```powershell
   python -m unittest discover -s tools\vo_pipeline\tests
   ```
   It should end with `OK`. Nothing it generates is a voiceover.

5. **Preview the voice** (optional). Listen to a few candidates and pick one:
   ```powershell
   edge-tts --list-voices | Select-String "en-US"
   edge-tts --voice en-US-ChristopherNeural --rate=-8% --text "You're looking at a fake that lasted 41 years." --write-media preview-christopher.mp3
   edge-tts --voice en-US-AndrewNeural --rate=-8% --text "You're looking at a fake that lasted 41 years." --write-media preview-andrew.mp3
   ```
   Good documentary candidates are `en-US-ChristopherNeural` (the default: deep and measured), `en-US-AndrewNeural` (warm) and `en-US-GuyNeural`.

6. **Run the full pipeline:**
   ```powershell
   python tools\vo_pipeline\vo_pipeline.py all --voice en-US-ChristopherNeural --target-wpm 137
   ```
   - Calibration synthesizes the longest paragraph (S18.3, 64 words), measures the real speaking rate from Edge's sentence boundaries, and adjusts `--rate` until it is within ±1.5 wpm of the target (at most 4 passes). Each pass is logged.
   - Then all 59 paragraphs are synthesized, the pauses inserted, and everything concatenated.
   - The log ends with the speaking rate (pauses excluded) and the overall rate (pauses included).
   - To skip calibration: `--rate=-10%`.
   - If you're behind a corporate proxy: `--proxy http://host:port`.

7. **Optional: cross-check the timings with Whisper.** This is an independent recognizer listening to the actual audio:
   ```powershell
   pip install faster-whisper
   python tools\vo_pipeline\vo_pipeline.py align --run production\piltdown-v3\<run-folder>
   ```
   It writes `whisper.json` and logs the median and 90th-percentile drift between Whisper's sentence starts and the TTS timings. A median drift under 0.3 s means the SRT is trustworthy. You can also run `/yt-edit` on the Whisper output:
   `python .claude\skills\yt-edit\deadair.py production\piltdown-v3\<run>\whisper.json`

8. **Rebuild the EDL** after editing the pause file or the outline (no new audio is made):
   ```powershell
   python tools\vo_pipeline\vo_pipeline.py edl --run production\piltdown-v3\<run-folder>
   ```

9. **Commit only the deliverables.** `production/.gitignore` keeps the per-clip files and the large WAV out of git:
   ```powershell
   git add production\piltdown-v3\<run-folder>
   git commit -m "Add Piltdown v3 voiceover, SRT and EDL"
   ```

## Reading the EDL

- **PRESERVE** is a planned dramatic pause: the S10 two-second beat, the title-card sting after S02, the silence after "What if someone had planted the bones?", and so on. `deadair.py` flags every one of these as dead air. The pipeline labels them so nobody cuts them.
- **OPTIONAL TRIM** is a planned paragraph breath (0.45 s) plus the TTS engine's own padding. Trim these only if the pacing drags.
- **SUGGESTED CUT** is a gap inside a synthesized line that nobody planned. With TTS there should be few or none.
- **VISUAL** cues fire at the start of the line they introduce. A scene's picture holds through its closing pause and cuts about 0.4 s before the next scene's first word.

## Files

- `vo_pipeline.py`: the pipeline (`extract`, `all`, `edl`, `align`).
- `piltdown-v3-pauses.json`: the pause plan.
- `test_pipeline_stub.py`: an offline end-to-end run with synthetic tone audio (used by `tests/`). **Its output is not a voiceover.**
- `YT-EDIT-ASSESSMENT.md`: how `/yt-edit` fits an automated Video Factory.
