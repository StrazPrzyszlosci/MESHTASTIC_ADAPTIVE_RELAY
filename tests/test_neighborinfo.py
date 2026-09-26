import unittest

from lib import neighborinfo as NI


class TestVarint(unittest.TestCase):
    def test_roundtrip_lossless(self):
        for n in (0, 1, 127, 128, 300, 16383, 16384, 1042, 4294967295):
            enc = NI.varint_encode(n)
            val, pos = NI.varint_decode(enc)
            self.assertEqual(val, n, f'varint roundtrip failed for {n}')
            self.assertEqual(pos, len(enc))

    def test_deterministic(self):
        self.assertEqual(NI.varint_encode(300), NI.varint_encode(300))
        self.assertEqual(NI.varint_encode(300), b'\xac\x02')

    def test_small_values_take_one_byte(self):
        self.assertEqual(len(NI.varint_encode(0)), 1)
        self.assertEqual(len(NI.varint_encode(127)), 1)
        self.assertEqual(len(NI.varint_encode(128)), 2)

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            NI.varint_encode(-1)


class TestQualityQuantization(unittest.TestCase):
    def test_quality_classes(self):
        self.assertEqual(NI.quality_class(None), 0)
        self.assertEqual(NI.quality_class(0.0), 1)
        self.assertEqual(NI.quality_class(0.5), 2)   # BAD: [0.40, 0.55)
        self.assertEqual(NI.quality_class(0.6), 3)   # FAIR: [0.55, 0.70)
        self.assertEqual(NI.quality_class(0.85), 5)
        self.assertEqual(NI.quality_class(0.99), 6)

    def test_freshness_classes(self):
        self.assertEqual(NI.freshness_class(0, 300000), 3)
        self.assertEqual(NI.freshness_class(100000, 300000), 2)
        self.assertEqual(NI.freshness_class(200000, 300000), 1)
        self.assertEqual(NI.freshness_class(280000, 300000), 0)

    def test_quality_byte_roundtrip(self):
        for pdr in (0.1, 0.5, 0.9):
            qb = NI.quality_byte(pdr, 50000, 300000)
            cls, fresh = NI.unpack_quality(qb)
            self.assertEqual(cls, NI.quality_class(pdr))
            self.assertEqual(fresh, NI.freshness_class(50000, 300000))
        self.assertLessEqual(NI.quality_byte(0.9, 0, 300000), 0x1F)


class TestEncoders(unittest.TestCase):
    def setUp(self):
        self.entries = [(1000, 0x0F), (1004, 0x0C), (1011, 0x08), (1020, 0x05)]

    def test_full_raw32_exact_size(self):
        payload, flags = NI.encode_full_raw32(self.entries, quality=True)
        self.assertEqual(len(payload), 4 * 4 + 4)      # 4B id + 1B quality each
        self.assertEqual(flags, NI.FLAG_QUALITY)
        payload2, _ = NI.encode_full_raw32(self.entries, quality=False)
        self.assertEqual(len(payload2), 16)            # ID only: 4B each

    def test_full_raw32_decode(self):
        payload, _ = NI.encode_full_raw32(self.entries, quality=True)
        dec = NI.decode_full_raw32(payload, quality=True)
        self.assertEqual(dec, self.entries)

    def test_full_delta_varint_shorter_than_raw(self):
        raw, _ = NI.encode_full_raw32(self.entries, quality=True)
        dv, _ = NI.encode_full_delta_varint(self.entries, quality=True)
        # first id varint(1000)=2B + deltas 4,7,9 (1B each) + 4 quality bytes
        self.assertEqual(len(dv), 9)
        self.assertLess(len(dv), len(raw))
        dec = NI.decode_full_delta_varint(dv, quality=True)
        self.assertEqual(dec, sorted(self.entries))

    def test_bitmap_encode_decode(self):
        local_map = {1000: 0, 1004: 1, 1011: 2, 1020: 3}
        bm, flags = NI.encode_full_local_bitmap(self.entries, local_map, quality=True)
        self.assertIsNotNone(bm)
        # bitmap_len(1) + bitmap(1B for 4 local ids) + 4 quality bytes
        self.assertEqual(len(bm), 6)
        dec = NI.decode_full_local_bitmap(bm, local_map, quality=True)
        self.assertEqual(sorted(dec), sorted(self.entries))

    def test_bitmap_beats_raw_for_dense_local_ids(self):
        local_map = {i: i for i in range(1000, 116)}  # 8 local ids... build dense
        local_map = {1000 + i: i for i in range(8)}
        entries = [(1000 + i, 0x0F) for i in range(8)]
        raw, _ = NI.encode_full_raw32(entries, quality=True)          # 40B
        bm, _ = NI.encode_full_local_bitmap(entries, local_map, quality=True)
        self.assertEqual(len(bm), 1 + 1 + 8)                          # 10B
        self.assertLess(len(bm), len(raw))

    def test_bitmap_requires_complete_dictionary(self):
        local_map = {1000: 0}   # incomplete: 1004/1011/1020 unknown
        bm, _ = NI.encode_full_local_bitmap(self.entries, local_map, quality=True)
        self.assertIsNone(bm, 'bitmap without complete dictionary must be refused')

    def test_auto_picks_shortest(self):
        local_map = {1000 + i: i for i in range(8)}
        entries = [(1000 + i, 0x0F) for i in range(8)]
        name, payload, flags = NI.choose_best_neighbor_encoding(entries, local_map, quality=True)
        self.assertEqual(name, 'local_bitmap')
        # without a dictionary: raw or delta_varint
        name2, payload2, _ = NI.choose_best_neighbor_encoding(self.entries, {}, quality=True)
        self.assertIn(name2, ('raw32', 'delta_varint'))

    def test_delta_ops_encoding(self):
        ops = [(NI.NI_OP_ADD, 1042, 0x0F), (NI.NI_OP_REMOVE, 873, None),
               (NI.NI_OP_QUALITY, 921, 0x08)]
        payload, flags = NI.encode_delta(ops)
        self.assertEqual(flags, NI.FLAG_HAS_OPS)
        # ADD: 1+varint(1042=2B)+1 = 4; REMOVE: 1+varint(873=2B) = 3; QUALITY: 4
        self.assertEqual(len(payload), 4 + 3 + 4)


class TestSummary(unittest.TestCase):
    def test_summary_few_bytes(self):
        payload, flags = NI.encode_summary(7, 3, 2, 1, 2)
        self.assertEqual(len(payload), 3)
        self.assertEqual(flags, 0)
        # redundancy/unique/confidence packed in byte 3
        packed = payload[2]
        self.assertEqual((packed >> 5) & 0x03, 2)   # redundancy HIGH
        self.assertEqual((packed >> 4) & 0x01, 1)   # unique path YES
        self.assertEqual((packed >> 2) & 0x03, 2)   # confidence MED


class TestDeltaReconstruction(unittest.TestCase):
    def test_full_then_delta(self):
        """FULL: A B C D; DELTA: -B +E  =>  A C D E (semantics of the update)."""
        full = [(1, 0x0F), (2, 0x0F), (3, 0x0F), (4, 0x0F)]
        table = {nid: qb for nid, qb in full}
        ops = [(NI.NI_OP_REMOVE, 2, None), (NI.NI_OP_ADD, 5, 0x0F)]
        payload, _ = NI.encode_delta(ops)
        # REMOVE: 1 + varint(2)=1B = 2; ADD: 1 + varint(5)=1B + quality = 3
        self.assertEqual(len(payload), 2 + 3)
        # apply ops to the table (receiver-side reconstruction)
        pos = 0
        while pos < len(payload):
            optype = payload[pos]; pos += 1
            nid, pos = NI.varint_decode(payload, pos)
            if optype == NI.NI_OP_ADD:
                qb = payload[pos]; pos += 1
                table[nid] = qb
            elif optype == NI.NI_OP_REMOVE:
                table.pop(nid, None)
            elif optype == NI.NI_OP_QUALITY:
                qb = payload[pos]; pos += 1
                table[nid] = qb
        self.assertEqual(set(table.keys()), {1, 3, 4, 5})


if __name__ == '__main__':
    unittest.main()
