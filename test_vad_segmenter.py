"""VAD segmentation tests (plan 2.3): injected fake session, D5 cut rules.

Owner: qa-test-assurance.  The ONNX session is an INJECTED dependency (D8: no
torch/onnxruntime in this suite) — a scripted fake scores each 512-sample
window.  Covers: pause detection with disjoint absolute spans, the min-segment
tail-hold (sub-min units stay open, long pauses flush, ``flush()`` forces the
final candidate), the 30 s force-split cap, ``default_feed`` introspection of
the session's declared inputs, and the D5 midpoint word-timestamp trim that
keeps seam words EXACTLY once across adjacent units (no blind overlap).
"""

import dataclasses
import struct
import unittest

import vad_segmenter as vad
from vad_segmenter import RawSpan, VadSegmenter

WINDOW = vad.WINDOW_SAMPLES                       # 512 samples = 1024 bytes @16 kHz
BLOCK = b"\x01\x00" * vad.WINDOW_SAMPLES          # exactly ONE window per feed
SPEECH_MS_PER_WINDOW = vad.WINDOW_SAMPLES / vad.SAMPLE_RATE * 1000


class _Node:
    def __init__(self, name, shape):
        self.name, self.shape = name, shape


class ScriptedSession:
    """Fake silero session: pops one probability per run() call.

    Declares the classic (x, h, sr) input layout so ``default_feed``
    introspection is exercised for real.
    """

    def __init__(self, scores, *, layout="full"):
        self.scores = list(scores)
        self.calls = 0
        self.feeds = []
        self.layout = layout

    def get_inputs(self):
        if self.layout == "full":
            return [_Node("x", [1, 1, 512]), _Node("h", [2, 1, 128]),
                    _Node("sr", [])]
        if self.layout == "minimal":               # silero v4-style (x, h, sr)
            return [_Node("input", [1, 512]), _Node("sr", [])]
        return []                                  # exotic graph: fallback path

    def run(self, _outputs, feed):
        self.feeds.append(feed)
        score = self.scores[min(self.calls, len(self.scores) - 1)]
        self.calls += 1
        return [[score]]


def _feed_windows(session, windows, **kwargs):
    segmenter = VadSegmenter(session, **kwargs)
    spans = []
    for _ in range(windows):
        spans.extend(segmenter.feed(BLOCK))
    return segmenter, spans


class PauseDetectionTests(unittest.TestCase):
    def test_opens_at_first_speech_and_closes_at_the_pause_cut(self):
        # 6 speech windows (onset 2), then silence (pause 3) -> ONE span
        session = ScriptedSession([0.9] * 6 + [0.0] * 12)
        segmenter, spans = _feed_windows(session, 18, onset_windows=2,
                                         pause_windows=3)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].start_byte, 0)                   # opens at first speech
        self.assertEqual(spans[0].end_byte, 6 * vad.WINDOW_BYTES)  # cut at the pause start
        self.assertEqual(segmenter.bytes_fed, 18 * vad.WINDOW_BYTES)
        self.assertIsNone(segmenter.flush())                       # already closed

    def test_second_unit_yields_a_disjoint_span(self):
        # speech -> pause -> speech -> pause: two DISJOINT spans (D5 base)
        scores = ([0.9] * 5 + [0.0] * 6) * 2
        session = ScriptedSession(scores)
        _segmenter, spans = _feed_windows(session, 22, onset_windows=2,
                                          pause_windows=3)
        self.assertEqual(len(spans), 2)
        self.assertLessEqual(spans[0].end_byte, spans[1].start_byte)
        self.assertEqual(spans[0].start_byte, 0)
        self.assertEqual(spans[1].start_byte, 11 * vad.WINDOW_BYTES)

    def test_brief_speech_below_onset_never_opens_a_unit(self):
        session = ScriptedSession([0.9, 0.9, 0.0, 0.0])            # 2 hits < onset 3
        segmenter, spans = _feed_windows(session, 4, onset_windows=3,
                                          pause_windows=2)
        self.assertFalse(spans)
        self.assertIsNone(segmenter.flush())

    def test_score_normalization_is_clamped_and_never_raises(self):
        self.assertEqual(vad._probability([[0.5]]), 0.5)
        self.assertEqual(vad._probability([0.25]), 0.25)
        self.assertEqual(vad._probability(1.7), 1.0)               # clamped high
        self.assertEqual(vad._probability(-0.4), 0.0)              # clamped low
        self.assertEqual(vad._probability([]), 0.0)
        self.assertEqual(vad._probability(None), 0.0)
        self.assertEqual(vad._probability("junk"), 0.0)

    def test_numpy_like_output_is_tolerated(self):
        class _Arr:                                                # duck-typed tolist()
            def tolist(self):
                return [[0.75]]
        self.assertEqual(vad._probability(_Arr()), 0.75)


class MinSegmentTailHoldTests(unittest.TestCase):
    def test_short_unit_stays_open_until_flush(self):
        # 4 speech windows (~0.13 s) then a pause shorter than MAX_MERGE_PAUSE_S
        # with min_segment_s=60 -> the unit is HELD (sub-min tail merges later)
        session = ScriptedSession([0.9] * 4 + [0.0] * 6)
        segmenter, spans = _feed_windows(session, 10, onset_windows=2,
                                         pause_windows=3, min_segment_s=60.0)
        self.assertFalse(spans)                                   # not closed by the pause
        final = segmenter.flush()
        self.assertIsNotNone(final)                                # sent anyway at stop (D5)
        self.assertEqual(final.start_byte, 0)

    def test_long_pause_flushes_a_short_unit_without_flush_call(self):
        # The hold releases only once the pause RUN itself reaches
        # MAX_MERGE_PAUSE_S (~2 s of silence below threshold)
        session = ScriptedSession([0.9] * 4 + [0.0] * 90)
        _segmenter, spans = _feed_windows(session, 94, onset_windows=2,
                                          pause_windows=3, min_segment_s=60.0)
        self.assertEqual(len(spans), 1)                            # hold released by the pause
        self.assertEqual(spans[0].start_byte, 0)
        self.assertEqual(spans[0].end_byte, 4 * vad.WINDOW_BYTES)  # cut at the pause start

    def test_min_size_hold_then_tail_merge_into_one_unit(self):
        # sub-min first unit + continued speech later must MERGE into one span
        scores = [0.9] * 4 + [0.0] * 3 + [0.9] * 6 + [0.0] * 8
        session = ScriptedSession(scores)
        segmenter, spans = _feed_windows(session, len(scores),
                                         onset_windows=2, pause_windows=3,
                                         min_segment_s=10.0)
        # 10 s min > available audio -> nothing closed during the run
        self.assertFalse(spans)
        final = segmenter.flush()
        self.assertIsNotNone(final)
        self.assertEqual(final.start_byte, 0)                      # ONE merged unit

    def test_min_size_is_derived_from_sample_rate_not_a_guess(self):
        segmenter = VadSegmenter(ScriptedSession([0.0]), min_segment_s=1.0,
                                 sample_rate=16000)
        self.assertEqual(segmenter._min_bytes, 16000 * 2)
        segmenter = VadSegmenter(ScriptedSession([0.0]), min_segment_s=1.0,
                                 sample_rate=8000)
        self.assertEqual(segmenter._min_bytes, 8000 * 2)


class ForceSplitTests(unittest.TestCase):
    CAP_BYTES = int(vad.MAX_UNIT_S * vad.SAMPLE_RATE * 2)

    @staticmethod
    def _split_index():
        # first window whose start reaches the 30 s cap (ceil of cap / window)
        return -(-ForceSplitTests.CAP_BYTES // vad.WINDOW_BYTES)

    def test_unit_force_splits_at_the_30_second_cap(self):
        # Continuous speech: a span is emitted at the cap WITHOUT any pause
        split_index = self._split_index()
        windows_needed = split_index + 4                            # a few past the split
        session = ScriptedSession([0.9])
        segmenter, spans = _feed_windows(session, windows_needed,
                                         onset_windows=2, pause_windows=3)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].start_byte, 0)
        self.assertEqual(spans[0].end_byte, split_index * vad.WINDOW_BYTES)
        self.assertAlmostEqual(spans[0].duration_s, vad.MAX_UNIT_S, delta=0.05)
        # the post-split tail is still open at stream end (flush hands it over)
        final = segmenter.flush()
        self.assertIsNotNone(final)
        self.assertEqual(final.start_byte, spans[0].end_byte)

    def test_second_cap_window_keeps_streaming_units(self):
        split_index = self._split_index()
        total_windows = split_index * 2 + 6                         # TWO cap splits
        session = ScriptedSession([0.9])
        segmenter, spans = _feed_windows(session, total_windows,
                                         onset_windows=2, pause_windows=3)
        self.assertGreaterEqual(len(spans), 2)                      # continuous flow
        for previous, current in zip(spans, spans[1:]):
            self.assertEqual(previous.end_byte, current.start_byte)  # disjoint, gapless
            self.assertEqual(current.start_byte - previous.start_byte,
                             split_index * vad.WINDOW_BYTES)
        final = segmenter.flush()
        self.assertIsNotNone(final)
        self.assertEqual(final.start_byte, spans[-1].end_byte)


class ForceSplitHoldBehaviorTests(unittest.TestCase):
    def test_flush_closes_open_unit_at_pause_run_or_stream_offset(self):
        segmenter = VadSegmenter(ScriptedSession([0.9]), onset_windows=2)
        for _ in range(5):
            segmenter.feed(BLOCK)
        final = segmenter.flush()
        self.assertIsNotNone(final)
        self.assertEqual(final.start_byte, 0)
        self.assertEqual(final.end_byte, 5 * vad.WINDOW_BYTES)
        # state fully reset: a second flush yields nothing
        self.assertIsNone(segmenter.flush())


class FeedInternalsTests(unittest.TestCase):
    def test_partial_windows_accumulate_across_blocks(self):
        session = ScriptedSession([0.9])
        segmenter = VadSegmenter(session, onset_windows=100)       # never opens
        segmenter.feed(b"\x01" * (vad.WINDOW_BYTES - 1))           # one byte short
        self.assertEqual(segmenter.bytes_fed, vad.WINDOW_BYTES - 1)
        self.assertEqual(session.calls, 0)                         # no full window yet
        segmenter.feed(b"\x02")
        self.assertEqual(session.calls, 1)                         # now one window ran

    def test_default_feed_introspects_declared_inputs(self):
        full = ScriptedSession([0.5], layout="full")
        feed = vad.default_feed(full, BLOCK, 16000)
        self.assertIn("x", feed)
        self.assertIn("h", feed)
        self.assertEqual(feed["sr"], 16000)
        self.assertEqual(len(feed["x"][0]), vad.WINDOW_SAMPLES)
        self.assertEqual(feed["h"], [[[0.0] * 128], [[0.0] * 128]])  # zero-state init
        minimal = ScriptedSession([0.5], layout="minimal")
        feed = vad.default_feed(minimal, BLOCK, 16000)
        self.assertIn("input", feed)
        self.assertEqual(feed["sr"], 16000)
        self.assertNotIn("h", feed)
        exotic = ScriptedSession([0.5], layout="none")
        feed = vad.default_feed(exotic, BLOCK, 16000)
        self.assertIn("x", feed)                                   # fallback keeps working

    def test_encoder_override_replaces_the_default_feed(self):
        captured = []

        def encoder(window):
            captured.append(window)
            return {"custom": True}

        session = ScriptedSession([0.5])
        segmenter = VadSegmenter(session, encoder=encoder, onset_windows=100)
        segmenter.feed(BLOCK)
        self.assertEqual(captured, [BLOCK])
        self.assertEqual(session.feeds, [{"custom": True}])        # bypassed introspection

    def test_window_floats_normalizes_s16le(self):
        floats = vad.window_floats(struct.pack("<2h", 32767, -32768))
        self.assertAlmostEqual(floats[0], 0.99997, places=4)
        self.assertEqual(floats[1], -1.0)

    def test_raw_span_duration_math(self):
        span = RawSpan(0, vad.WINDOW_BYTES)
        self.assertAlmostEqual(span.duration_s, vad.WINDOW_SAMPLES / 16000)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            span.end_byte = 1


class WordTrimTests(unittest.TestCase):
    """D5 midpoint trim: seam words survive EXACTLY once across units."""

    def test_midpoint_decides_inclusion_boundary_inclusive(self):
        words = [(0.0, 0.2, "a"), (0.4, 0.6, "b"), (1.4, 1.6, "c")]
        kept = vad.trim_word_timings(words, unit_offset_s=10.0,
                                     speech_start_s=10.0, speech_end_s=11.5)
        self.assertEqual(len(kept), 3)                            # 11.5 boundary IS inside
        kept = vad.trim_word_timings(words, unit_offset_s=10.0,
                                     speech_start_s=10.0, speech_end_s=11.0)
        self.assertEqual(len(kept), 2)                            # 11.5 midpoint outside

    def test_adjacent_units_keep_seam_words_exactly_once(self):
        # Production contract: each unit carries ONLY its own decode output
        # (disjoint audio -> disjoint word lists).  Trim the seam artifacts a
        # vendor overhangs across the VAD cut and assert the union of both
        # units keeps every owned word EXACTLY once (D5: no blind overlap,
        # no fabrication, nothing silently dropped).
        unit_a_words = [(0.0, 0.2, "w0"), (0.9, 1.05, "seam-a")]   # a's decode
        unit_b_words = [(0.0, 0.1, "seam-b"), (0.2, 0.4, "w2")]    # b's decode
        kept_a = vad.trim_word_timings(unit_a_words, unit_offset_s=0.0,
                                       speech_start_s=0.0, speech_end_s=1.0)
        kept_b = vad.trim_word_timings(unit_b_words, unit_offset_s=1.0,
                                       speech_start_s=1.0, speech_end_s=2.0)
        texts_a = [word[2] for word in kept_a]
        texts_b = [word[2] for word in kept_b]
        kept_texts = texts_a + texts_b
        self.assertEqual(len(kept_texts), len(set(kept_texts)))    # exactly once
        self.assertEqual(set(kept_texts), {"w0", "seam-a", "seam-b", "w2"})
        # a vendor artifact straddling BEFORE the VAD cut: session-shifted
        # midpoint outside the unit's own span -> dropped (never guessed)
        overhang = [(1.4, 1.6, "leading-noise")]                   # mid 1.5 -> unit a span?
        self.assertEqual(vad.trim_word_timings(overhang, unit_offset_s=0.0,
                                               speech_start_s=0.0,
                                               speech_end_s=1.0), ())

    def test_unparseable_entries_are_dropped_never_guessed(self):
        words = [(0.0, 0.2, "ok"), "junk", None, {"start": 5}, (0.1,)]
        kept = vad.trim_word_timings(words, unit_offset_s=0.0,
                                     speech_start_s=0.0, speech_end_s=10.0)
        self.assertEqual(kept, (words[0],))

    def test_extract_variants_attr_dict_and_callable(self):
        class W:
            def __init__(self, start, end):
                self.start, self.end = start, end
        self.assertEqual(len(vad.trim_word_timings(
            [W(0.1, 0.3)], 0.0, 0.0, 1.0)), 1)
        self.assertEqual(len(vad.trim_word_timings(
            [{"start": 0.1, "end": 0.3}], 0.0, 0.0, 1.0)), 1)
        self.assertEqual(len(vad.trim_word_timings(
            [(5.0, 6.0)], 0.0, 0.0, 1.0, extract=lambda _w: (0.1, 0.2))), 1)
        # extract returning garbage drops the word (never raises)
        self.assertEqual(vad.trim_word_timings(
            ["x"], 0.0, 0.0, 1.0, extract=lambda _w: None), ())

    def test_garbage_inputs_never_raise(self):
        self.assertEqual(vad.trim_word_timings(None, 0.0, 0.0, 1.0), ())
        self.assertEqual(vad.trim_word_timings([], 0.0, 0.0, 1.0), ())
        # unit_offset None -> treated as 0.0
        self.assertEqual(len(vad.trim_word_timings(
            [(0.1, 0.2)], None, 0.0, 1.0)), 1)


if __name__ == "__main__":
    unittest.main()
