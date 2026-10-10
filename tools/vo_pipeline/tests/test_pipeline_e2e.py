"""End-to-end runs of vo_pipeline.py with the synthetic tone stub (no network, no real voice)."""
import csv, hashlib, io, os, sys, tempfile, unittest, contextlib
from helpers import PIPE, REPO
sys.path.insert(0, PIPE)
import test_pipeline_stub as stub
import factory_edit as fe

SCRIPTS = [os.path.join(REPO, "scripts", n) for n in
           ("piltdown-man-documentary.md", "piltdown-man-documentary-v2.md", "piltdown-man-documentary-v3.md",
            "piltdown-man-v3-production-outline.md", "voice-provisional.md")]
SKILL = [os.path.join(REPO, ".claude", "skills", "yt-edit", n) for n in ("deadair.py", "SKILL.md")]


def digest(paths):
    out = {}
    for p in paths:
        with open(p, "rb") as f:
            out[p] = hashlib.sha256(f.read()).hexdigest()
    return out


def quiet_run(**kw):
    tmp = tempfile.mkdtemp(prefix="vo-e2e-")
    with contextlib.redirect_stdout(io.StringIO()):
        vp, run = stub.stub_run(tmp, **kw)
    return vp, run


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = digest(SCRIPTS + SKILL)
        cls.vp, cls.run_dir = quiet_run()
        cls.tl = cls.vp.read_json(os.path.join(cls.run_dir, "timeline.json"))

    def test_scripts_and_skill_untouched(self):
        self.assertEqual(digest(SCRIPTS + SKILL), self.before)

    def test_offsets_equal_sample_counts(self):
        t = 0.0
        for x in self.tl["timeline"]:
            self.assertAlmostEqual(x["start"], t, places=6); t = x["end"]
        self.assertAlmostEqual(fe.media_duration(os.path.join(self.run_dir, "narration.wav")), t, places=4)

    def test_srt_monotonic_and_inside_clips(self):
        cues = fe.load_cues(os.path.join(self.run_dir, "narration.srt"))
        self.assertEqual(len(cues), len(self.tl["cues"]))
        for (a, b, _), (c, d, _) in zip(cues, cues[1:]):
            self.assertLess(a, b); self.assertLessEqual(b, c)
        for x in self.tl["timeline"]:
            for s in x.get("sentences", []):
                self.assertGreaterEqual(s["start"], x["start"]); self.assertLessEqual(s["end"], x["end"] + 1e-6)

    def test_timing_verified_when_audio_matches(self):
        t = self.vp.read_json(os.path.join(self.run_dir, "timing.json"))
        self.assertEqual(t["status"], "VERIFIED", t["problems"][:5])
        self.assertEqual(t["clips_checked"], 59)

    def test_no_cut_touches_a_protected_pause(self):
        res = self.vp.read_json(os.path.join(self.run_dir, "factory_edit.json"))
        prot = [r for r in self.vp.read_json(os.path.join(self.run_dir, "protect.json")) if r["kind"] == "protected"]
        self.assertGreaterEqual(len(prot), 17)
        fe.assert_no_protected(res["cuts"] + res["optional"], prot)
        raw = self.vp.read_json(os.path.join(self.run_dir, "deadair.json"))
        self.assertGreater(len(raw["cuts"]), 0, "the unmodified skill still proposes these cuts")

    def test_duration_is_the_real_audio_length(self):
        res = self.vp.read_json(os.path.join(self.run_dir, "factory_edit.json"))
        self.assertAlmostEqual(res["duration"], self.tl["duration"], places=3)
        self.assertGreaterEqual(res["lead_silence"], 1.0)
        self.assertGreaterEqual(res["tail"], 3.0)

    def test_edl_has_all_21_scenes_and_status(self):
        with open(os.path.join(self.run_dir, "edl.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(sorted({r["scene"] for r in rows if r["event"] == "SCENE IN"}), [f"S{i:02d}" for i in range(1, 22)])
        prot = [r for r in self.vp.read_json(os.path.join(self.run_dir, "protect.json")) if r["kind"] == "protected"]
        # every protected silence (lead-in, 13 dramatic pauses, scene/part transitions, tail) is listed
        self.assertEqual(sum(r["event"] == "PRESERVE" for r in rows), len(prot))
        md = self.vp.read_text(os.path.join(self.run_dir, "edl.md"))
        self.assertIn("Timing status: VERIFIED", md)
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, "edl-vs-outline.md")))


class DriftIsCaught(unittest.TestCase):
    def test_shifted_audio_is_flagged_and_edl_warns(self):
        vp, run = quiet_run(shift=0.35)
        t = vp.read_json(os.path.join(run, "timing.json"))
        self.assertEqual(t["status"], "FORCED_ALIGNMENT_REQUIRED")
        self.assertIn("WARNING", vp.read_text(os.path.join(run, "edl.md")))

    def test_silent_audio_is_never_reported_as_verified(self):
        vp, run = quiet_run(silent=True)
        self.assertEqual(vp.read_json(os.path.join(run, "timing.json"))["status"], "FORCED_ALIGNMENT_REQUIRED")


class EdgeLikeTimings(unittest.TestCase):
    def test_inflated_final_sentence_ends_are_snapped_and_verified(self):
        vp, run = quiet_run(inflate=0.86)  # the overshoot measured on real Edge TTS
        t = vp.read_json(os.path.join(run, "timing.json"))
        self.assertEqual(t["status"], "VERIFIED_AFTER_SNAP", t["problems"][:5])
        self.assertEqual(t["cues_snapped"], 59)
        self.assertEqual(t["before_snap"]["problems"][0][:5], "S01.1")
        self.assertTrue(os.path.exists(os.path.join(run, "narration.engine.srt")))
        md = vp.read_text(os.path.join(run, "edl.md"))
        self.assertIn("Timing status: VERIFIED_AFTER_SNAP", md); self.assertNotIn("WARNING", md)

    def test_onset_drift_is_not_snapped(self):
        vp, run = quiet_run(shift=0.35, inflate=0.86)
        self.assertEqual(vp.read_json(os.path.join(run, "timing.json"))["status"], "FORCED_ALIGNMENT_REQUIRED")


class SubtitleSourcePriority(unittest.TestCase):
    def test_verified_tts_timings_beat_whisper(self):
        vp, run = quiet_run()  # VERIFIED
        words = [(c["start"] - 0.3, c["end"] - 0.3, w) for c in vp.read_json(os.path.join(run, "timeline.json"))["cues"]
                 for w in [c["text"]]]
        with contextlib.redirect_stdout(io.StringIO()):
            vp.finish_alignment(run, words, "stub-words")
        status, srt, _ = vp.timing_status(run)
        self.assertEqual((status, srt), ("VERIFIED", "narration.srt"))


class AlignmentPath(unittest.TestCase):
    def test_alignment_replaces_tts_timings_with_measured_ones(self):
        vp, run = quiet_run(shift=0.35)
        # Stand-in for faster-whisper: words placed where the (shifted) tones really are.
        words = []
        for c in vp.read_json(os.path.join(run, "timeline.json"))["cues"]:
            ws = c["text"].split(); step = (c["end"] - c["start"]) / len(ws)
            words += [(c["start"] + 0.35 + i * step, c["start"] + 0.35 + (i + 1) * step, w) for i, w in enumerate(ws)]
        with contextlib.redirect_stdout(io.StringIO()):
            vp.finish_alignment(run, words, "stub-words")
        al = vp.read_json(os.path.join(run, "alignment.json"))
        self.assertEqual(al["status"], "ALIGNED")
        self.assertAlmostEqual(al["median_drift"], 0.35, places=2)
        self.assertIn("Timing status: ALIGNED", vp.read_text(os.path.join(run, "edl.md")))


if __name__ == "__main__":
    unittest.main()
