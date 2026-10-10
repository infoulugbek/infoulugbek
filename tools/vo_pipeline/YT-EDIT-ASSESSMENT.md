# `/yt-edit` (`deadair.py`): fit for an automated Video Factory

Assessed 2026-10-10 by reading the source and running it on: (a) the full Piltdown v3 dry run (59 paragraphs, 17 planned pauses, silent stand-in audio from `test_pipeline_stub.py`), and (b) small hand-made SRT files probing edge cases. The numbers below come from those runs.

## Verdict

**Useful as a QC sensor; not usable unsupervised as an editor.** It's a small, dependency-free parser with machine-readable output, which makes it easy to integrate. But it has no idea which silences are intentional, so it can't be allowed to apply cuts on its own. For TTS narration its main finding is noise (the planned pauses); its real value is for human-recorded takes (retakes, fillers, long hesitations).

| Criterion | Rating | Evidence |
|---|---|---|
| Ease of integration | Good | Python standard library only. Reads SRT, VTT and Whisper JSON. `--json` gives `{source, duration, cuts[], removed, out}`. Single file, about 80 lines. |
| Accuracy on human takes | Plausible, untested here | Retake detection compares against the last *speech* cue, skipping fillers. Cuts trim dead air from the middle of a gap, leaving a breath on each side. No real human take was available to test. |
| Accuracy on scripted TTS | Poor without a wrapper | Dry run: 58 DEAD items, 51.03 s (8.0%) proposed for removal. **Every one** was a planned silence: 23 dramatic pauses and 35 paragraph breaths. With no protect list, it would have cut the scripted two-second beat in S10 and the title-card sting. |
| Runtime projection | Inaccurate | It treats the end of the last cue as the file's duration. Dry run: deadair reported 634.70 s; the real file was 638.10 s. The 1 s lead-in and 3 s tail are invisible to it. |
| Retake detection | False positives on dated parallel lines | It strips digits before comparing the first 5 words. "In 1913, the jaw was examined in London." followed by "In 1915, the jaw was examined in Washington." was flagged as a REPEAT and proposed for a 2 s cut. Documentary scripts use this structure constantly. |
| Filler detection | English-only, whole-cue only | A cue that is only "Right." is cut as FILLER. That's fine for ad-libs, but wrong for a scripted one-word line. Fillers inside longer cues are never found. |
| Granularity | Limited by the transcript | With sentence-level cues (Edge TTS SentenceBoundary, Whisper segments) it can't see pauses inside a sentence. Feed it word-level timestamps for finer edits. |
| Output for editors | Missing | Seconds only. No frame rate, no CMX3600 EDL, FCPXML or OpenTimelineIO export. It never touches media, by design. |
| Schema stability | Weak | The JSON has no version field and the cut `kind` values are free strings. Pin the file's version in the factory. |

## What the Piltdown pipeline already adds around it

`tools/vo_pipeline/vo_pipeline.py` wraps `deadair.py` without modifying it:

1. A **protect list** (`piltdown-v3-pauses.json`): DEAD items that overlap a planned dramatic pause become **PRESERVE**, and planned paragraph breaths become **OPTIONAL TRIM**. Only unplanned gaps stay as **SUGGESTED CUT**.
2. **True duration** from the assembled WAV's sample count, not from the last cue.
3. **Scene mapping**: every finding is attached to S01–S21 alongside the visual and sound cues, as `edl.csv` and `edl.md`.
4. **Raw output kept** (`deadair.json`), so the factory can audit what the tool itself said.

## Recommended changes before using it in the Video Factory

In priority order. Each is small; most are a few lines in a wrapper or a fork.

1. **Protected ranges as input** (`--protect ranges.json`). This is a must for any scripted or TTS content.
2. **Keep digits in the REPEAT comparison**, or require the cue to be shorter than the cue it repeats, plus a time window (retakes happen within about 10 s). This fixes the dated-line false positive.
3. **Duration from the media** (`--media file.wav` read via ffprobe), so leading and trailing silence are counted.
4. **Frame-accurate export**: `--fps 23.976|25|29.97` and an OpenTimelineIO, FCPXML or CMX3600 writer, so the factory can hand the cuts to Resolve, Premiere or ffmpeg.
5. **Versioned JSON schema** (`"schema": 1`), with numeric confidence per cut.
6. **Word-level input** (Whisper `word_timestamps`) for pauses inside sentences, and to catch fillers inside longer cues.
7. **Exit codes for automation**: 0 when nothing is found, 2 when cuts are found, 1 on errors, so a pipeline step can gate on it.

## Where it fits in a factory flow

```
script ─► TTS (or human recording) ─► transcript (TTS boundaries / Whisper)
       ─► deadair.py --json ─► wrapper: protect list + scene map + true duration
       ─► EDL (csv/json/OTIO) ─► human review gate ─► apply cuts (ffmpeg) ─► picture edit
```

Keep the human review gate. The tool's own documentation says it "prints the cuts, you apply them", and the dry run shows why: unsupervised, it would have removed every dramatic pause in the film.
