"""Timestamp synchronization: SRT writing, concat paths, and the audio-vs-timings validator."""
import ntpath, os, subprocess, tempfile, unittest
from helpers import write_tone_wav
import timing_check as tc
import vo_pipeline as vp


def timeline_for(spans_per_clip, gaps):
    """Build a timeline like vo_pipeline's: clip i holds sentences at the given local spans."""
    tl, t = [], 0.0
    for i, spans in enumerate(spans_per_clip):
        clip_len = spans[-1][1] + 0.15
        sents = [{"start": t + a, "end": t + b, "text": f"s{i}.{j}"} for j, (a, b) in enumerate(spans)]
        tl.append({"type": "speech", "key": f"S01.{i + 1}", "start": t, "end": t + clip_len, "sentences": sents})
        t += clip_len
        tl.append({"type": "silence", "start": t, "end": t + gaps[i], "why": "planned", "protected": True})
        t += gaps[i]
    return tl, t


class Validator(unittest.TestCase):
    def make(self, d, tl, total, shift=0.0, leak=None):
        """Audio whose tones sit exactly where the timeline says speech is (optionally shifted)."""
        spans = [(s["start"] + shift, s["end"] + shift) for x in tl if x["type"] == "speech" for s in x["sentences"]]
        if leak: spans.append(leak)
        p = os.path.join(d, "n.wav"); write_tone_wav(p, total, spans); return p

    def setUp(self):
        self.tl, self.total = timeline_for([[(0.12, 2.0), (2.3, 4.1)], [(0.12, 3.0)]], [1.5, 2.0])

    def test_matching_audio_is_verified(self):
        with tempfile.TemporaryDirectory() as d:
            r = tc.validate(self.tl, self.make(d, self.tl, self.total), self.total)
            self.assertEqual(r["status"], tc.VERIFIED, r["problems"])
            # Regression: sentences start 0.12 s into each clip. With the old 0.12 s detector
            # resolution that short lead-in was invisible and showed up as a false 0.12 s delta.
            self.assertLess(r["worst_delta"], 0.01)

    def test_shifted_audio_requires_forced_alignment(self):
        with tempfile.TemporaryDirectory() as d:
            r = tc.validate(self.tl, self.make(d, self.tl, self.total, shift=0.3), self.total)
            self.assertEqual(r["status"], tc.NEEDS_ALIGNMENT)
            self.assertTrue(any("onset" in p for p in r["problems"]))

    def test_silent_audio_requires_forced_alignment(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.wav"); write_tone_wav(p, self.total, [])
            r = tc.validate(self.tl, p, self.total)
            self.assertEqual(r["status"], tc.NEEDS_ALIGNMENT)

    def test_sound_leaking_into_planned_silence_is_caught(self):
        sil = next(x for x in self.tl if x["type"] == "silence")
        with tempfile.TemporaryDirectory() as d:
            r = tc.validate(self.tl, self.make(d, self.tl, self.total, leak=(sil["start"] + 0.4, sil["end"] - 0.4)), self.total)
            self.assertTrue(any("planned silence" in p for p in r["problems"]))

    def test_mp3_without_delay_tag_stays_within_tolerance(self):
        """Edge TTS mp3s may lack a LAME/Xing header, so decoders cannot trim encoder delay.
        Measure that shift for real and make sure the tolerance covers it."""
        with tempfile.TemporaryDirectory() as d:
            wav = self.make(d, self.tl, self.total)
            mp3, back = os.path.join(d, "n.mp3"), os.path.join(d, "back.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-c:a", "libmp3lame", "-write_xing", "0", mp3], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-i", mp3, "-ac", "1", "-ar", "24000", back], check=True)
            r = tc.validate(self.tl, back, self.total)
            self.assertEqual(r["status"], tc.VERIFIED, r["problems"])
            # Measured on ffmpeg 6.1: about 0.046 s (LAME encoder delay at 24 kHz). Well inside 0.15 s.
            self.assertLess(r["worst_delta"], 0.06)


class EndSnapping(unittest.TestCase):
    """Real Edge TTS (CI, windows-latest): starts within ~0.11 s, but each clip's final sentence
    reported to end 0.84-0.89 s after the speech stops."""

    def setUp(self):
        self.tl, self.total = timeline_for([[(0.12, 2.0), (2.3, 4.1)], [(0.12, 3.0)]], [1.5, 2.0])

    def inflated(self, by=0.86):
        tl = [dict(x) for x in self.tl]
        for x in tl:
            if x["type"] == "speech":
                x["sentences"] = [dict(s) for s in x["sentences"]]
                x["sentences"][-1]["end"] += by; x["end"] += by
        return tl

    def test_inflated_ends_fail_then_snap_to_verified(self):
        with tempfile.TemporaryDirectory() as d:
            true_spans = [(s["start"], s["end"]) for x in self.tl if x["type"] == "speech" for s in x["sentences"]]
            p = os.path.join(d, "n.wav"); write_tone_wav(p, self.total + 2, true_spans)
            bad = self.inflated()
            self.assertEqual(tc.validate(bad, p, self.total + 2)["status"], tc.NEEDS_ALIGNMENT)
            snapped, moved = tc.snap_ends(bad, p, self.total + 2)
            self.assertEqual(moved, 2)
            r = tc.validate(snapped, p, self.total + 2)
            self.assertEqual(r["status"], tc.VERIFIED, r["problems"])
            for x0, x1 in zip(self.tl, snapped):
                for s0, s1 in zip(x0.get("sentences", []), x1.get("sentences", [])):
                    self.assertEqual(s1["start"], s0["start"], "starts are never moved")
                    self.assertLess(abs(s1["end"] - s0["end"]), 0.1)
            self.assertTrue(all("engine_end" in x["sentences"][-1] for x in snapped if x["type"] == "speech"))

    def test_snapping_never_lengthens_or_moves_starts(self):
        with tempfile.TemporaryDirectory() as d:
            spans = [(s["start"], s["end"]) for x in self.tl if x["type"] == "speech" for s in x["sentences"]]
            p = os.path.join(d, "n.wav"); write_tone_wav(p, self.total, spans)
            snapped, moved = tc.snap_ends(self.tl, p, self.total)
            for x0, x1 in zip(self.tl, snapped):
                for s0, s1 in zip(x0.get("sentences", []), x1.get("sentences", [])):
                    self.assertLessEqual(s1["end"], s0["end"]); self.assertEqual(s1["start"], s0["start"])


class SrtAndPaths(unittest.TestCase):
    def test_srt_rejects_overlap_and_empty_cues(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.srt")
            with self.assertRaises(ValueError):
                vp.write_srt(p, [{"start": 0, "end": 2, "text": "a"}, {"start": 1.5, "end": 3, "text": "b"}])
            with self.assertRaises(ValueError):
                vp.write_srt(p, [{"start": 1, "end": 1.0002, "text": "a"}])

    def test_srt_timestamp_format(self):
        self.assertEqual(vp.ts(0), "00:00:00,000")
        self.assertEqual(vp.ts(3723.4567), "01:02:03,457")
        self.assertEqual(vp.ts(59.9996), "00:01:00,000")

    def test_srt_roundtrip_is_millisecond_exact(self):
        import factory_edit
        cues = [{"start": 1.2344, "end": 3.5, "text": "One."}, {"start": 3.5004, "end": 7.25, "text": "Two."}]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.srt"); vp.write_srt(p, cues)
            back = factory_edit.load_cues(p)
            for c, (a, b, t) in zip(cues, back):
                self.assertLessEqual(abs(a - c["start"]), 0.0005); self.assertLessEqual(abs(b - c["end"]), 0.0005)

    def test_windows_path_in_concat_list(self):
        win = ntpath.join("C:\\Users\\O'Neil", "My Videos", "clip 001.wav")
        self.assertEqual(vp.concat_line(win), "file 'C:/Users/O'\\''Neil/My Videos/clip 001.wav'")

    def test_concat_handles_spaces_and_apostrophes_for_real(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "O'Neil's clips"); os.makedirs(sub)
            a, b = os.path.join(sub, "a 1.wav"), os.path.join(sub, "b 2.wav")
            write_tone_wav(a, 1.0, [(0.2, 0.8)]); write_tone_wav(b, 1.5, [])
            lst = os.path.join(d, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:  # must be closed before ffmpeg reads it (Windows)
                f.write(vp.concat_line(a) + "\n" + vp.concat_line(b) + "\n")
            out = os.path.join(d, "out.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out], check=True)
            self.assertAlmostEqual(vp.wav_seconds(out), 2.5, places=4)


class EngineOverlaps(unittest.TestCase):
    """Found on real Edge TTS (GitHub Actions, windows-latest): sentence 1 reported to end at
    5.058 s while sentence 2 started at 5.008 s. The SRT writer refused it, which is correct."""

    def test_measured_edge_overlap_is_resolved_and_recorded(self):
        sents = [{"start": 0.1, "end": 5.058, "text": "One."}, {"start": 5.008, "end": 7.0, "text": "Two."}]
        out = vp.resolve_overlaps(sents, "S01.1")
        self.assertEqual(out[0]["end"], 5.008)
        self.assertEqual(out[0]["engine_end"], 5.058)
        self.assertAlmostEqual(out[0]["overlap_fixed"], 0.05, places=3)
        self.assertEqual(sents[0]["end"], 5.058, "input must not be mutated")
        with tempfile.TemporaryDirectory() as d:
            vp.write_srt(os.path.join(d, "ok.srt"), out)  # no longer raises

    def test_large_overlap_is_refused(self):
        with self.assertRaises(RuntimeError):
            vp.resolve_overlaps([{"start": 0, "end": 5.5, "text": "a"}, {"start": 5.0, "end": 7, "text": "b"}], "S01.1")

    def test_no_overlap_untouched(self):
        s = [{"start": 0, "end": 1, "text": "a"}, {"start": 1.2, "end": 2, "text": "b"}]
        self.assertEqual(vp.resolve_overlaps(s, "k"), s)


class CrossDrivePaths(unittest.TestCase):
    """Found on GitHub Actions windows-latest: temp dir on C:, repository on D:."""

    def test_rel_never_raises_across_drives(self):
        import unittest.mock as m
        with m.patch.object(vp.os.path, "relpath", side_effect=ValueError("path is on mount 'C:', start on mount 'D:'")):
            self.assertEqual(vp.rel("/x/y"), os.path.abspath("/x/y"))


try:
    import numpy  # noqa: F401  (installed with faster-whisper)
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, "numpy comes with faster-whisper; only needed for `align`")
class WhisperAudioLoader(unittest.TestCase):
    """faster-whisper 1.2.1 + PyAV 19: TypeError in av.open(metadata_errors=...) (seen in CI).
    The pipeline decodes with ffmpeg itself and passes samples, bypassing that code."""

    def test_16k_float_samples(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav"); write_tone_wav(p, 2.0, [(0.5, 1.0)])
            a = vp.load_audio_16k(p)
            self.assertEqual(str(a.dtype), "float32"); self.assertEqual(len(a), 32000)
            self.assertAlmostEqual(float(abs(a).max()), 0.3, places=2)


class ForcedAlignment(unittest.TestCase):
    def test_maps_recognized_words_to_script_sentences(self):
        sents = ["In 1949, Kenneth Oakley tested Piltdown.", "The dawn man was not ancient."]
        words = [(0.5, 0.7, "In"), (0.7, 1.3, "1949,"), (1.3, 1.8, "Kenneth"), (1.8, 2.2, "Oakley"),
                 (2.2, 2.6, "tested"), (2.6, 3.2, "Piltdown."), (3.6, 3.7, "The"), (3.7, 4.0, "dawn"),
                 (4.0, 4.3, "man"), (4.3, 4.5, "was"), (4.5, 4.7, "not"), (4.7, 5.3, "ancient.")]
        r = tc.align_sentences(sents, words)
        self.assertEqual((r[0]["start"], r[0]["end"]), (0.5, 3.2))
        self.assertEqual((r[1]["start"], r[1]["end"]), (3.6, 5.3))

    def test_tolerates_recognition_errors(self):
        r = tc.align_sentences(["Dawson wrote to Woodward in February."],
                               [(0, .4, "Dorson"), (.4, .7, "wrote"), (.7, .8, "to"), (.8, 1.4, "Woodward"),
                                (1.4, 1.5, "in"), (1.5, 2.1, "February.")])
        self.assertEqual((r[0]["start"], r[0]["end"]), (0.4, 2.1))

    def test_unrecognized_sentence_is_left_empty_not_invented(self):
        r = tc.align_sentences(["Completely different words here."], [(0, 1, "hello"), (1, 2, "world")])
        self.assertIsNone(r[0]["start"]); self.assertIsNone(r[0]["end"])


if __name__ == "__main__":
    unittest.main()
