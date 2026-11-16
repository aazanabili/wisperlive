"""Pure contract tests for dictation_types (plan 2.3 matrix; Tier 3).

Owner: qa-test-assurance (test-only).  Covers:
* SessionConfig immutability + D11 repr-secret pins (no transcript, no keys
  in reprs; snapshot not serializable; api_keys frozen AND copied),
* SegmentIn / EmissionOut shape + emission_seq ordering keys,
* the FULL SEGMENT_STATUS legal-jump matrix (everything else raises),
* the TypedSpan registry ops (register / invalidate / latest_valid TTL /
  next_seq / validate_emission),
* build_session_config snapshot resolution + least-privilege api_keys.

Everything here is deterministic and I/O-free: the module under test is the
pure leaf of the dictation-modes feature (D10/D11/D5 contracts).
"""

import dataclasses
import unittest

import dictation_types as dt
import providers


def _segment(**overrides):
    base = dict(session_id="s1", seq=0, audio_ref="ring:0:640",
                start_ms=0, end_ms=1000)
    base.update(overrides)
    return dt.Segment(**base)


def _word(start_ms, end_ms, text):
    return dt.WordSpan(start_ms=start_ms, end_ms=end_ms, text=text)


class SessionConfigImmutabilityTests(unittest.TestCase):
    """D10: the snapshot is frozen, deeply detached, and D11 repr-hidden."""

    def _config(self, **extra):
        source = {
            "dictation_mode": "chunked", "model_size": "auto",
            "provider": "xiaomi-mimo", "provider_variant": "token-plan-ams",
            "model": "mimo-v2.6-flash", "transcription_model": "mimo-v2.5-asr",
            "chunked_engine": "", "cleanup_mode": "cloud",
            "target_language": "Arabic", "polish_mode": "insert",
            "gemini_consent": True,
        }
        source.update(extra)
        return dt.build_session_config(source, session_id="sid", started_at_epoch=10,
                                       api_keys={"groq": "gsk-SECRET"})

    def test_snapshot_is_frozen(self):
        snapshot = self._config()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.dictation_mode = "live"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.session_id = "other"

    def test_api_keys_are_frozen_proxy_and_copied_not_aliased(self):
        keys = {"groq": "gsk-SECRET"}
        snapshot = dt.build_session_config({}, session_id="s", started_at_epoch=1,
                                           api_keys=keys)
        keys["groq"] = "MUTATED-AFTER-BUILD"      # caller mutation must not alias
        self.assertEqual(snapshot.api_keys["groq"], "gsk-SECRET")
        with self.assertRaises(TypeError):
            snapshot.api_keys["openai"] = "sk-x"
        with self.assertRaises(TypeError):
            del snapshot.api_keys["groq"]

    def test_repr_never_leaks_secrets_or_text_fields(self):
        snapshot = self._config()
        rendered = repr(snapshot)
        self.assertNotIn("gsk-SECRET", rendered)
        self.assertNotIn("api_keys", rendered)    # repr=False hides the whole field
        # repr=False pins on the D11-bearing fields of the payload types
        segment = dt.SegmentIn(segment_seq=0, pcm16=b"\x01\x02" * 8,
                               speech_start_s=0.0, speech_end_s=1.0,
                               audio_seconds=1.0)
        self.assertNotIn("0102", repr(segment))
        emission = dt.EmissionOut("secret transcript")
        self.assertNotIn("secret transcript", repr(emission))
        word = _word(0, 10, "private-word")
        self.assertNotIn("private-word", repr(word))

    def test_snapshot_is_deliberately_not_serializable(self):
        snapshot = self._config()
        self.assertFalse(hasattr(snapshot, "to_dict"))
        self.assertFalse(hasattr(snapshot, "asdict"))
        with self.assertRaises((TypeError, ValueError)):
            dataclasses.asdict(snapshot)          # MappingProxyType is not deep-copyable

    def test_gemini_enabled_is_fail_closed_or(self):
        self.assertFalse(self._config(gemini_consent=False).gemini_enabled)
        self.assertTrue(self._config(gemini_consent=True).gemini_enabled)
        self.assertTrue(self._config(
            gemini_consent=False, gemini_paid_key_confirmed=True).gemini_enabled)
        # Non-literal truthy values never enable (M20 normalized upstream, N1 here)
        self.assertFalse(self._config(gemini_consent="yes").gemini_enabled)


class BuildSessionConfigTests(unittest.TestCase):
    """A3 s2.2: every field resolved ONCE from the live config (least privilege)."""

    def test_resolves_provider_facts_from_registry(self):
        snapshot = dt.build_session_config(
            {"provider": "xiaomi-mimo", "provider_variant": "token-plan-sgp",
             "provider_base_url": "https://stale.example/v1",
             "model": "mimo-v2.6-pro", "transcription_model": ""},
            session_id="sid", started_at_epoch=1700000000, api_keys={})
        self.assertEqual(snapshot.provider_id, "xiaomi-mimo")
        self.assertEqual(snapshot.provider_variant, "token-plan-sgp")
        self.assertEqual(snapshot.provider_base_url,
                         providers.resolve_base_url("xiaomi-mimo", "token-plan-sgp"))
        self.assertNotIn("stale.example", snapshot.provider_base_url)  # registry wins
        self.assertEqual(snapshot.dialect, "mimo-asr")
        self.assertEqual(snapshot.transcription_model, "mimo-v2.5-asr")  # curated default
        self.assertEqual(snapshot.post_model, "mimo-v2.6-pro")
        self.assertEqual(snapshot.started_at_epoch, 1700000000)

    def test_defaults_match_shipped_policy(self):
        snapshot = dt.build_session_config({}, session_id="s", started_at_epoch=1,
                                           api_keys={})
        self.assertEqual(snapshot.dictation_mode, "local")     # U4 fail-closed default
        self.assertEqual(snapshot.model_size, "auto")
        self.assertEqual(snapshot.chunked_engine, "custom")    # "" inherits provider
        self.assertEqual(snapshot.live_engine, "local")
        self.assertEqual(snapshot.cleanup_mode, "cloud")       # D2 default
        self.assertEqual(snapshot.polish_mode, "insert")       # U3 default
        self.assertFalse(snapshot.gemini_enabled)              # U2 default OFF

    def test_least_privilege_keys_are_exactly_what_the_session_may_call(self):
        keys = {"groq": "gsk-1"}
        snapshot = dt.build_session_config(
            {"provider": "openai"}, session_id="s", started_at_epoch=1, api_keys=keys)
        self.assertEqual(set(snapshot.api_keys), {"groq"})
        # garbage input degrades without raising (N1)
        broken = dt.build_session_config(None, session_id="g",
                                         started_at_epoch="bad", api_keys={})
        self.assertEqual(broken.dictation_mode, "local")
        self.assertEqual(broken.started_at_epoch, 0)


class ResolveChunkedEngineTests(unittest.TestCase):
    def test_matrix(self):
        cases = [
            ({"chunked_engine": "groq-whisper"}, "groq-whisper"),
            ({"chunked_engine": "  ", "provider": "groq"}, "groq"),
            ({"chunked_engine": "", "provider": "openai"}, "openai"),
            ({"chunked_engine": None, "provider": "mistral"}, "mistral"),
            ({"chunked_engine": 42, "provider": "deepseek"}, "deepseek"),
            ({"chunked_engine": "unknown-engine", "provider": "groq"}, "groq"),
            ({}, "custom"),
            (None, "custom"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(dt.resolve_chunked_engine(source), expected)

    def test_stored_preset_id_passes_byte_identical(self):
        self.assertEqual(
            dt.resolve_chunked_engine({"chunked_engine": "xiaomi-mimo",
                                       "provider": "groq"}),
            "xiaomi-mimo")


class SegmentStatusMatrixTests(unittest.TestCase):
    """A3 s2.3: the FULL legal-jump table; everything else raises ValueError."""

    LEGAL = {
        "pending": {"transcribing", "failed", "dropped"},
        "transcribing": {"confirmed", "failed", "dropped"},
        "confirmed": set(),
        "failed": set(),
        "dropped": set(),
    }

    def test_every_legal_jump_is_accepted_and_returns_a_new_segment(self):
        for status, targets in self.LEGAL.items():
            for target in targets:
                with self.subTest(f"{status}->{target}"):
                    source = _segment(status=status)
                    moved = dt.set_status(source, target)
                    self.assertEqual(moved.status, target)
                    self.assertEqual(source.status, status)     # original untouched

    def test_every_illegal_jump_raises(self):
        for status in dt.SEGMENT_STATUS:
            for target in dt.SEGMENT_STATUS:
                if target in self.LEGAL[status]:
                    continue
                with self.subTest(f"{status}->{target}"):
                    with self.assertRaises(ValueError):
                        dt.set_status(_segment(status=status), target)

    def test_unknown_status_raises(self):
        for bad in ("bogus", "", None, 42, "PENDING"):
            with self.subTest(status=bad):
                with self.assertRaises(ValueError):
                    dt.set_status(_segment(), bad)

    def test_confirmed_to_transcribing_named_example_never_happens(self):
        with self.assertRaises(ValueError):
            dt.set_status(_segment(status="confirmed"), "transcribing")

    def test_error_kind_is_taxonomy_only_and_kept_only_on_failed(self):
        good = dt.set_status(_segment(), "failed", error_kind="LLMNetworkError")
        self.assertEqual(good.error_kind, "LLMNetworkError")
        # a message body (whitespace) is rejected — D11
        for bad_kind in ("boom: message body with spaces", "two words", 42, None):
            with self.subTest(error_kind=bad_kind):
                with self.assertRaises(ValueError):
                    dt.set_status(_segment(), "failed", error_kind=bad_kind)
        # "" is the legal empty value (accepted on any legal jump)
        self.assertEqual(dt.set_status(_segment(), "failed", error_kind="").error_kind, "")
        # error_kind never sticks on a NON-failed transition
        moved = dt.set_status(_segment(), "transcribing", error_kind="LLMError")
        self.assertEqual(moved.error_kind, "")

    def test_terminal_states_never_move_at_all(self):
        for terminal in ("confirmed", "failed", "dropped"):
            for target in dt.SEGMENT_STATUS:
                with self.subTest(f"{terminal}->{target}"):
                    with self.assertRaises(ValueError):
                        dt.set_status(_segment(status=terminal), target)


class TypedSpanRegistryTests(unittest.TestCase):
    def _span(self, span_id, seq=0, now=1000, valid=True):
        return dt.TypedSpan(span_id=span_id, emission_seq=seq, char_count=10,
                            word_count=2, start=0, length=5,
                            insertion_mode="insert", created_at_epoch=now,
                            valid=valid)

    def test_register_is_newest_first_and_invalidate_older(self):
        registry = dt.register((), self._span(1))
        registry = dt.register(registry, self._span(2))
        self.assertEqual([span.span_id for span in registry], [2, 1])
        self.assertTrue(registry[0].valid)
        self.assertFalse(registry[1].valid)       # rule (a): caret moved

    def test_capacity_is_four_drop_oldest(self):
        self.assertEqual(dt.SPAN_REGISTRY_CAPACITY, 4)
        registry = ()
        for span_id in range(1, 7):
            registry = dt.register(registry, self._span(span_id))
        self.assertEqual(len(registry), 4)
        self.assertEqual([span.span_id for span in registry], [6, 5, 4, 3])

    def test_invalidate_all_and_by_id_and_no_selector(self):
        registry = dt.register(dt.register((), self._span(1)), self._span(2))
        self.assertTrue(registry[0].valid)              # newest valid...
        self.assertFalse(registry[1].valid)             # ...older already invalidated (a)
        cleared = dt.invalidate(registry, all_=True)
        self.assertTrue(all(not span.valid for span in cleared))
        self.assertTrue(registry[0].valid)              # pure: original tuple untouched
        one = dt.invalidate(registry, span_id=2)
        self.assertFalse(one[0].valid)                  # span 2 cleared
        unchanged = dt.invalidate(registry)
        self.assertEqual(unchanged, tuple(registry))    # no selector: no-op

    def test_latest_valid_respects_validity_and_ttl(self):
        registry = dt.register((), self._span(1, now=1000))
        self.assertIsNotNone(dt.latest_valid(registry, 1000 + dt.SPAN_TTL_S))
        self.assertIsNone(dt.latest_valid(registry, 1000 + dt.SPAN_TTL_S + 1))
        self.assertIsNone(dt.latest_valid(dt.invalidate(registry, all_=True), 1000))
        self.assertIsNone(dt.latest_valid((), 0))
        # newest VALID wins: a manually mixed-validity state (any producer that
        # invalidates a single span) is scanned newest-first for the first valid
        mixed = (self._span(2, valid=False), self._span(1, valid=True))
        self.assertEqual(dt.latest_valid(mixed, 1000).span_id, 1)

    def test_polish_span_alias_is_one_implementation(self):
        self.assertIs(dt.PolishSpan, dt.TypedSpan)

    def test_typed_span_carries_no_text_field(self):
        self.assertNotIn("text", {field.name for field in
                                  dataclasses.fields(dt.TypedSpan)})


class SequenceAndEmissionGateTests(unittest.TestCase):
    def test_next_seq_is_strictly_increasing_and_never_raises(self):
        self.assertEqual(dt.next_seq(-1), 0)
        self.assertEqual(dt.next_seq(0), 1)
        self.assertEqual(dt.next_seq(41), 42)
        for garbage in (None, "x", 3.5, object()):
            self.assertEqual(dt.next_seq(garbage), 0)

    def test_emission_defaults_and_kinds(self):
        emission = dt.EmissionOut("text")
        self.assertEqual(emission.segment_seq, -1)
        self.assertEqual(emission.emission_seq, -1)
        self.assertEqual(emission.kind, "phrase")
        self.assertEqual(emission.replaces_seq, -1)
        self.assertTrue(emission.is_final)
        self.assertEqual(dt.EMISSION_KINDS, ("phrase", "polish_insert", "correction"))

    def test_validate_emission_gate(self):
        registry = dt.register((), dt.TypedSpan(
            span_id=1, emission_seq=7, char_count=4, word_count=1, start=0,
            length=4, insertion_mode="insert", created_at_epoch=0, valid=True))
        self.assertTrue(dt.validate_emission(dt.EmissionOut("ok"), registry))
        self.assertTrue(dt.validate_emission(
            dt.EmissionOut("pol", kind="polish_insert"), registry))
        # correction ONLY against a VALID span covering replaces_seq
        correction = dt.EmissionOut("fixed", kind="correction", replaces_seq=7)
        self.assertTrue(dt.validate_emission(correction, registry))
        dangling = dt.EmissionOut("fixed", kind="correction", replaces_seq=99)
        self.assertFalse(dt.validate_emission(dangling, registry))
        invalid_registry = dt.invalidate(registry, all_=True)
        self.assertFalse(dt.validate_emission(correction, invalid_registry))
        # unknown kind rejected; malformed input never raises (N1)
        self.assertFalse(dt.validate_emission(dt.EmissionOut("x", kind="nope"), ()))
        self.assertFalse(dt.validate_emission(object(), ()))
        self.assertFalse(dt.validate_emission(None, None))


if __name__ == "__main__":
    unittest.main()
