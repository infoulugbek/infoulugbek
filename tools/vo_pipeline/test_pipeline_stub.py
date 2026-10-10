#!/usr/bin/env python3
"""TEST ONLY - exercises vo_pipeline.py end to end without Edge TTS.

Edge TTS is replaced by a stand-in that writes SYNTHETIC audio: tone bursts where it claims each
sentence is, digital silence elsewhere. That lets the whole chain run offline: parsing, pause
plan, assembly, SRT, timing check, deadair.py hand-off, Video Factory analysis and EDL. The
`shift` option moves the tones against the reported timings to prove the timing check catches
drift. NOTHING this produces is a voiceover, and its timings must never be used for an edit.

    python tools/vo_pipeline/test_pipeline_stub.py            # prints the temp folder it used
"""
import importlib.util, math, os, re, struct, subprocess, sys, tempfile, wave

HERE = os.path.dirname(os.path.abspath(__file__))


def load_pipeline():
    spec = importlib.util.spec_from_file_location("vo_pipeline_under_test", os.path.join(HERE, "vo_pipeline.py"))
    vp = importlib.util.module_from_spec(spec); spec.loader.exec_module(vp)
    return vp


def make_stub(shift=0.0, silent=False):
    async def tone_stub(text, voice, rate, mp3, proxy=None):
        wpm = 150 * (1 + float(rate.strip("%")) / 100)  # pretend the engine default is 150 wpm
        t, out = 0.12, []
        for s in [s for s in re.split(r"(?<=[.?!])\s+", text) if s]:
            d = len(s.split()) / wpm * 60
            out.append({"start": t, "end": t + d, "text": s}); t += d + 0.30
        total, sr = t + 0.1 - 0.30 + 0.15, 24000
        buf = bytearray(int(total * sr) * 2)
        if not silent:
            for c in out:
                for i in range(int((c["start"] + shift) * sr), min(len(buf) // 2, int((c["end"] + shift) * sr))):
                    struct.pack_into("<h", buf, i * 2, int(9000 * math.sin(2 * math.pi * 220 * i / sr)))
        wav = mp3 + ".src.wav"
        with wave.open(wav, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(bytes(buf))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libmp3lame", mp3], check=True)
        os.remove(wav)
        return out
    return tone_stub


def stub_run(out_root, shift=0.0, silent=False, voice="TEST-STUB"):
    vp = load_pipeline()
    sys.modules.setdefault("edge_tts", type(sys)("edge_tts"))  # satisfy the preflight import
    vp.synth_one = make_stub(shift, silent)
    vp.OUT_ROOT = out_root
    argv = sys.argv
    try:
        sys.argv = ["vo_pipeline.py", "all", "--voice", voice]
        vp.main()
    finally:
        sys.argv = argv
    runs = [d for d in os.listdir(out_root) if d.startswith(f"vo-{voice}-")]
    return vp, os.path.join(out_root, sorted(runs)[-1])


if __name__ == "__main__":
    vp, run = stub_run(tempfile.mkdtemp(prefix="vo-stub-"))
    print(f"\nTEST STUB RUN (synthetic tones, not a voiceover): {run}")
