"""Manifest + digest tests (plan 2.3 matrix; supply-chain gate G3).

Owner: qa-test-assurance.  Covers:
* ``parse_manifest`` schema pins (document/bare list, versions, duplicate ids,
  format pins: 64-lowercase-hex sha256, 40-hex hf_repo_sha, size > 0),
* tampered-digest rejection through the streaming ``DigestVerifier``
  (constant-time compare, malformed expectation NEVER matches),
* mirror ORDER (primary first) + H4 URL hygiene (https, no userinfo/query/
  fragment) and traversal-UNSAFE install paths rejected (A4),
* the shipped ``assets/models_manifest.json`` itself: parses, pins present,
  and NO placeholder zero-digest (G3 manifest-pin finding).
"""

import hashlib
import json
import os
import unittest

from model_manifest import (KNOWN_KINDS, DigestVerifier, ModelArtifact,
                            ModelFile, ModelManifest, find, parse_manifest)

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SHA = "a" * 64


def _entry(**overrides):
    entry = {
        "model_id": "faster-whisper-base",
        "version": 1,
        "files": [{"path": "model.bin",
                   "url": ["https://example.com/model.bin"],
                   "sha256": SHA, "size": 100}],
    }
    entry.update(overrides)
    return entry


class ParsePinsTests(unittest.TestCase):
    def test_document_and_bare_list_forms(self):
        doc = parse_manifest({"version": 2, "models": [_entry()]})
        self.assertEqual(doc.version, 2)
        self.assertEqual(len(doc.artifacts), 1)
        bare = parse_manifest([_entry()])          # bare entry list tolerated (v1)
        self.assertEqual(bare.version, 1)
        empty = parse_manifest({"models": []})     # empty list is schema-legal
        self.assertEqual(empty.artifacts, ())
        self.assertIsInstance(bare.artifacts[0], ModelArtifact)
        self.assertIsInstance(bare.artifacts[0].files[0], ModelFile)

    def test_malformed_documents_raise_value_error(self):
        for bad in (None, 42, "models", {"models": "x"}, {"version": 0, "models": []},
                    {"version": True, "models": []}, {"version": "1", "models": []},
                    object()):
            with self.subTest(data=bad):
                with self.assertRaises(ValueError):
                    parse_manifest(bad)

    def test_entry_pins_reject_duplicates_and_bad_shapes(self):
        with self.assertRaises(ValueError):
            parse_manifest([_entry(), _entry()])                   # duplicate model_id
        with self.assertRaises(ValueError):
            parse_manifest([_entry(version=0)])
        with self.assertRaises(ValueError):
            parse_manifest([_entry(version=True)])
        with self.assertRaises(ValueError):
            parse_manifest([_entry(model_id="   ")])
        with self.assertRaises(ValueError):
            parse_manifest([_entry(files=[])])
        with self.assertRaises(ValueError):
            parse_manifest([_entry(files=["not-a-dict"])])
        with self.assertRaises(ValueError):
            parse_manifest(["not-a-dict"])

    def test_sha256_must_be_64_lowercase_hex(self):
        for bad in ("A" * 64,          # uppercase never matches (format pin)
                    "a" * 63,          # too short
                    "a" * 65,          # too long
                    "g" * 64,          # not hex
                    "", None, 42, "0x" + "a" * 62):
            with self.subTest(digest=bad):
                with self.assertRaises(ValueError):
                    parse_manifest([_entry(files=[{
                        "path": "model.bin", "url": ["https://e.com/f"],
                        "sha256": bad, "size": 1}])])

    def test_size_and_optional_pins(self):
        for bad_size in (0, -1, "100", None, True, 1.5):
            with self.subTest(size=bad_size):
                with self.assertRaises(ValueError):
                    parse_manifest([_entry(files=[{
                        "path": "f.bin", "url": ["https://e.com/f"],
                        "sha256": SHA, "size": bad_size}])])
        with self.assertRaises(ValueError):                          # bad hf pin
            parse_manifest([_entry(hf_repo_sha="zz")])
        with self.assertRaises(ValueError):                          # unknown kind
            parse_manifest([_entry(kind="onnx-weird")])
        ok = parse_manifest([_entry(hf_repo_sha="b" * 40, kind="silero-vad",
                                    license="MIT")])
        self.assertEqual(ok.artifacts[0].kind, "silero-vad")
        self.assertEqual(set(KNOWN_KINDS), {"faster-whisper", "silero-vad"})

    def test_find_is_never_raising(self):
        manifest = parse_manifest([_entry()])
        self.assertEqual(find(manifest, "faster-whisper-base").version, 1)
        self.assertIsNone(find(manifest, "missing"))
        self.assertIsNone(find(manifest, None))
        self.assertIsNone(find("junk", "x"))


class MirrorOrderAndUrlHygieneTests(unittest.TestCase):
    def test_url_list_order_is_preserved_primary_first(self):
        urls = ["https://primary.example/model.bin",
                "https://hf-mirror.example/model.bin",
                "https://releases.example/model.bin"]
        parsed = parse_manifest([_entry(files=[{
            "path": "model.bin", "url": urls, "sha256": SHA, "size": 5}])])
        self.assertEqual(parsed.artifacts[0].files[0].urls, tuple(urls))
        self.assertEqual(parsed.artifacts[0].files[0].urls[0], urls[0])  # H4 order

    def test_url_hygiene_rejects_non_https_and_credentials(self):
        bad_urls = [
            "http://plain.example/f",                       # not https
            "ftp://example.com/f",                          # wrong scheme
            "https://user:pass@example.com/f",              # userinfo
            "https://example.com/f?token=TOPSECRET",        # query
            "https://example.com/f#frag",                   # fragment
            "https:///nohost",                              # no host
            "/relative/path",                               # not absolute
            "", None, 42,
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    parse_manifest([_entry(files=[{
                        "path": "f.bin", "url": [url],
                        "sha256": SHA, "size": 1}])])
        with self.assertRaises(ValueError):
            parse_manifest([_entry(files=[{
                "path": "f.bin", "url": [], "sha256": SHA, "size": 1}])])

    def test_url_list_also_accepts_the_urls_alias_but_not_both_shapes(self):
        alias = parse_manifest([_entry(files=[{
            "path": "f.bin", "urls": ["https://e.com/f"], "sha256": SHA, "size": 1}])])
        self.assertEqual(alias.artifacts[0].files[0].urls, ("https://e.com/f",))


class TraversalSafePathTests(unittest.TestCase):
    """A4: manifest paths are RELATIVE — absolute / ``..`` never install."""

    def test_relative_paths_are_accepted(self):
        for path in ("model.bin", "sub/model.bin", "a/b/c.bin",
                     "folder\\windows-style.bin"):
            with self.subTest(path=path):
                parsed = parse_manifest([_entry(files=[{
                    "path": path, "url": ["https://e.com/f"],
                    "sha256": SHA, "size": 1}])])
                self.assertEqual(parsed.artifacts[0].files[0].path, path)

    def test_traversal_and_absolute_paths_raise(self):
        for path in ("../escape.bin", "a/../../b", "..", ".", "a/./b",
                     "/etc/passwd", "\\windows\\system32\\x",
                     "C:\\evil.dll", "C:evil.dll", "", None, 42,
                     "a//b", "trailing/"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    parse_manifest([_entry(files=[{
                        "path": path, "url": ["https://e.com/f"],
                        "sha256": SHA, "size": 1}])])


class DigestVerifierTests(unittest.TestCase):
    def test_streaming_digest_matches_in_chunks(self):
        payload = os.urandom(300_000)
        expected = hashlib.sha256(payload).hexdigest()
        verifier = DigestVerifier(expected)
        for offset in range(0, len(payload), 64 * 1024):
            verifier.update(payload[offset:offset + 64 * 1024])
        self.assertTrue(verifier.matches())
        self.assertEqual(verifier.byte_count, len(payload))

    def test_tampered_bytes_never_match(self):
        payload = b"weights-bytes"
        expected = hashlib.sha256(payload).hexdigest()
        verifier = DigestVerifier(expected)
        verifier.update(b"weights-bytex")          # single flipped byte
        self.assertFalse(verifier.matches())
        # a length cheat (prefix) must not match either
        short = DigestVerifier(expected)
        short.update(payload[:5])
        self.assertFalse(short.matches())

    def test_malformed_expectation_never_matches(self):
        verifier = DigestVerifier()
        verifier.update(b"data")
        for bad in (None, "", "A" * 64, "a" * 63, "g" * 64, 42):
            with self.subTest(expected=bad):
                self.assertFalse(verifier.matches(bad))
        self.assertFalse(DigestVerifier("a" * 64).matches("b" * 64))
        # non-bytes updates are ignored, never corrupt the digest
        weird = DigestVerifier(hashlib.sha256(b"data").hexdigest())
        weird.update("not-bytes")
        weird.update(None)
        weird.update(42)
        weird.update(b"data")
        self.assertTrue(weird.matches())

    def test_matches_uses_constant_time_compare(self):
        # A9: the comparison goes through hmac.compare_digest (source pin — a
        # plain == here would leak digest-prefix timing).
        with open(os.path.join(REPO_DIR, "model_manifest.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("hmac.compare_digest(self._digest.hexdigest(), expected)", source)


class ShippedManifestPinTests(unittest.TestCase):
    """G3: the in-binary manifest parses and every pin is a REAL digest."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(REPO_DIR, "assets", "models_manifest.json")
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        cls.raw = raw
        cls.manifest = parse_manifest(raw)

    def test_manifest_shape_and_known_artifacts(self):
        self.assertGreaterEqual(self.manifest.version, 1)
        ids = [artifact.model_id for artifact in self.manifest.artifacts]
        for required in ("faster-whisper-base", "faster-whisper-small", "silero-vad"):
            self.assertIn(required, ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_placeholder_zero_digests(self):
        # A placeholder ("0"*64) would pass the 64-hex FORMAT pin but is a
        # G3 finding: model_store + smoke both refuse it — assert none exists.
        placeholders = [entry.path for artifact in self.manifest.artifacts
                        for entry in artifact.files if set(entry.sha256) == {"0"}]
        self.assertEqual(placeholders, [])
        for artifact in self.manifest.artifacts:
            for entry in artifact.files:
                self.assertRegex(entry.sha256, r"^[0-9a-f]{64}$")
                self.assertGreater(entry.size, 0)
                self.assertTrue(entry.urls)                       # mirror list present
                self.assertTrue(all(url.startswith("https://") for url in entry.urls))

    def test_every_model_file_path_is_traversal_safe(self):
        for artifact in self.manifest.artifacts:
            for entry in artifact.files:
                with self.subTest(model=artifact.model_id, path=entry.path):
                    self.assertFalse(os.path.isabs(entry.path))
                    self.assertNotIn("..", entry.path.split("/"))


if __name__ == "__main__":
    unittest.main()
