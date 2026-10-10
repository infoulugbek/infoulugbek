#!/usr/bin/env python3
"""timing_check.py - verify subtitle timings against the audio they claim to describe.

Edge TTS reports sentence offsets from its own synthesis clock. Those offsets can drift from the
decoded audio (MP3 encoder/decoder delay, engine padding), and nothing guarantees they match. This
module measures the audio itself, with ffmpeg's `silencedetect`, and checks:

  * ONSET   - each clip's first sentence starts where speech actually begins (within tolerance);
  * OFFSET  - each clip's last sentence ends where speech actually stops (within tolerance);
  * COVERAGE- every sentence span is mostly sound, not silence;
  * SILENCE - planned pauses really are silent (no speech leaked into them);
  * ORDER   - cues are monotonic, non-overlapping and inside their clip.

If every check passes, the SRT is VERIFIED. Otherwise the result is FORCED_ALIGNMENT_REQUIRED, and
`align_sentences` (fed with word timestamps from faster-whisper) produces measured sentence timings.
Nothing is ever adjusted silently.
"""
import difflib, re, subprocess

VERIFIED, NEEDS_ALIGNMENT = "VERIFIED", "FORCED_ALIGNMENT_REQUIRED"


def detect_silences(path, noise_db=-45, min_silence=0.05):
    """Measured silent intervals [(start, end)] in an audio file, via ffmpeg silencedetect.

    min_silence sets the resolution: a quiet stretch shorter than it is reported as sound. At the
    old 0.12 s, a sentence starting 0.12 s into a clip looked as if speech began at 0.00 s (a false
    0.12 s delta). 0.05 s resolves clip padding; brief stop-consonant gaps inside words may show up
    as silences, which only lowers the coverage figure (the threshold allows for it)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg silencedetect failed: {p.stderr[-400:]}")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", p.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None  # silence running to end of file has no end line
        out.append((max(0.0, s), e))
    return out


def _close_open_silence(sil, duration):
    return [(s, duration if e is None else e) for s, e in sil]


def silent_fraction(a, b, sil):
    if b <= a:
        return 1.0
    covered = sum(max(0.0, min(b, e) - max(a, s)) for s, e in sil)
    return covered / (b - a)


def speech_onset(a, b, sil):
    """First moment in [a, b) that is not silent."""
    t = a
    for s, e in sorted(sil):
        if s <= t < e:
            t = e
    return t if t < b else None


def speech_offset(a, b, sil):
    """Last moment in (a, b] that is not silent."""
    t = b
    for s, e in sorted(sil, reverse=True):
        if s < t <= e:
            t = s
    return t if t > a else None


def validate(timeline, audio_path, duration, tolerance=0.15, coverage=0.6, sil=None):
    """timeline: entries from vo_pipeline timeline.json (speech clips carry 'sentences')."""
    sil = _close_open_silence(detect_silences(audio_path) if sil is None else sil, duration)
    problems, deltas = [], []
    prev_end = -1.0
    for x in timeline:
        if x["type"] == "silence":
            if x["end"] - x["start"] > 0.3 and silent_fraction(x["start"] + 0.05, x["end"] - 0.05, sil) < 0.9:
                problems.append(f"sound inside planned silence {x['start']:.2f}-{x['end']:.2f}s ({x['why']})")
            continue
        sents = x.get("sentences", [])
        if not sents:
            problems.append(f"{x['key']}: no sentence timings"); continue
        for s in sents:
            if s["end"] <= s["start"] or s["start"] < prev_end - 1e-6:
                problems.append(f"{x['key']}: cue order/overlap at {s['start']:.2f}s")
            if s["start"] < x["start"] - 1e-6 or s["end"] > x["end"] + 0.05:
                problems.append(f"{x['key']}: cue outside its clip at {s['start']:.2f}s")
            if 1 - silent_fraction(s["start"], s["end"], sil) < coverage:
                problems.append(f"{x['key']}: cue {s['start']:.2f}-{s['end']:.2f}s is mostly silent")
            prev_end = s["end"]
        on = speech_onset(x["start"], x["end"], sil)
        off = speech_offset(x["start"], x["end"], sil)
        if on is None or off is None:
            problems.append(f"{x['key']}: no speech detected in clip"); continue
        d_on, d_off = sents[0]["start"] - on, sents[-1]["end"] - off
        deltas.append({"key": x["key"], "onset_delta": round(d_on, 3), "offset_delta": round(d_off, 3)})
        if abs(d_on) > tolerance:
            problems.append(f"{x['key']}: reported start differs from measured speech onset by {d_on:+.3f}s")
        if abs(d_off) > tolerance:
            problems.append(f"{x['key']}: reported end differs from measured speech end by {d_off:+.3f}s")
    worst = max((max(abs(d["onset_delta"]), abs(d["offset_delta"])) for d in deltas), default=None)
    return {"status": VERIFIED if not problems else NEEDS_ALIGNMENT, "tolerance": tolerance,
            "clips_checked": len(deltas), "worst_delta": worst, "deltas": deltas,
            "problems": problems, "silences_detected": len(sil)}


# ----------------------------------------------------------------------------- forced alignment
def _norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def align_sentences(sentences, words, min_match=0.5):
    """Map script sentences onto recognized words.

    sentences: [text, ...] in order. words: [(start, end, word), ...] from a recognizer with word
    timestamps. Returns [{"text", "start", "end", "matched"}], with start/end None when a sentence
    could not be anchored (fewer than `min_match` of its words were recognized).
    """
    sent_tokens = [[_norm(w) for w in s.split() if _norm(w)] for s in sentences]
    flat, owner = [], []
    for i, toks in enumerate(sent_tokens):
        flat += toks; owner += [i] * len(toks)
    rec = [_norm(w[2]) for w in words]
    sm = difflib.SequenceMatcher(None, flat, rec, autojunk=False)
    hits = [[] for _ in sentences]
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            hits[owner[blk.a + k]].append(blk.b + k)
    out = []
    for i, s in enumerate(sentences):
        frac = len(hits[i]) / max(1, len(sent_tokens[i]))
        if hits[i] and frac >= min_match:
            out.append({"text": s, "start": words[min(hits[i])][0], "end": words[max(hits[i])][1],
                        "matched": round(frac, 2)})
        else:
            out.append({"text": s, "start": None, "end": None, "matched": round(frac, 2)})
    return out
