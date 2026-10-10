"""Shared helpers for the Video Factory tests. All audio made here is SYNTHETIC test signal
(tone bursts and digital silence). None of it is, or may be presented as, a voiceover."""
import math, os, struct, subprocess, sys, wave

HERE = os.path.dirname(os.path.abspath(__file__))
PIPE = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(PIPE, "..", ".."))
sys.path.insert(0, PIPE)
SR = 24000


def write_tone_wav(path, duration, spans, sr=SR, freq=220.0, amp=0.3):
    """Silence everywhere except sine bursts over the given [(start, end)] spans."""
    n = int(round(duration * sr))
    buf = bytearray(n * 2)
    for s, e in spans:
        for i in range(max(0, int(s * sr)), min(n, int(e * sr))):
            struct.pack_into("<h", buf, i * 2, int(amp * 32767 * math.sin(2 * math.pi * freq * i / sr)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(bytes(buf))


def write_srt(path, cues):
    def ts(x):
        ms = int(round(x * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(cues, 1):
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n")


def run_deadair(srt):
    """The UNMODIFIED yt-edit skill tool, for before/after comparisons."""
    import json
    out = subprocess.run([sys.executable, os.path.join(REPO, ".claude", "skills", "yt-edit", "deadair.py"),
                          srt, "--json"], capture_output=True, text=True, check=True).stdout
    return json.loads(out)
