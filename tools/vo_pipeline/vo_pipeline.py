#!/usr/bin/env python3
"""vo_pipeline.py - script -> voiceover -> SRT -> EDL, with real timings only.

    python vo_pipeline.py extract                     # parse the script, show segments and planned pauses
    python vo_pipeline.py all                         # calibrate, synthesize, assemble, SRT, EDL
    python vo_pipeline.py all --voice en-US-ChristopherNeural --target-wpm 137
    python vo_pipeline.py edl  --run production/piltdown-v3/<run>   # rebuild the EDL for an existing run
    python vo_pipeline.py check --run production/piltdown-v3/<run>  # re-verify SRT timings against the audio
    python vo_pipeline.py align --run production/piltdown-v3/<run>  # forced alignment with faster-whisper

Every timestamp comes from either (a) Microsoft Edge TTS SentenceBoundary events for the clip they
belong to, or (b) the exact sample count of the clips and silences that were concatenated, and is
then checked against the audio itself (timing_check.py). If that check fails, the run says
FORCED_ALIGNMENT_REQUIRED and the `align` command measures the timings with faster-whisper. Nothing is
estimated. If synthesis fails, the run stops; it never writes placeholder audio or timings.

Cut decisions come from factory_edit.py (protected pauses, retake guard, real duration). The
yt-edit skill's deadair.py is run unmodified, for the audit trail only.

Requirements: Python 3.10+, ffmpeg + ffprobe on PATH, `pip install edge-tts` (and, for `align`,
`pip install faster-whisper`). Output goes to a NEW timestamped folder; existing runs are never
overwritten.
"""
import argparse, asyncio, csv, datetime, json, os, re, shutil, subprocess, sys, time, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factory_edit, timing_check  # noqa: E402  (our integration layer)

REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SCRIPT = os.path.join(REPO, "scripts", "piltdown-man-documentary-v3.md")
OUTLINE = os.path.join(REPO, "scripts", "piltdown-man-v3-production-outline.md")
PAUSES = os.path.join(HERE, "piltdown-v3-pauses.json")
DEADAIR = os.path.join(REPO, ".claude", "skills", "yt-edit", "deadair.py")
OUT_ROOT = os.path.join(REPO, "production", "piltdown-v3")
SR = 24000  # Edge TTS mp3 output is 24 kHz mono; everything is normalized to this.
TICKS = 10_000_000  # Edge TTS offsets are in 100-ns units.


# ----------------------------------------------------------------------------- file helpers
# Always close files: on Windows an open handle blocks renames/deletes, and unflushed data can be
# missing when ffmpeg or the next step reads the file.
def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)


# ----------------------------------------------------------------------------- script parsing
def parse_script(path=SCRIPT):
    """Return scenes in order: id, title, part, and a list of items (narration paragraphs and cues)."""
    body = read_text(path).split("## Hooks (scored")[0]
    scenes, cur, part = [], None, None
    for line in body.splitlines():
        if line.startswith("## "):
            part = line[3:].strip(); continue
        m = re.match(r"### (S\d+) · (.+)", line)
        if m:
            cur = {"id": m.group(1), "title": m.group(2).strip(), "part": part, "items": []}
            scenes.append(cur); continue
        if cur is None or not line.strip() or line.startswith("---"):
            continue
        s = line.strip()
        if s.startswith("**[RETENTION:"):
            cur["claims"] = re.findall(r"C\d+a?", s); continue
        if s.startswith("**["):
            kind = re.match(r"\*\*\[([A-Z ]+):", s)
            cur["items"].append({"type": "cue", "kind": (kind.group(1) if kind else "CUE").strip(),
                                 "text": s.strip("*[] ")}); continue
        cur["items"].append({"type": "para", "text": s.replace("*", "")})
    for sc in scenes:
        n = 0
        for it in sc["items"]:
            if it["type"] == "para":
                n += 1; it["n"] = n; it["key"] = f"{sc['id']}.{n}"
    return scenes


def plan(scenes, pauses):
    """Flatten to a playlist of ('speech', para) and ('silence', seconds, why, protected)."""
    seq = [("silence", pauses["lead_in"], "lead-in before the hook (foley)", True)]
    for si, sc in enumerate(scenes):
        paras = [i for i in sc["items"] if i["type"] == "para"]
        for pi, p in enumerate(paras):
            seq.append(("speech", sc["id"], p))
            last_para, last_scene = pi == len(paras) - 1, si == len(scenes) - 1
            special = pauses["after"].get(p["key"])
            if last_para and last_scene:
                seq.append(("silence", max(pauses["tail"], special["seconds"] if special else 0),
                            "tail: music resolves", True)); continue
            if special:
                seq.append(("silence", special["seconds"], special["why"], True))
            elif last_para:
                new_part = scenes[si + 1]["part"] != sc["part"]
                seq.append(("silence", pauses["part_gap"] if new_part else pauses["scene_gap"],
                            "part transition" if new_part else "scene transition", True))
            else:
                seq.append(("silence", pauses["para_gap"], "paragraph breath", False))
    return seq


# ----------------------------------------------------------------------------- synthesis
async def synth_one(text, voice, rate, mp3_path, proxy=None):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="SentenceBoundary", proxy=proxy)
    sents = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "SentenceBoundary":
                sents.append({"start": chunk["offset"] / TICKS,
                              "end": (chunk["offset"] + chunk["duration"]) / TICKS,
                              "text": chunk["text"]})
    if os.path.getsize(mp3_path) == 0:
        raise RuntimeError(f"Edge TTS returned no audio for: {text[:60]}")
    return sents


def synth(text, voice, rate, mp3_path, proxy=None, tries=3, log=print):
    """synth_one with retries for transient network errors (2 s, 4 s back-off)."""
    for i in range(tries):
        try:
            return asyncio.run(synth_one(text, voice, rate, mp3_path, proxy))
        except Exception as e:  # edge-tts raises several network error types
            if i == tries - 1:
                raise
            log(f"  retry {i + 1}/{tries - 1} after {type(e).__name__}: {e}")
            time.sleep(2 ** (i + 1))


def to_wav(src, dst):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-ac", "1", "-ar", str(SR),
                    "-c:a", "pcm_s16le", dst], check=True)


def wav_seconds(path):
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()


def write_silence(path, seconds):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(b"\x00\x00" * int(round(seconds * SR)))


def words(text):
    return len(text.split())


def rate_str(pct):
    return f"{'+' if pct >= 0 else ''}{int(round(pct))}%"


def calibrate(scenes, voice, target, proxy, work, log):
    """Synthesize a long paragraph, measure real words-per-minute, adjust Edge's rate. Max 4 passes."""
    sample = max((p for sc in scenes for p in sc["items"] if p["type"] == "para"),
                 key=lambda p: words(p["text"]))
    pct = 0.0
    for i in range(4):
        mp3, wav = os.path.join(work, f"calib{i}.mp3"), os.path.join(work, f"calib{i}.wav")
        sents = synth(sample["text"], voice, rate_str(pct), mp3, proxy, log=log)
        to_wav(mp3, wav)
        # Speech span from the engine's own boundaries, so leading/trailing padding is excluded.
        span = (sents[-1]["end"] - sents[0]["start"]) if sents else wav_seconds(wav)
        wpm = words(sample["text"]) / span * 60
        log(f"calibration pass {i + 1}: rate {rate_str(pct)} -> {wpm:.1f} wpm on {sample['key']}")
        if abs(wpm - target) <= 1.5:
            break
        pct = (1 + pct / 100) * (target / wpm) * 100 - 100
    return rate_str(pct), wpm


# ----------------------------------------------------------------------------- assembly
def ts(sec, sep=","):
    ms = int(round(sec * 1000)); h, ms = divmod(ms, 3_600_000); m, ms = divmod(ms, 60_000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def tc(sec):
    """Editor-friendly timecode, minutes:seconds.tenths."""
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:04.1f}"


def concat_line(abs_path):
    """One line of an ffmpeg concat list. Forward slashes work on Windows; quotes are escaped."""
    p = abs_path.replace("\\", "/").replace("'", "'\\''")
    return f"file '{p}'"


def write_srt(path, cues):
    """Write cues as SRT, refusing anything that would desync: empty, reversed or overlapping cues.
    Times are rounded to the millisecond once, and rounding can never create an overlap."""
    out, prev_end_ms = [], -1
    for i, c in enumerate(cues, 1):
        a, b = int(round(c["start"] * 1000)), int(round(c["end"] * 1000))
        if b <= a:
            raise ValueError(f"cue {i} has no duration: {c['start']:.3f}-{c['end']:.3f}")
        if a < prev_end_ms:
            raise ValueError(f"cue {i} starts before cue {i - 1} ends ({a} ms < {prev_end_ms} ms)")
        out.append(f"{i}\n{ts(a / 1000)} --> {ts(b / 1000)}\n{c['text']}\n")
        prev_end_ms = b
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))


def protect_ranges(timeline):
    """Planned silences as protect.json entries for factory_edit."""
    return [{"start": round(x["start"], 3), "end": round(x["end"], 3), "why": x["why"],
             "kind": "protected" if x["protected"] else "breath"}
            for x in timeline if x["type"] == "silence"]


def assemble(seq, voice, rate, proxy, run, log):
    clips = os.path.join(run, "clips"); os.makedirs(clips)
    timeline, cues, listing, t, owner = [], [], [], 0.0, None
    for idx, item in enumerate(seq):
        if item[0] == "silence":
            _, sec, why, protected = item
            path = os.path.join(clips, f"{idx:03d}_sil.wav"); write_silence(path, sec)
            dur = wav_seconds(path)
            timeline.append({"type": "silence", "scene": owner or "S01", "start": t, "end": t + dur,
                             "why": why, "protected": protected})
        else:
            _, sid, p = item
            owner = sid
            mp3 = os.path.join(clips, f"{idx:03d}_{p['key']}.mp3"); wav = mp3[:-4] + ".wav"
            log(f"synth {p['key']} ({words(p['text'])} words)")
            sents = synth(p["text"], voice, rate, mp3, proxy, log=log)
            to_wav(mp3, wav); dur = wav_seconds(wav)
            glob = [{"start": t + s["start"], "end": t + s["end"], "text": s["text"], "key": p["key"]} for s in sents]
            cues += glob
            timeline.append({"type": "speech", "scene": sid, "key": p["key"], "text": p["text"],
                             "start": t, "end": t + dur, "sentences": glob,
                             "speech_start": glob[0]["start"] if glob else t,
                             "speech_end": glob[-1]["end"] if glob else t + dur})
            path = wav
        listing.append(concat_line(os.path.abspath(path)))
        t += dur
    lst = os.path.join(run, "concat.txt")
    with open(lst, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(listing) + "\n")
    master = os.path.join(run, "narration.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", master], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", master, "-c:a", "libmp3lame", "-b:a", "192k",
                    os.path.join(run, "narration.mp3")], check=True)
    total = wav_seconds(master)
    if abs(total - t) > 0.005:
        raise RuntimeError(f"assembled length {total:.3f}s != sum of parts {t:.3f}s; timings unsafe")
    write_srt(os.path.join(run, "narration.srt"), cues)
    write_json(os.path.join(run, "timeline.json"),
               {"voice": voice, "rate": rate, "duration": total, "timeline": timeline, "cues": cues})
    write_json(os.path.join(run, "protect.json"), protect_ranges(timeline))
    return total


def check_timing(run, log=print, tolerance=0.15):
    data = read_json(os.path.join(run, "timeline.json"))
    res = timing_check.validate(data["timeline"], os.path.join(run, "narration.wav"), data["duration"], tolerance)
    write_json(os.path.join(run, "timing.json"), res)
    log(f"timing check: {res['status']} ({res['clips_checked']} clips, worst delta "
        f"{res['worst_delta'] if res['worst_delta'] is not None else 'n/a'}s, tolerance {tolerance}s)")
    for p in res["problems"][:10]:
        log(f"  - {p}")
    if res["status"] != timing_check.VERIFIED:
        log("  Edge TTS timings could NOT be verified against the audio. Forced alignment is required: "
            "run `vo_pipeline.py align --run <this folder>` before using the SRT or EDL for editing.")
    return res


# ----------------------------------------------------------------------------- EDL
def outline_estimates(path=OUTLINE):
    est = {}
    if not os.path.exists(path):
        return est
    for m in re.finditer(r"^\| (S\d+) [^|]+\| [^|]+\| (\d+) \| ~(\d+)–(\d+) s \|", read_text(path), re.M):
        est[m.group(1)] = {"words": int(m.group(2)), "lo": int(m.group(3)), "hi": int(m.group(4))}
    return est


def timing_status(run):
    """Which SRT the EDL should trust, and how far."""
    al = os.path.join(run, "alignment.json")
    if os.path.exists(al):
        a = read_json(al)
        return a["status"], "narration.aligned.srt", a
    tf = os.path.join(run, "timing.json")
    if os.path.exists(tf):
        t = read_json(tf)
        return t["status"], "narration.srt", t
    return "UNCHECKED", "narration.srt", {}


def build_edl(run, log=print):
    data = read_json(os.path.join(run, "timeline.json"))
    tl = data["timeline"]
    total = factory_edit.media_duration(os.path.join(run, "narration.wav"))  # real audio length
    status, srt_name, _ = timing_status(run)
    srt = os.path.join(run, srt_name)
    scenes = parse_script()
    # Audit trail: the skill's own verdict, unmodified.
    raw = subprocess.run([sys.executable, DEADAIR, srt, "--json"], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    write_text(os.path.join(run, "deadair.json"), raw)
    dead = json.loads(raw)
    ranges = protect_ranges(tl)
    res = factory_edit.analyze(factory_edit.load_cues(srt), total,
                               [r for r in ranges if r["kind"] == "protected"],
                               [r for r in ranges if r["kind"] == "breath"])
    write_json(os.path.join(run, "factory_edit.json"), res)

    speech = [x for x in tl if x["type"] == "speech"]
    if srt_name == "narration.aligned.srt":  # use measured times for paragraph boundaries too
        al = read_json(os.path.join(run, "alignment.json"))["sentences"]
        for x in speech:
            got = [s for s in al if s["key"] == x["key"] and s["start"] is not None]
            if got:
                x["speech_start"], x["speech_end"] = got[0]["start"], got[-1]["end"]
    first = {}
    for x in speech:
        first.setdefault(x["scene"], x)
    order = [sc["id"] for sc in scenes]
    bounds = {}
    for i, sid in enumerate(order):
        cur = first[sid]
        prev_end = 0.0 if i == 0 else max(x["speech_end"] for x in speech if x["scene"] == order[i - 1])
        gap = cur["speech_start"] - prev_end
        # The picture holds through a dramatic pause and cuts just before the next line.
        start = 0.0 if i == 0 else cur["speech_start"] - min(0.4, gap / 2)
        bounds[sid] = [start, None]
        if i:
            bounds[order[i - 1]][1] = start
    bounds[order[-1]][1] = total
    est = outline_estimates()
    warn = status not in (timing_check.VERIFIED, "ALIGNED")
    rows, md = [], []
    md += ["# Piltdown v3: Edit Decision List", "",
           f"Generated {datetime.datetime.now():%Y-%m-%d %H:%M} from `{os.path.basename(os.path.normpath(run))}`.",
           f"Voice `{data['voice']}`, rate `{data['rate']}`. Runtime **{tc(total)}** ({total:.2f} s of audio, "
           f"including {res['lead_silence']:.2f} s before the first word and {res['tail']:.2f} s after the last).", "",
           f"**Timing status: {status}** (subtitle source `{srt_name}`).", ""]
    if warn:
        md += ["> **WARNING:** these timings have not been verified against the audio. Do not cut picture",
               "> to them until `vo_pipeline.py align --run <folder>` has produced a verified alignment.", ""]
    md += ["Times are in minutes:seconds.tenths from the start of `narration.wav`.", "",
           "Legend: **PRESERVE** = a planned dramatic pause (can never be cut); **OPTIONAL TRIM** = a planned",
           "paragraph breath (never applied automatically); **SUGGESTED CUT** = an unplanned gap, filler or",
           "retake found by the Video Factory analysis; **VISUAL** = picture change; **SOUND** = music or beat cue.", ""]
    for sc in scenes:
        sid = sc["id"]; a, b = bounds[sid]
        paras = {x["key"]: x for x in speech if x["scene"] == sid}
        e = est.get(sid)
        dur = b - a
        verdict = "n/a" if not e else ("within" if e["lo"] - 2 <= dur <= e["hi"] + 2 else ("LONGER" if dur > e["hi"] else "SHORTER"))
        md += [f"## {sid} · {sc['title']}", "",
               f"**In {tc(a)} · Out {tc(b)} · Duration {dur:.1f} s**"
               + (f" (outline estimate ~{e['lo']}–{e['hi']} s: {verdict})" if e else ""), ""]
        events, pending, prev = [], [], None
        for it in sc["items"]:
            if it["type"] == "cue":
                pending.append(it); continue
            p = paras[it["key"]]
            for cue in pending:
                if cue["kind"] == "ON SCREEN":  # picture changes with the line it introduces
                    events.append((a if p is first[sid] else p["speech_start"], "VISUAL", cue["text"]))
                else:  # sound and beats start where the previous line ends
                    events.append((a if prev is None else prev["speech_end"], "SOUND", cue["text"]))
            pending, prev = [], p
            events.append((p["speech_start"], "NARRATION", f"{it['key']} \"{it['text'][:70]}{'…' if len(it['text']) > 70 else ''}\" (ends {tc(p['speech_end'])})"))
        for cue in pending:  # cues after the last paragraph land at the end of the scene's speech
            last = max(paras.values(), key=lambda x: x["speech_end"])
            events.append((last["speech_end"], "VISUAL" if cue["kind"] == "ON SCREEN" else "SOUND", cue["text"]))
        for s in [x for x in tl if x["type"] == "silence" and x["scene"] == sid]:
            if s["protected"]:
                events.append((s["start"], "PRESERVE", f"{s['end'] - s['start']:.2f} s silence: {s['why']}"))
        for label, items in (("OPTIONAL TRIM", res["optional"]), ("SUGGESTED CUT", res["cuts"])):
            for c in items:
                if a <= c["start"] < b:
                    events.append((c["start"], label, f"{c['kind']} {c['start']:.2f}–{c['end']:.2f} s: {c['why']}"))
        events.sort(key=lambda x: x[0])
        md += ["| Time | Event | Detail |", "|---|---|---|"] + [f"| {tc(t)} | {k} | {d.replace('|', '/')} |" for t, k, d in events] + [""]
        for t, k, d in events:
            rows.append({"scene": sid, "time_s": round(t, 3), "timecode": tc(t), "event": k, "detail": d})
        rows.append({"scene": sid, "time_s": round(a, 3), "timecode": tc(a), "event": "SCENE IN", "detail": sc["title"]})
        rows.append({"scene": sid, "time_s": round(b, 3), "timecode": tc(b), "event": "SCENE OUT", "detail": f"{dur:.2f}s; outline {verdict}"})
    opt_s = sum(c["end"] - c["start"] for c in res["optional"])
    md += ["## Summary", "",
           f"- Timing status: **{status}**.",
           f"- Raw `deadair.py` (unmodified skill) proposed {len(dead['cuts'])} cuts ({dead['removed']:.2f} s) and "
           f"reported {dead['duration']:.2f} s of audio; the real file is {total:.2f} s.",
           f"- Video Factory analysis: {len(res['preserved'])} proposals overlapped protected pauses and were "
           f"refused; {len(res['optional'])} optional breath trims ({opt_s:.2f} s); {len(res['cuts'])} suggested cuts "
           f"({res['removed']:.2f} s).",
           f"- Runtime: {tc(total)} as recorded; {tc(res['out'])} with suggested cuts; "
           f"{tc(res['out_if_optional_trimmed'])} with optional trims too.", ""]
    write_text(os.path.join(run, "edl.md"), "\n".join(md))
    with open(os.path.join(run, "edl.csv"), "w", newline="", encoding="utf-8-sig") as f:  # -sig: Excel on Windows
        w = csv.DictWriter(f, fieldnames=["scene", "time_s", "timecode", "event", "detail"]); w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["time_s"], r["event"] != "SCENE IN")))
    # Outline comparison
    cmp = ["# EDL vs production outline", "", f"Timing status: **{status}**.", "",
           "| Scene | Outline estimate | Measured | Δ vs range | Verdict |", "|---|---|---|---|---|"]
    for sc in scenes:
        sid = sc["id"]; a, b = bounds[sid]; d = b - a; e = est.get(sid)
        if not e:
            cmp.append(f"| {sid} | – | {d:.1f} s | – | n/a |"); continue
        delta = 0 if e["lo"] <= d <= e["hi"] else (d - e["hi"] if d > e["hi"] else d - e["lo"])
        v = "within" if abs(delta) <= 2 else ("LONGER" if delta > 0 else "SHORTER")
        cmp.append(f"| {sid} {sc['title']} | ~{e['lo']}–{e['hi']} s | {d:.1f} s | {delta:+.1f} s | {v} |")
    lo, hi = sum(e["lo"] for e in est.values()), sum(e["hi"] for e in est.values())
    cmp += ["", f"Outline total ~{tc(lo)}–{tc(hi)}; measured {tc(total)}."]
    write_text(os.path.join(run, "edl-vs-outline.md"), "\n".join(cmp) + "\n")
    log(f"EDL written: {len(rows)} rows; {len(res['cuts'])} suggested cuts; "
        f"{len(res['preserved'])} protected pauses defended; runtime {tc(total)}; timing {status}")


# ----------------------------------------------------------------------------- forced alignment
def align(run, model="small.en", log=print):
    """Measure sentence timings from the audio with faster-whisper word timestamps."""
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="auto", compute_type="int8")
    segs, _ = m.transcribe(os.path.join(run, "narration.wav"), word_timestamps=True, vad_filter=False)
    wl = [(w.start, w.end, w.word) for s in segs for w in (s.words or [])]
    write_json(os.path.join(run, "whisper-words.json"), {"words": wl})
    finish_alignment(run, wl, model, log)


def finish_alignment(run, wl, model, log=print):
    cues = read_json(os.path.join(run, "timeline.json"))["cues"]
    res = timing_check.align_sentences([c["text"] for c in cues], wl)
    sentences = [{**r, "key": c["key"], "tts_start": c["start"], "tts_end": c["end"]} for r, c in zip(res, cues)]
    missing = [s for s in sentences if s["start"] is None]
    drift = sorted(abs(s["start"] - s["tts_start"]) for s in sentences if s["start"] is not None)
    status = "ALIGNED" if not missing else "PARTIAL_ALIGNMENT"
    write_json(os.path.join(run, "alignment.json"),
               {"status": status, "model": model, "sentences": sentences,
                "unmatched": [s["text"] for s in missing],
                "median_drift": drift[len(drift) // 2] if drift else None,
                "max_drift": drift[-1] if drift else None})
    if missing:
        log(f"alignment PARTIAL: {len(missing)} sentences could not be anchored; no aligned SRT written for them.")
    # Only measured sentences go into the aligned SRT; nothing is filled in.
    write_srt(os.path.join(run, "narration.aligned.srt"),
              [s for s in sentences if s["start"] is not None])
    if drift:
        log(f"alignment {status}: median drift vs Edge TTS {drift[len(drift) // 2]:.3f}s, max {drift[-1]:.3f}s")
    build_edl(run, log)


# ----------------------------------------------------------------------------- CLI
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        # aiohttp + the Proactor loop prints "Event loop is closed" noise on exit on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["extract", "all", "edl", "check", "align"])
    ap.add_argument("--voice", default="en-US-ChristopherNeural")
    ap.add_argument("--target-wpm", type=float, default=137.0)
    ap.add_argument("--rate", help="skip calibration and use this Edge rate, e.g. -12%%")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY"))
    ap.add_argument("--run", help="existing run folder (edl / check / align)")
    ap.add_argument("--whisper-model", default="small.en")
    ap.add_argument("--tolerance", type=float, default=0.15, help="timing check tolerance, seconds")
    a = ap.parse_args()
    pauses = read_json(PAUSES)
    scenes = parse_script()
    if a.cmd == "extract":
        for item in plan(scenes, pauses):
            if item[0] == "speech":
                print(f"  {item[2]['key']:<7} {words(item[2]['text']):3d}w  {item[2]['text'][:70]}")
            else:
                print(f"  {'':7} pause {item[1]:.2f}s{' PRESERVE' if item[3] else ''}  {item[2]}")
        n = sum(words(p["text"]) for sc in scenes for p in sc["items"] if p["type"] == "para")
        print(f"\n  {len(scenes)} scenes, {n} narrated words, "
              f"{sum(i[1] for i in plan(scenes, pauses) if i[0] == 'silence'):.1f}s of planned silence")
        return
    if a.cmd in ("edl", "check", "align"):
        if not a.run: sys.exit("--run is required")
        if a.cmd == "edl": return build_edl(a.run)
        if a.cmd == "check": check_timing(a.run, tolerance=a.tolerance); return build_edl(a.run)
        return align(a.run, a.whisper_model)
    # Preflight before creating anything, so a failed run leaves no half-written folder.
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool): sys.exit(f"{tool} not found on PATH (see the Windows guide, step 1)")
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        sys.exit("edge-tts is not installed: pip install edge-tts")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run = os.path.join(OUT_ROOT, f"vo-{a.voice}-{stamp}")
    os.makedirs(run, exist_ok=False)  # never overwrite an existing run
    logf = open(os.path.join(run, "run.log"), "a", encoding="utf-8")
    def log(msg):
        print(msg); logf.write(msg + "\n"); logf.flush()
    log(f"script {os.path.relpath(SCRIPT, REPO)}; voice {a.voice}; target {a.target_wpm} wpm")
    try:
        work = os.path.join(run, "calibration"); os.makedirs(work)
        rate, wpm = (a.rate, None) if a.rate else calibrate(scenes, a.voice, a.target_wpm, a.proxy, work, log)
        log(f"using rate {rate}")
        total = assemble(plan(scenes, pauses), a.voice, rate, a.proxy, run, log)
    except Exception as e:
        # Never leave a folder that could be mistaken for a finished voiceover.
        log(f"FAILED: {type(e).__name__}: {e}")
        logf.close(); os.rename(run, run + "-FAILED")
        sys.exit(f"synthesis failed; see {os.path.relpath(run, REPO)}-FAILED{os.sep}run.log")
    tl = read_json(os.path.join(run, "timeline.json"))["timeline"]
    sp = [x for x in tl if x["type"] == "speech"]
    nw = sum(words(x["text"]) for x in sp)
    speaking = sum(x["speech_end"] - x["speech_start"] for x in sp)
    log(f"narration {total:.2f}s; {nw} words; speaking-rate {nw / speaking * 60:.1f} wpm "
        f"(excludes planned pauses); overall {nw / total * 60:.1f} wpm")
    check_timing(run, log, a.tolerance)
    build_edl(run, log)
    log(f"done: {os.path.relpath(run, REPO)}")
    logf.close()


if __name__ == "__main__":
    main()
