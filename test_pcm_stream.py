"""PCM plumbing tests (plan 2.3): bounded block feed + byte-bounded ring (D10/D5).

Owner: qa-test-assurance.  Covers BlockQueue's drop-NEWEST non-blocking offer
with counted loss, the EOS drain semantics, PcmRing's 32 MiB ceiling with
drop-OLDEST + counted overflow, absolute-range reads (engine-owned copies),
and the in-memory WAV conversion shared by local/HTTP transports.
"""

import io
import struct
import unittest
import wave

import pcm_stream


class ConstantPinTests(unittest.TestCase):
    def test_hard_bounds_match_the_shipped_contract(self):
        self.assertEqual(pcm_stream.SAMPLE_RATE, 16000)
        self.assertEqual(pcm_stream.CHANNELS, 1)
        self.assertEqual(pcm_stream.BLOCK_MS, 20)
        self.assertEqual(pcm_stream.BLOCK_BYTES, 640)             # 320 s16le samples
        self.assertEqual(pcm_stream.MAX_PCM_RING_BYTES, 32 * 1024 * 1024)  # D10 ceiling
        self.assertEqual(pcm_stream.BLOCK_QUEUE_MAX, 256)         # ~5 s feed bound
        self.assertEqual(pcm_stream.BLOCK_QUEUE_MAX * pcm_stream.BLOCK_BYTES,
                         163840)


class BlockQueueTests(unittest.TestCase):
    def test_offer_never_blocks_and_drops_newest_with_count(self):
        queue = pcm_stream.BlockQueue(maxsize=2)
        self.assertTrue(queue.offer(b"block-0"))
        self.assertTrue(queue.offer(b"block-1"))
        self.assertFalse(queue.offer(b"block-2"))                 # drop-NEWEST...
        self.assertFalse(queue.offer(b"block-3"))
        self.assertEqual(queue.dropped_blocks, 2)                 # ...COUNTED (never silent)
        self.assertEqual(queue.take(timeout=0.01), b"block-0")    # oldest survives

    def test_offer_rejects_non_bytes_payloads(self):
        queue = pcm_stream.BlockQueue(maxsize=4)
        self.assertFalse(queue.offer("text-block"))
        self.assertFalse(queue.offer(42))
        self.assertFalse(queue.offer(None))
        self.assertEqual(queue.dropped_blocks, 0)                 # invalid input isn't a drop
        # bytearray/memoryview are accepted as bytes
        self.assertTrue(queue.offer(bytearray(b"ab")))
        self.assertTrue(queue.offer(memoryview(b"cd")))
        self.assertEqual(queue.take(timeout=0.01), b"ab")

    def test_close_drains_tail_then_reports_end_of_stream(self):
        queue = pcm_stream.BlockQueue(maxsize=4)
        queue.offer(b"first")
        queue.close()
        self.assertTrue(queue.closed)
        self.assertEqual(queue.take(timeout=0.01), b"first")      # queued tail first
        self.assertIs(queue.take(timeout=0.01), pcm_stream.END_OF_STREAM)
        self.assertIs(queue.take(timeout=0.01), pcm_stream.END_OF_STREAM)  # sticky

    def test_timeout_reports_none_while_open(self):
        queue = pcm_stream.BlockQueue(maxsize=4)
        self.assertIsNone(queue.take(timeout=0.01))               # open + empty -> None
        queue.offer(b"x")
        queue.close()                                            # closed: EOS after the tail
        self.assertEqual(queue.take(timeout=0.01), b"x")
        self.assertIs(queue.take(timeout=0.01), pcm_stream.END_OF_STREAM)

    def test_close_on_full_queue_keeps_dropping_not_blocking(self):
        queue = pcm_stream.BlockQueue(maxsize=1)
        queue.offer(b"one")
        queue.close()                                            # EOS put fails: queue full
        self.assertEqual(queue.take(timeout=0.01), b"one")
        self.assertIs(queue.take(timeout=0.01), pcm_stream.END_OF_STREAM)

    def test_maxsize_floor_is_one(self):
        queue = pcm_stream.BlockQueue(maxsize=0)
        self.assertTrue(queue.offer(b"a"))
        self.assertFalse(queue.offer(b"b"))


class PcmRingTests(unittest.TestCase):
    def test_byte_ceiling_evicts_oldest_with_counted_overflow(self):
        ring = pcm_stream.PcmRing(max_bytes=1000)
        for index in range(10):
            ring.append(bytes([index]) * 500)
        self.assertTrue(ring.overflowed)
        self.assertGreater(ring.dropped_bytes, 0)
        self.assertLessEqual(ring.retained_bytes, 1000)
        self.assertEqual(ring.write_offset, 5000)                 # absolute write cursor
        self.assertEqual(ring.read_range(4500, 5000), bytes([9]) * 500)
        evicted = ring.read_range(0, 500)                         # dropped region -> partial
        self.assertLess(len(evicted), 500)

    def test_overflow_flag_is_false_below_the_ceiling(self):
        ring = pcm_stream.PcmRing(max_bytes=1024)
        ring.append(b"\x01" * 1024)
        self.assertFalse(ring.overflowed)
        self.assertEqual(ring.dropped_bytes, 0)
        self.assertEqual(ring.retained_bytes, 1024)

    def test_read_range_returns_a_copy_at_exact_absolute_offsets(self):
        ring = pcm_stream.PcmRing(max_bytes=4096)
        ring.append(b"AAAA")
        ring.append(b"BBBB")
        self.assertEqual(ring.read_range(0, 4), b"AAAA")
        self.assertEqual(ring.read_range(4, 8), b"BBBB")
        self.assertEqual(ring.read_range(2, 6), b"AABB")
        self.assertEqual(ring.read_range(0, 8), b"AAAABBBB")
        self.assertEqual(ring.read_range(6, 6), b"")
        self.assertEqual(ring.read_range(6, 0), b"")              # inverted -> empty
        self.assertEqual(ring.read_range(0, 999), b"AAAABBBB")    # past end clamps
        # reads are stable copies (repeat read yields identical bytes)
        self.assertEqual(ring.read_range(1, 3), ring.read_range(1, 3))

    def test_empty_append_is_a_noop_and_clear_keeps_offsets(self):
        ring = pcm_stream.PcmRing(max_bytes=100)
        ring.append(b"")
        self.assertEqual(ring.write_offset, 0)
        ring.append(b"data")
        ring.clear()
        self.assertEqual(ring.retained_bytes, 0)
        self.assertEqual(ring.write_offset, 4)                    # session-scoped offsets survive
        self.assertEqual(ring.read_range(0, 4), b"")              # nothing retained

    def test_overflowed_region_reads_only_the_surviving_part(self):
        ring = pcm_stream.PcmRing(max_bytes=10)
        ring.append(b"0123456789")            # exactly at the ceiling
        ring.append(b"ABCDEF")                # evicts 0123456789 partially
        self.assertTrue(ring.overflowed)
        data = ring.read_range(0, 16)
        self.assertTrue(data.endswith(b"ABCDEF"))
        self.assertEqual(len(data), ring.retained_bytes)

    def test_maxsize_floor_is_one_byte(self):
        ring = pcm_stream.PcmRing(max_bytes=0)
        ring.append(b"xy")
        self.assertLessEqual(ring.retained_bytes, 1)
        self.assertTrue(ring.overflowed)


class WavBytesTests(unittest.TestCase):
    def test_wav_container_shape_matches_engine_expectations(self):
        payload = bytes(range(256)) * 4
        wav = pcm_stream.pcm16_to_wav_bytes(payload)
        with wave.open(io.BytesIO(wav), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getframerate(), 16000)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getnframes(), len(payload) // 2)
            self.assertEqual(handle.readframes(handle.getnframes()), payload)

    def test_custom_sample_rate_is_honored(self):
        wav = pcm_stream.pcm16_to_wav_bytes(b"\x00\x00" * 160, sample_rate=8000)
        with wave.open(io.BytesIO(wav), "rb") as handle:
            self.assertEqual(handle.getframerate(), 8000)

    def test_empty_pcm_produces_a_valid_header_only_wav(self):
        wav = pcm_stream.pcm16_to_wav_bytes(b"")
        with wave.open(io.BytesIO(wav), "rb") as handle:
            self.assertEqual(handle.getnframes(), 0)

    def test_bytes_are_identical_to_a_real_s16le_block(self):
        block = struct.pack("<320h", *([1000] * 320))             # one 20 ms block
        self.assertEqual(len(block), pcm_stream.BLOCK_BYTES)
        wav = pcm_stream.pcm16_to_wav_bytes(block)
        with wave.open(io.BytesIO(wav), "rb") as handle:
            self.assertEqual(handle.getnframes(), 320)


if __name__ == "__main__":
    unittest.main()
