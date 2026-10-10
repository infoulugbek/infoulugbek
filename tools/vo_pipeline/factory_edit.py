#!/usr/bin/env python3
"""factory_edit.py - Video Factory edit analysis: dead air, fillers and retakes, done safely.

This is our integration layer around the yt-edit skill. The skill's `deadair.py` is NOT modified
and is still run for an audit trail, but cut decisions come from here, because deadair.py:

  1. has no notion of intentional pauses, so it proposes cutting every dramatic silence;
  2. strips digits before comparing sentence openings, so "In 1913, the jaw..." followed by
     "In 1915, the jaw..." is reported as a retake;
  3. treats the end of the last cue as the file's duration, ignoring opening silence and the tail.

Fixes:
  1. PROTECTED ranges: a cut is never allowed to overlap one. Overlapping parts are subtracted,
     and `assert_no_protected` is checked again before anything is written.
  2. RETAKES keep numbers, and a pair of cues is not a retake if they differ in any number, date
     or named entity, if they're too far apart in time, or if their wording diverges.
  3. DURATION is read from the media itself (exact WAV sample count, or ffprobe for other files).

    python factory_edit.py narration.srt --media narration.wav --protect protect.json [--json]

`protect.json` is a list of {"start", "end", "why", "kind"} where kind is "protected" (never cut)
or "breath" (may be offered as an optional trim, never applied automatically).
"""
import argparse, difflib, json, os, re, subprocess, sys, wave

FILLER_ONLY = re.compile(r"^[\s,.-]*((um+|uh+|er+|ah+|so|okay|ok|right|yeah|like|anyway|basically|"
                         r"actually|you know|i mean|let me see|hold on)[\s,.-]*)+$", re.I)
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"}
NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
                "eighty", "ninety", "hundred", "thousand", "million", "first", "second", "third"}


# ----------------------------------------------------------------------------- input
def parse_ts(s):
    s = s.strip().replace(",", ".")
    p = s.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2]) if len(p) == 3 else int(p[0]) * 60 + float(p[1])


def load_cues(path):
    """SRT, VTT or Whisper-style JSON -> [(start, end, text)], sorted by start."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:  # -sig: Notepad adds a BOM
        raw = f.read()
    if path.lower().endswith(".json"):
        d = json.loads(raw)
        segs = d.get("segments", []) if isinstance(d, dict) else d
        cues = [(float(s["start"]), float(s["end"]), (s.get("text") or "").strip()) for s in segs]
    else:
        cues, cur = [], None
        for line in raw.splitlines():
            m = re.match(r"\s*(\d[\d:.,]+)\s*-->\s*(\d[\d:.,]+)", line)
            if m:
                cur = [parse_ts(m.group(1)), parse_ts(m.group(2)), []]
                cues.append(cur)
            elif cur is not None and line.strip() and not line.strip().isdigit():
                cur[2].append(line.strip())
        cues = [(a, b, " ".join(t)) for a, b, t in cues if t]
    return sorted(cues, key=lambda c: c[0])


def media_duration(path):
    """Actual duration of the audio, including opening silence and tail.

    WAV is exact (sample count) and is the authority. For compressed files ffprobe reports the
    container duration, which for MP3 includes frame padding (measured: +0.064 s on 5.000 s of
    audio), so edit decisions should always be made against the WAV."""
    if path.lower().endswith(".wav"):
        with wave.open(path) as w:
            return w.getnframes() / w.getframerate()
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


# ----------------------------------------------------------------------------- protected ranges
def subtract(interval, ranges):
    """interval minus the union of ranges -> list of remaining (start, end) pieces."""
    pieces = [interval]
    for rs, re_ in ranges:
        nxt = []
        for s, e in pieces:
            if re_ <= s or rs >= e:
                nxt.append((s, e)); continue
            if s < rs: nxt.append((s, rs))
            if re_ < e: nxt.append((re_, e))
        pieces = nxt
    return pieces


def assert_no_protected(cuts, protected, eps=1e-6):
    for c in cuts:
        for p in protected:
            if c["start"] < p["end"] - eps and c["end"] > p["start"] + eps:
                raise AssertionError(f"cut {c['start']:.3f}-{c['end']:.3f} overlaps protected "
                                     f"{p['start']:.3f}-{p['end']:.3f} ({p.get('why', '')})")


# ----------------------------------------------------------------------------- retakes
def tokens(text):
    """Lowercased words, KEEPING digits (1913 and 1915 must stay different)."""
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace("’", "'"))


def distinguishing(text):
    """Numbers, dates and named entities: things that make two similar sentences different facts."""
    out = set(NUMBER.findall(text))
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in MONTHS or lw in NUMBER_WORDS:
            out.add(lw)
        elif w[0].isupper() and i > 0:  # capitalized, not sentence-initial -> likely a name
            out.add(lw)
    return out


def is_retake(prev, cur, window=15.0, prefix=4, ratio=0.6):
    """True if `cur` restarts `prev`. Both are (start, end, text)."""
    if cur[0] - prev[1] > window:
        return False
    a, b = tokens(prev[2]), tokens(cur[2])
    if len(a) < 3 or len(b) < 3 or a[:prefix] != b[:prefix]:
        return False
    da, db = distinguishing(prev[2]), distinguishing(cur[2])
    # Different facts, not a retake: a number or name in the abandoned take that the new one drops
    # or changes. A cut-off take may simply not have reached the later names yet, so only facts
    # within the shared span count.
    shared = min(len(a), len(b))
    span_a = distinguishing(" ".join(prev[2].split()[:shared]))
    span_b = distinguishing(" ".join(cur[2].split()[:shared]))
    if span_a != span_b or (da - db and db - da):
        return False
    # The abandoned take is a prefix of the new one (cut off), or the two say nearly the same thing.
    if b[:len(a)] == a or a[:len(b)] == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= max(ratio, 0.8)


# ----------------------------------------------------------------------------- analysis
def analyze(cues, duration, protected=(), breaths=(), floor=0.45, min_cut=0.15):
    """Return a dict of cuts. Only 'cuts' may be applied automatically (after review)."""
    prot = sorted((p["start"], p["end"]) for p in protected)
    raw = []
    for i, (s, e, t) in enumerate(cues):
        if FILLER_ONLY.match(t):
            raw.append({"kind": "FILLER", "start": s, "end": e, "why": t.strip()[:48]})
        if i:
            gap = s - cues[i - 1][1]
            if gap > floor:
                keep = floor / 2
                raw.append({"kind": "DEAD", "start": cues[i - 1][1] + keep, "end": s - keep,
                            "why": f"{gap:.2f}s gap"})
        if t.strip() and not FILLER_ONLY.match(t):
            j = i - 1
            while j >= 0 and (FILLER_ONLY.match(cues[j][2]) or not cues[j][2].strip()):
                j -= 1
            if j >= 0 and is_retake(cues[j], cues[i]):
                raw.append({"kind": "REPEAT", "start": cues[j][0], "end": cues[j][1],
                            "why": f'restart of "{" ".join(tokens(cues[j][2])[:5])}"'})
    # Opening and closing silence are real time in the file. Reported, never cut.
    lead = cues[0][0] if cues else duration
    tail = duration - (cues[-1][1] if cues else 0.0)

    cuts, optional, preserved = [], [], []
    for c in sorted(raw, key=lambda c: c["start"]):
        if c["end"] <= c["start"]:
            continue
        pieces = subtract((c["start"], c["end"]), prot)
        if sum(e - s for s, e in pieces) < (c["end"] - c["start"]) - 1e-9:
            preserved.append({**c, "why": c["why"] + " (overlaps a protected pause)"})
        for s, e in pieces:
            if e - s < min_cut:
                continue
            piece = {**c, "start": round(s, 3), "end": round(e, 3)}
            in_breath = c["kind"] == "DEAD" and any(s < b["end"] and e > b["start"] for b in breaths)
            (optional if in_breath else cuts).append(piece)
    assert_no_protected(cuts + optional, protected)
    cut_s = sum(c["end"] - c["start"] for c in cuts)
    opt_s = sum(c["end"] - c["start"] for c in optional)
    return {"duration": round(duration, 3), "lead_silence": round(lead, 3), "tail": round(tail, 3),
            "cues": len(cues), "cuts": cuts, "optional": optional, "preserved": preserved,
            "removed": round(cut_s, 3), "out": round(duration - cut_s, 3),
            "out_if_optional_trimmed": round(duration - cut_s - opt_s, 3)}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript")
    ap.add_argument("--media", required=True, help="the audio file: its real duration is used")
    ap.add_argument("--protect", help="JSON list of {start,end,why,kind}")
    ap.add_argument("--floor", type=float, default=0.45)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    ranges = json.load(open(a.protect, encoding="utf-8")) if a.protect else []
    res = analyze(load_cues(a.transcript), media_duration(a.media),
                  [r for r in ranges if r.get("kind", "protected") == "protected"],
                  [r for r in ranges if r.get("kind") == "breath"], a.floor)
    if a.json:
        print(json.dumps(res, indent=1)); return
    print(f"\n  {a.transcript}: {res['duration']:.2f}s of audio (lead {res['lead_silence']:.2f}s, "
          f"tail {res['tail']:.2f}s), {res['cues']} cues")
    for label, items in (("CUT", res["cuts"]), ("OPTIONAL", res["optional"])):
        for c in items:
            print(f"    {label:<9}{c['kind']:<7} {c['start']:8.2f} -> {c['end']:8.2f}  {c['why']}")
    print(f"\n  {len(res['preserved'])} protected overlaps kept; {res['removed']:.2f}s cut; "
          f"{res['out']:.2f}s out ({res['out_if_optional_trimmed']:.2f}s with optional trims)\n")


if __name__ == "__main__":
    main()
