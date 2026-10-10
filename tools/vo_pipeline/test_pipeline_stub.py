#!/usr/bin/env python3
"""TEST ONLY - exercises vo_pipeline.py end to end without Edge TTS.

Edge TTS is replaced with SILENT audio sized at a fixed speaking rate, so this proves the parsing,
pause plan, assembly, SRT, deadair.py hand-off and EDL logic work offline. Its timings are NOT a
voiceover and must never be used for an edit. Output goes to a temporary folder that is printed.

    python tools/vo_pipeline/test_pipeline_stub.py
"""
import asyncio, importlib.util, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("vp", os.path.join(HERE, "vo_pipeline.py"))
vp = importlib.util.module_from_spec(spec); spec.loader.exec_module(vp)


async def silent_stub(text, voice, rate, mp3, proxy=None):
    wpm = 150 * (1 + float(rate.strip("%")) / 100)  # pretend the engine default is 150 wpm
    t, out = 0.12, []
    for s in [s for s in re.split(r"(?<=[.?!])\s+", text) if s]:
        d = len(s.split()) / wpm * 60
        out.append({"start": t, "end": t + d, "text": s}); t += d + 0.30
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                    "-t", f"{t + 0.1:.3f}", "-c:a", "libmp3lame", mp3], check=True)
    return out


def main():
    sys.modules.setdefault("edge_tts", type(sys)("edge_tts"))  # satisfy the preflight import
    vp.synth_one = silent_stub
    vp.OUT_ROOT = tempfile.mkdtemp(prefix="vo-stub-")
    sys.argv = ["vo_pipeline.py", "all", "--voice", "TEST-STUB"]
    vp.main()
    run = os.path.join(vp.OUT_ROOT, os.listdir(vp.OUT_ROOT)[0])
    tl = json.load(open(os.path.join(run, "timeline.json")))
    scenes = {x["scene"] for x in tl["timeline"] if x["type"] == "speech"}
    assert len(scenes) == 21, scenes
    assert open(os.path.join(run, "edl.csv"), encoding="utf-8").read().count("SCENE IN") == 21
    cues = tl["cues"]
    assert all(b["start"] >= a["end"] for a, b in zip(cues, cues[1:])), "SRT cues overlap"
    print(f"OK - stub run in {run}")


if __name__ == "__main__":
    main()
