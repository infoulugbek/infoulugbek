#!/usr/bin/env python3
"""edge_smoke.py - a small REAL Edge TTS check: are Edge's sentence timings true to the audio?

Synthesizes the first two scenes of the Piltdown v3 script (3 paragraphs, about 1 minute) with the
real Edge TTS service, assembles them exactly as the full pipeline does, then runs the timing
check against the decoded audio. This is a verification sample, NOT the production voiceover:
it is written to a temporary folder (or --out) and should not be used in the edit.

    python tools/vo_pipeline/edge_smoke.py [--voice en-US-ChristopherNeural] [--rate -8%] [--out DIR]

Exit code: 0 = VERIFIED or VERIFIED_AFTER_SNAP, 3 = FORCED_ALIGNMENT_REQUIRED, 1 = synthesis/network failure.
"""
import argparse, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vo_pipeline as vp  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.platform == "win32" and hasattr(vp.asyncio, "WindowsSelectorEventLoopPolicy"):
        vp.asyncio.set_event_loop_policy(vp.asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="en-US-ChristopherNeural")
    ap.add_argument("--rate", default="-8%")
    ap.add_argument("--out")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY"))
    a = ap.parse_args()
    run = a.out or tempfile.mkdtemp(prefix="edge-smoke-")
    os.makedirs(run, exist_ok=True)
    scenes = vp.parse_script()[:2]
    pauses = vp.read_json(vp.PAUSES)
    try:
        total = vp.assemble(vp.plan(scenes, pauses), a.voice, a.rate, a.proxy, run, print)
    except Exception as e:
        print(f"SMOKE FAILED: {type(e).__name__}: {e}")
        return 1
    res = vp.check_timing(run, print)
    vp.build_edl(run, print)
    for name in ("narration.engine.srt", "narration.srt"):  # print, so the CI log carries the evidence
        path = os.path.join(run, name)
        if os.path.exists(path):
            print(f"----- {name} -----"); print(vp.read_text(path))
    summary = {"voice": a.voice, "rate": a.rate, "duration": round(total, 3), "status": res["status"],
               "worst_delta": res["worst_delta"], "deltas": res["deltas"], "problems": res["problems"],
               "cues_snapped": res.get("cues_snapped", 0), "before_snap": res.get("before_snap"),
               "folder": run}
    vp.write_json(os.path.join(run, "smoke-summary.json"), summary)
    print(json.dumps(summary, indent=1))
    return 0 if res["status"] in ("VERIFIED", "VERIFIED_AFTER_SNAP") else 3


if __name__ == "__main__":
    sys.exit(main())
