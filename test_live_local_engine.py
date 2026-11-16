"""LIVE local engine tests (plan 2.3; D12 + U6 confirmed-only emission).

Owner: qa-test-assurance.  The decoder is an INJECTED seam (the dev venv has
no faster_whisper): a fake decoder drives the ``SegmentIn -> EmissionOut``
contract deterministically while the real LA-2 confirmation logic, tail scroll,
adaptive cadence, and CPU cap mapping run unmodified.

Covers:
* LA-2: a hypothesis that FLIPS between decodes is never emitted — only the
  word-level longest common prefix of TWO consecutive hypotheses types (U6);
* ``adaptive_interval`` clamps (0.5..3.0 s, honest None while unmeasured);
* flush -> ONE final emission of the tail (is_final=True), bounded;
* D12 CPU cap: live resolves ONLY to base/small behind the capability probe;
* bounded tail cap + empty-hypothesis trim (never invent text).
"""

import threading
import time
import unittest
from unittest import mock

import dictation_types as dt
import live_local_engine
from live_local_engine import LiveLocalEngine


def _words(*pairs):
    """((text, start_ms, end_ms), ...) -> WordSpan tuple."""
    return tuple(dt.WordSpan(start_ms=start, end_ms=end, text=text)
                 for text, start, end in pairs)


class FakeDecoder:
    """Replays a scripted list of word-hypotheses (or raises)."""

    def __init__(self, hypotheses=(), rtf=None):
        self.hypotheses = list(hypotheses)
        self.rtf = rtf
        self.started_with = []
        self.processed = []
        self.closed = 0

    def start(self, config):
        self.started_with.append(config)

    def process(self, segment):
        self.processed.append(segment)
        if isinstance(self.hypotheses, Exception):
            raise self.hypotheses
        emission = self.hypotheses.pop(0) if self.hypotheses else None
        if emission is None:
            return []
        text, words = emission
        return [dt.EmissionOut(text, segment_seq=segment.segment_seq,
                               words=words)]

    def estimated_rtf(self):
        return self.rtf

    def close(self):
        self.closed += 1


def make_engine(decoder=None, *, rtf=None):
    decoder = decoder if decoder is not None else FakeDecoder(rtf=rtf)
    engine = LiveLocalEngine(decoder=decoder)
    emitted = []
    engine._emit = emitted.append          # caller-owned callback (no thread yet)
    engine._running = True
    return engine, decoder, emitted


def advance(engine, hyp, *, confirm_all=False, tail_bytes=16000 * 2):
    """Drive one LA-2 step exactly as the decode thread would (lock held)."""
    with engine._cond:
        if tail_bytes is not None:
            engine._tail = bytearray(b"\x01" * tail_bytes)
        engine._advance(hyp, confirm_all=confirm_all)


class LocalAgreementTests(unittest.TestCase):
    def test_flipping_hypothesis_is_never_emitted(self):
        # LA-2: h1 -> no confirm; h2 agrees on w1 -> "w1" confirmed;
        # h3 FLIPS to a different word -> NOTHING typed; h4 confirms the new
        # branch -> only the new branch's prefix types.  (U6: no partial rewrite)
        engine, _decoder, emitted = make_engine()
        advance(engine, _words(("Hello", 0, 400)))
        advance(engine, _words(("Hello", 0, 400), ("world", 450, 900)))
        advance(engine, _words(("Bye", 0, 300)))              # FLIP
        advance(engine, _words(("Bye", 0, 300), ("there", 350, 800)))
        self.assertEqual([e.text for e in emitted], ["Hello", "Bye"])
        self.assertTrue(all(e.is_final is False for e in emitted))  # confirmations
        self.assertNotIn("world", [e.text for e in emitted])  # diverging word never typed

    def test_lcp_is_whitespace_and_case_insensitive(self):
        from live_local_engine import _lcp
        prev = _words(("  Hello ", 0, 400))
        hyp = _words(("hello", 0, 400), ("WORLD", 450, 900))
        self.assertEqual(_lcp(prev, hyp), hyp[:1])        # "  Hello " == "hello"
        self.assertEqual(_lcp(prev, _words(("HELLO", 0, 400))), _words(
            ("HELLO", 0, 400)))                           # casefolded agreement
        self.assertEqual(_lcp(prev, _words(("goodbye", 0, 400))), ())  # no agreement

    def test_partial_agreement_emits_only_the_confirmed_prefix(self):
        engine, _d, emitted = make_engine()
        advance(engine, _words(("a", 0, 100), ("b", 100, 200), ("c", 200, 300)))
        advance(engine, _words(("a", 0, 100), ("b", 100, 200), ("X", 200, 300)))
        self.assertEqual([e.text for e in emitted], ["a b"])   # "c"/"X" unresolved

    def test_non_confirming_decode_replaces_prev_immediately(self):
        # comparing over the SAME tail: a non-confirming step must not leave a
        # stale, non-adjacent hypothesis to confirm against later.
        engine, _d, emitted = make_engine()
        advance(engine, _words(("one", 0, 300)))
        advance(engine, _words(("two", 0, 300)))              # flip: no confirm
        advance(engine, _words(("two", 0, 300), ("words", 350, 700)))
        self.assertEqual([e.text for e in emitted], ["two"])

    def test_empty_hypothesis_emits_nothing(self):
        engine, _d, emitted = make_engine()
        advance(engine, ())
        advance(engine, ())
        self.assertEqual(emitted, [])

    def test_emission_carries_the_confirmed_words_and_raw_text(self):
        engine, _d, emitted = make_engine()
        words = _words(("first", 0, 300))
        advance(engine, words)
        advance(engine, _words(("first", 0, 300), ("second", 350, 700)))
        emission = emitted[0]
        self.assertEqual(emission.kind, "phrase")
        self.assertEqual(emission.segment_seq, -1)            # live marker
        self.assertEqual([w.text for w in emission.words], ["first"])


class TailScrollTests(unittest.TestCase):
    def test_confirmed_words_scroll_the_tail_precisely(self):
        engine, _d, emitted = make_engine()
        advance(engine, _words(("alpha", 0, 500)), tail_bytes=32000)
        advance(engine, _words(("alpha", 0, 500), ("beta", 550, 1000)),
                tail_bytes=32000)
        # 500 ms confirmed -> 16000 bytes dropped from the 32000-byte tail
        # (word-precise scroll: audio drops WITH the words, no blind overlap)
        self.assertEqual(len(engine._tail), 32000 - 16000)
        self.assertEqual([e.text for e in emitted], ["alpha"])

    def test_tail_cap_forces_confirm_of_the_whole_hypothesis(self):
        engine, _d, emitted = make_engine()
        cap_tail = int(live_local_engine.MAX_TAIL_S * live_local_engine.BYTES_PER_S)
        # nothing agreed yet, but the 30 s cap forces the current hypothesis out
        with engine._cond:
            engine._tail = bytearray(b"\x01" * cap_tail)
            engine._advance(_words(("overflow", 0, 300)), confirm_all=False)
        self.assertEqual([e.text for e in emitted], ["overflow"])

    def test_cap_with_an_empty_hypothesis_trims_half_and_invents_nothing(self):
        engine, _d, emitted = make_engine()
        cap_tail = int(live_local_engine.MAX_TAIL_S * live_local_engine.BYTES_PER_S)
        with engine._cond:
            engine._tail = bytearray(b"\x01" * cap_tail)
            engine._advance((), confirm_all=False)
        self.assertEqual(emitted, [])
        self.assertEqual(len(engine._tail), cap_tail // 2)    # bounded memory (D5)


class AdaptiveIntervalTests(unittest.TestCase):
    def test_formula_and_clamps(self):
        from live_local_engine import adaptive_interval
        self.assertIsNone(adaptive_interval(5.0, None))       # honest unknown
        self.assertIsNone(adaptive_interval(5.0, 0))
        self.assertIsNone(adaptive_interval(5.0, -1))
        self.assertIsNone(adaptive_interval(0.0, 1.0))        # no tail -> no cost
        self.assertEqual(adaptive_interval(1.0, 1.0), 2.0)    # cost/0.5
        self.assertEqual(adaptive_interval(2.0, 0.5), 2.0)
        self.assertEqual(adaptive_interval(0.1, 0.5), 0.5)    # clamped to MIN
        self.assertEqual(adaptive_interval(10.0, 1.0), 3.0)   # clamped to MAX
        self.assertEqual(live_local_engine.MIN_INTERVAL, 0.5)
        self.assertEqual(live_local_engine.MAX_INTERVAL, 3.0)

    def test_interval_is_reevaluated_after_every_decode(self):
        decoder = FakeDecoder(rtf=2.0)                        # expensive decode
        engine, _d, _e = make_engine(decoder=decoder)
        engine._interval = live_local_engine.DEFAULT_INTERVAL
        advance(engine, ())                                   # decode happened
        # tail 1s * rtf 2.0 / 0.5 = 4.0 -> clamped to 3.0
        self.assertEqual(engine.decode_interval, 3.0)
        decoder.rtf = 0.1                                     # cheap decode
        advance(engine, (), tail_bytes=16000)                 # 1 s tail
        self.assertAlmostEqual(engine.decode_interval, 0.5, places=6)  # clamped MIN

    def test_unmeasured_rtf_keeps_the_current_cadence(self):
        decoder = FakeDecoder(rtf=None)
        engine, _d, _e = make_engine(decoder=decoder)
        engine._interval = 1.7
        advance(engine, ())
        self.assertEqual(engine.decode_interval, 1.7)         # no fabricated number


class CpuCapMappingTests(unittest.TestCase):
    def test_live_resolves_only_to_base_or_small(self):
        from live_local_engine import live_model_id
        with mock.patch.object(live_local_engine, "capability_probe",
                               return_value=(True, "")):
            self.assertEqual(live_model_id("standard"),
                             "faster-whisper-small")
            self.assertEqual(live_model_id("auto"), "faster-whisper-small")
        with mock.patch.object(live_local_engine, "capability_probe",
                               return_value=(False, "no int8")):
            self.assertEqual(live_model_id("standard"),
                             "faster-whisper-base")
            self.assertEqual(live_model_id("auto"),
                             "faster-whisper-base")

    def test_fast_skips_the_probe_entirely(self):
        from live_local_engine import live_model_id
        with mock.patch.object(live_local_engine, "capability_probe",
                               side_effect=AssertionError("must not probe")):
            self.assertEqual(live_model_id("fast"),
                             live_local_engine.BASE_MODEL_ID)

    def test_unknown_sizes_coerce_to_auto_and_tiers_stay_in_the_cap(self):
        from live_local_engine import live_model_id, _TIER_OF
        with mock.patch.object(live_local_engine, "capability_probe",
                               return_value=(True, "")):
            self.assertEqual(live_model_id("turbo"), "faster-whisper-small")
            self.assertEqual(live_model_id(None), "faster-whisper-small")
        # turbo/large tiers are NOT in the live tier map (D12: never resolve)
        self.assertNotIn("turbo", _TIER_OF)
        self.assertEqual(set(_TIER_OF.values()), {"fast", "standard"})

    def test_start_passes_a_capped_tier_to_the_decoder(self):
        decoder = FakeDecoder()
        engine = LiveLocalEngine(decoder=decoder)
        config = dt.build_session_config(
            {"model_size": "standard"}, session_id="s", started_at_epoch=1,
            api_keys={})
        with mock.patch.object(live_local_engine, "capability_probe",
                               return_value=(False, "no int8")), \
                mock.patch.object(live_local_engine, "threading") as threads:
            threads.Thread.return_value = mock.MagicMock()
            engine.start(config, lambda _e: None)
        started_config = decoder.started_with[0]
        self.assertEqual(started_config.model_size, "fast")  # small DOWNGRADED (D12)
        self.assertEqual(live_local_engine._TIER_OF[
            live_local_engine.BASE_MODEL_ID], "fast")
        engine._running = False                               # fake thread never ran

    def test_start_fails_closed_before_audio_when_the_decoder_rejects(self):
        decoder = FakeDecoder()
        decoder.start = mock.Mock(side_effect=RuntimeError("extras missing"))
        engine = LiveLocalEngine(decoder=decoder)
        config = dt.build_session_config({"model_size": "fast"},
                                         session_id="s", started_at_epoch=1,
                                         api_keys={})
        with self.assertRaises(RuntimeError):
            engine.start(config, lambda _e: None)            # typed by the seam
        self.assertFalse(engine._running)
        self.assertIsNone(engine._thread)


class DecodeSeamTests(unittest.TestCase):
    def test_process_receives_a_live_segment_in_memory_only(self):
        decoder = FakeDecoder()
        engine, _d, _e = make_engine(decoder=decoder)
        engine._decode(b"\x01\x00" * 16000)                  # 1 s of audio
        segment = decoder.processed[0]
        self.assertEqual(segment.segment_seq, -1)
        self.assertEqual(segment.pcm16, b"\x01\x00" * 16000)
        self.assertEqual(segment.audio_seconds, 1.0)
        self.assertEqual(segment.speech_end_s, 1.0)

    def test_empty_snapshot_or_missing_decoder_returns_no_words(self):
        engine, _d, _e = make_engine()
        self.assertEqual(engine._decode(b""), ())
        engine._decoder = None
        self.assertEqual(engine._decode(b"\x01" * 640), ())

    def test_decode_failure_logs_and_keeps_the_tail_bounded(self):
        exploding = FakeDecoder()
        exploding.hypotheses = RuntimeError("model exploded")
        engine, _d, emitted = make_engine(decoder=exploding)
        with mock.patch.object(live_local_engine, "logger") as logger:
            self.assertEqual(engine._decode(b"\x01" * 3200), ())
            logger.exception.assert_called_once()
        self.assertEqual(emitted, [])                         # nothing invented

    def test_emission_words_are_required_for_confirmation(self):
        # a text-only emission (no timings) never fabricates a scroll
        decoder = FakeDecoder()
        decoder.hypotheses = [("no timings", None)]
        engine, _d, _e = make_engine(decoder=decoder)
        self.assertEqual(engine._decode(b"\x01" * 3200), ())


class FlushAndCloseTests(unittest.TestCase):
    def test_threaded_flush_delivers_exactly_one_final_before_returning(self):
        # Real decode thread: push audio -> flush() blocks until the tail was
        # decoded and the FINAL emission delivered (A6 s3c — the session can
        # never finish ahead of its last phrase).
        decoder = FakeDecoder(hypotheses=[
            ("hello", _words(("hello", 0, 600)))])
        engine = LiveLocalEngine(decoder=decoder)
        emitted = []
        config = dt.build_session_config({"model_size": "fast"},
                                         session_id="s", started_at_epoch=1,
                                         api_keys={})
        engine.start(config, emitted.append)
        try:
            engine.push_audio(b"\x01\x00" * 16000)          # 1 s of speech
            engine.flush()                                  # bounded wait
            self.assertEqual([e.text for e in emitted], ["hello"])
            self.assertTrue(emitted[0].is_final)
            self.assertEqual(len(emitted), 1)               # ONE final, no dupes
        finally:
            engine.close()

    def test_flush_returns_immediately_when_not_running(self):
        engine = LiveLocalEngine(decoder=FakeDecoder())
        started = time.monotonic()
        engine.flush()                                       # not running -> no-op
        self.assertLess(time.monotonic() - started, 0.5)

    def test_close_joins_the_thread_and_closes_the_decoder(self):
        decoder = FakeDecoder()
        engine, _d, _e = make_engine(decoder=decoder)
        thread = threading.Thread(target=lambda: None, daemon=True)
        # simulate a live thread that is already finished
        engine._thread = thread
        thread.start()
        engine.close()
        engine.close()                                       # repeat-safe (no raise)
        self.assertGreaterEqual(decoder.closed, 1)           # decoder released at least once
        self.assertFalse(engine._running)
        self.assertIsNone(engine._thread)
        self.assertIsNone(engine._emit)

    def test_close_never_raises_when_the_decoder_breaks(self):
        decoder = FakeDecoder()
        decoder.close = mock.Mock(side_effect=RuntimeError("boom"))
        engine, _d, _e = make_engine(decoder=decoder)
        with mock.patch.object(live_local_engine, "logger") as logger:
            engine.close()
            logger.warning.assert_called()

    def test_emit_callback_failure_never_kills_the_engine(self):
        engine, _d, _e = make_engine()
        engine._emit = mock.Mock(side_effect=RuntimeError("subscriber bug"))
        with mock.patch.object(live_local_engine, "logger") as logger:
            advance(engine, _words(("x", 0, 100)))
            advance(engine, _words(("x", 0, 100), ("y", 150, 300)))
            logger.warning.assert_called()                   # logged, not raised
        self.assertIsNotNone(engine._emit)                   # engine still alive


if __name__ == "__main__":
    unittest.main()
