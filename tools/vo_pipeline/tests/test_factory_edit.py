"""Bugs 1-3 from the yt-edit integration review: protected pauses, retake false positives, duration."""
import os, tempfile, unittest
from helpers import write_srt, write_tone_wav, run_deadair
import factory_edit as fe


class ProtectedPauses(unittest.TestCase):
    cues = [(1.0, 4.0, "You're looking at a fake that lasted 41 years."),
            (6.5, 9.0, "The answer was hidden in plain sight.")]   # 2.5 s gap
    prot = [{"start": 4.2, "end": 6.2, "why": "scripted beat", "kind": "protected"}]

    def test_dead_air_inside_protected_pause_is_never_cut(self):
        r = fe.analyze(self.cues, 10.0, self.prot)
        fe.assert_no_protected(r["cuts"] + r["optional"], self.prot)
        self.assertEqual(r["cuts"], [], "nothing outside the pause is long enough to cut")
        self.assertEqual(len(r["preserved"]), 1)

    def test_partial_overlap_is_trimmed_to_outside_the_pause(self):
        prot = [{"start": 5.0, "end": 6.0, "why": "beat", "kind": "protected"}]
        r = fe.analyze(self.cues, 10.0, prot)
        fe.assert_no_protected(r["cuts"], prot)
        spans = [(c["start"], c["end"]) for c in r["cuts"]]
        self.assertIn((4.225, 5.0), spans)
        self.assertIn((6.0, 6.275), spans)

    def test_breath_is_optional_never_a_cut(self):
        breath = [{"start": 4.0, "end": 6.5, "why": "paragraph breath", "kind": "breath"}]
        r = fe.analyze(self.cues, 10.0, [], breath)
        self.assertEqual(r["cuts"], [])
        self.assertEqual(len(r["optional"]), 1)

    def test_guard_raises_on_violation(self):
        with self.assertRaises(AssertionError):
            fe.assert_no_protected([{"start": 5.0, "end": 5.5}], self.prot)

    def test_upstream_tool_would_have_cut_it(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.srt"); write_srt(p, self.cues)
            self.assertTrue(any(c["kind"] == "DEAD" for c in run_deadair(p)["cuts"]),
                            "documents the original bug in the unmodified skill")


class RetakeDetection(unittest.TestCase):
    def check(self, a, b, expected, gap=0.3):
        prev, cur = (0.0, 2.0, a), (2.0 + gap, 4.0 + gap, b)
        self.assertEqual(fe.is_retake(prev, cur), expected, f"{a!r} / {b!r}")

    def test_different_dates_are_not_retakes(self):
        self.check("In 1913, the jaw was examined in London.", "In 1915, the jaw was examined in Washington.", False)

    def test_different_names_are_not_retakes(self):
        self.check("In 1913, the jaw was studied by Waterston.", "In 1913, the jaw was studied by Miller.", False)

    def test_different_number_words_are_not_retakes(self):
        self.check("The second site was found two miles away.", "The second site was found three miles away.", False)

    def test_different_months_are_not_retakes(self):
        self.check("On the 14th of February he wrote again.", "On the 14th of December he wrote again.", False)

    def test_cut_off_take_is_a_retake(self):
        self.check("In 1913, David Waterston of", "In 1913, David Waterston of King's College London argued in Nature.", True)

    def test_identical_take_is_a_retake(self):
        self.check("The first crack came from chemistry.", "The first crack came from chemistry.", True)

    def test_far_apart_is_not_a_retake(self):
        self.check("The first crack came from chemistry.", "The first crack came from chemistry.", False, gap=60)

    def test_retake_with_filler_between_is_cut(self):
        cues = [(0.0, 1.0, "In 1913, David Waterston of"), (1.2, 1.5, "um"),
                (1.8, 5.0, "In 1913, David Waterston of King's College London argued in Nature.")]
        r = fe.analyze(cues, 5.0)
        kinds = sorted(c["kind"] for c in r["cuts"])
        self.assertEqual(kinds, ["FILLER", "REPEAT"])

    def test_upstream_tool_flags_dated_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.srt")
            write_srt(p, [(0, 2, "In 1913, the jaw was examined in London."),
                          (2.1, 4, "In 1915, the jaw was examined in Washington.")])
            self.assertTrue(any(c["kind"] == "REPEAT" for c in run_deadair(p)["cuts"]),
                            "documents the original bug in the unmodified skill")
            self.assertFalse(any(c["kind"] == "REPEAT" for c in fe.analyze(fe.load_cues(p), 4.0)["cuts"]))


class Duration(unittest.TestCase):
    def test_duration_includes_lead_and_tail(self):
        with tempfile.TemporaryDirectory() as d:
            wav, srt = os.path.join(d, "n.wav"), os.path.join(d, "n.srt")
            write_tone_wav(wav, 9.0, [(1.0, 3.0), (3.4, 6.0)])  # 1 s lead-in, 3 s tail
            write_srt(srt, [(1.0, 3.0, "First line."), (3.4, 6.0, "Second line.")])
            dur = fe.media_duration(wav)
            self.assertAlmostEqual(dur, 9.0, places=4)
            r = fe.analyze(fe.load_cues(srt), dur)
            self.assertAlmostEqual(r["lead_silence"], 1.0, places=3)
            self.assertAlmostEqual(r["tail"], 3.0, places=3)
            self.assertAlmostEqual(r["out"], 9.0 - r["removed"], places=3)
            self.assertAlmostEqual(run_deadair(srt)["duration"], 6.0, places=3,
                                   msg="documents the original bug: upstream stops at the last cue")

    def test_mp3_duration_via_ffprobe(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            wav, mp3 = os.path.join(d, "n.wav"), os.path.join(d, "n.mp3")
            write_tone_wav(wav, 5.0, [(1.0, 2.0)])
            subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-c:a", "libmp3lame", mp3], check=True)
            # MP3 pads to whole frames (1152 samples = 48 ms at 24 kHz) plus encoder delay, so it is
            # only frame-accurate. That is why the pipeline measures narration.wav, never the mp3.
            self.assertAlmostEqual(fe.media_duration(mp3), 5.0, delta=0.1)
            self.assertAlmostEqual(fe.media_duration(wav), 5.0, places=4)

    def test_bom_srt_parses(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bom.srt")
            with open(p, "w", encoding="utf-8-sig") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nHello.\n")
            self.assertEqual(fe.load_cues(p), [(1.0, 2.0, "Hello.")])


if __name__ == "__main__":
    unittest.main()
