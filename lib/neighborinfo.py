#!/usr/bin/env python3
"""Binary NEIGHBORINFO codec for ADAPTIVE_RELAY (v0.4).

Pure, deterministic, lossless codec functions — no simulator state, no oracle.
Designed for real LoRa firmware: explicit byte counts, quantized quality,
varint / bitmap / raw encodings, FULL / DELTA / SUMMARY message types.

Binary formats (all sizes in bytes; documented for the report):
  HEADER (all types): type(1) | version(2, LE) | base_version(2, LE) |
                      flags(1: bit0=quality_present, bit1=delta_has_ops)
  FULL + RAW32:       per entry: id(4, LE) + [quality(1)]
  FULL + DELTA_VARINT:id varint (first = full id), then sorted deltas varint;
                      per entry: + [quality(1)]  (order = id order)
  FULL + LOCAL_BITMAP:local-map present: bitmap over local short ids (0..255),
                      ceil(n/8) bytes; quality bytes in local-id order
  DELTA:              per op: optype(1: 0=ADD, 1=REMOVE, 2=QUALITY) +
                      id varint + [quality(1)] (ADD/QUALITY only)
  SUMMARY (L1):       neighbor_count(1) | segment_count(1) |
                      packed(1: redundancy 2b << 5 | unique 1b << 4 |
                             confidence 2b << 2 | reserved 2b)

Quality byte (when quality present): 3b class << 2 | 2b freshness
  class: 0 UNKNOWN, 1 VERY_BAD, 2 BAD, 3 FAIR, 4 GOOD, 5 VERY_GOOD,
         6 EXCELLENT, 7 RESERVED
  freshness: 0 stale, 1 low, 2 medium, 3 high
"""
import math

NI_TYPE_FULL = 1
NI_TYPE_DELTA = 2
NI_TYPE_SUMMARY = 3

NI_OP_ADD = 0
NI_OP_REMOVE = 1
NI_OP_QUALITY = 2

QUAL_CLASSES = (0.0, 0.40, 0.55, 0.70, 0.80, 0.90, 0.97, 1.01)  # upper bounds

HEADER_LEN = 6  # type(1) + version(2) + base_version(2) + flags(1)

FLAG_QUALITY = 0x01
FLAG_HAS_OPS = 0x02


# ----------------------------------------------------------------------
# Trickle timer (RFC 6206 simplified) — gates control-plane TX
# ----------------------------------------------------------------------
class TrickleTimer:
    """Exponential doubling when the network is stable, reset on
    inconsistency (topology change / new node). Used to keep AR control
    traffic (NEIGHBORINFO etc.) from flooding the channel."""
    def __init__(self, min_interval=1000, max_interval=60000, stable_ticks=5):
        self.min = min_interval
        self.max = max_interval
        self.interval = min_interval
        self.counter = 0
        self.stable_ticks = stable_ticks

    def on_tick(self):
        """Called on a quiet check: when stable for a while, double the
        interval (exponentially, capped at max)."""
        self.counter += 1
        if self.counter > self.stable_ticks:
            self.interval = min(self.interval * 2, self.max)
            self.counter = 0

    def on_inconsistency(self):
        """Topology changed / new node / stale version heard: reset."""
        self.interval = self.min
        self.counter = 0


# ----------------------------------------------------------------------
# varint (unsigned LEB128) — deterministic, lossless
# ----------------------------------------------------------------------
def varint_encode(n):
    n = int(n)
    if n < 0:
        raise ValueError('varint is unsigned')
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def varint_decode(buf, pos=0):
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


# ----------------------------------------------------------------------
# quality quantization (3b class + 2b freshness -> 1 byte)
# ----------------------------------------------------------------------
def quality_class(pdr):
    """Map PDR 0..1 to a 3-bit quality class (0=UNKNOWN .. 6=EXCELLENT)."""
    if pdr is None:
        return 0
    for c in range(1, 7):
        if pdr < QUAL_CLASSES[c]:
            return c
    return 6


def freshness_class(age_ms, expiry_ms):
    """Map information age to 2-bit freshness (0 stale .. 3 high)."""
    if expiry_ms <= 0:
        return 0
    frac = age_ms / expiry_ms
    if frac < 0.15:
        return 3
    if frac < 0.35:
        return 2
    if frac < 0.7:
        return 1
    return 0


def quality_byte(pdr, age_ms, expiry_ms):
    """Pack 3b quality class + 2b freshness into one byte."""
    return ((quality_class(pdr) & 0x07) << 2) | (freshness_class(age_ms, expiry_ms) & 0x03)


def unpack_quality(qb):
    """(class, freshness) from a quality byte."""
    return ((qb >> 2) & 0x07), (qb & 0x03)


# ----------------------------------------------------------------------
# encoders — entries: [(nid, quality_byte_or_None)]
# ----------------------------------------------------------------------
def encode_full_raw32(entries, quality=True):
    """FULL snapshot, raw 4-byte IDs. Baseline encoding."""
    out = bytearray()
    flags = FLAG_QUALITY if quality else 0
    for nid, qb in entries:
        out += int(nid).to_bytes(4, 'little')
        if quality:
            out.append(qb if qb is not None else 0)
    return bytes(out), flags


def encode_full_delta_varint(entries, quality=True):
    """FULL snapshot, sorted IDs + varint deltas."""
    out = bytearray()
    flags = FLAG_QUALITY if quality else 0
    prev = 0
    for nid, qb in sorted(entries, key=lambda e: e[0]):
        out += varint_encode(int(nid) - prev)
        prev = int(nid)
        if quality:
            out.append(qb if qb is not None else 0)
    return bytes(out), flags


def encode_full_local_bitmap(entries, local_map, quality=True):
    """FULL snapshot over a stable LOCAL short-id dictionary (0..255).
    Payload: bitmap_len(1) | bitmap (ceil(max_local/8) bytes) | [quality(1)
    per present local id, ascending]. Bitmap only when the shared dictionary
    is complete (`local_map` from really exchanged FULL snapshots)."""
    ids = [nid for nid, _ in entries]
    unknown = [nid for nid in ids if nid not in local_map]
    if unknown or not local_map:
        return None, 0  # cannot use bitmap without a complete shared dictionary
    max_local = max(local_map[nid] for nid in ids)
    n_bytes = (max_local // 8) + 1
    bitmap = bytearray(n_bytes)
    for nid in ids:
        li = local_map[nid]
        bitmap[li // 8] |= (1 << (li % 8))
    out = bytearray([n_bytes & 0xFF])
    out += bitmap
    if quality:
        q_by_local = {local_map[nid]: qb for nid, qb in entries}
        for li in sorted(q_by_local):
            out.append(q_by_local[li] if q_by_local[li] is not None else 0)
    return bytes(out), (FLAG_QUALITY if quality else 0)


def encode_delta(ops):
    """DELTA update: ops = [(optype, nid, quality_byte_or_None)]."""
    out = bytearray()
    flags = FLAG_HAS_OPS
    for optype, nid, qb in ops:
        out.append(optype & 0x03)
        out += varint_encode(int(nid))
        if optype in (NI_OP_ADD, NI_OP_QUALITY):
            out.append(qb if qb is not None else 0)
    return bytes(out), flags


def encode_summary(neighbor_count, segment_count, redundancy_level, unique_path, confidence):
    """L1 SUMMARY: a few bytes about local structure, no IDs.
    redundancy_level: 0 LOW, 1 MED, 2 HIGH; unique_path: 0/1;
    confidence: 0..3."""
    packed = ((redundancy_level & 0x03) << 5) | ((unique_path & 0x01) << 4) \
             | ((int(confidence) & 0x03) << 2)
    out = bytearray()
    out.append(neighbor_count & 0xFF)
    out.append(segment_count & 0xFF)
    out.append(packed)
    return bytes(out), 0


def choose_best_neighbor_encoding(entries, local_map, quality=True):
    """AUTO: pick the shortest representation for this message.
    Returns (name, payload, flags). Bitmap only when it is strictly smaller
    and the shared local dictionary is complete."""
    raw, flags = encode_full_raw32(entries, quality)
    best = ('raw32', raw, flags)
    dv, flags2 = encode_full_delta_varint(entries, quality)
    if len(dv) < len(best[1]):
        best = ('delta_varint', dv, flags2)
    bm, flags3 = encode_full_local_bitmap(entries, local_map, quality)
    if bm is not None and len(bm) < len(best[1]):
        best = ('local_bitmap', bm, flags3)
    return best


# ----------------------------------------------------------------------
# decoders (sim-level "parsed" equivalents; used by unit tests)
# ----------------------------------------------------------------------
def decode_full_raw32(payload, quality=True):
    entries = []
    step = 5 if quality else 4
    for off in range(0, len(payload), step):
        nid = int.from_bytes(payload[off:off + 4], 'little')
        qb = payload[off + 4] if quality and off + 4 < len(payload) else None
        entries.append((nid, qb))
    return entries


def decode_full_delta_varint(payload, quality=True):
    entries = []
    pos = 0
    prev = 0
    while pos < len(payload):
        delta, pos = varint_decode(payload, pos)
        nid = prev + delta
        prev = nid
        qb = payload[pos] if quality and pos < len(payload) else None
        if quality:
            pos += 1
        entries.append((nid, qb))
    return entries


def decode_full_local_bitmap(payload, local_map, quality=True):
    """Reconstruct entries from a bitmap using the shared local dictionary
    (reverse: {local_id: real_id}). Payload: bitmap_len(1) | bitmap |
    [quality(1) per present local id, ascending]."""
    reverse = {li: rid for rid, li in local_map.items()}
    if not payload:
        return []
    n_bytes = payload[0]
    bitmap = payload[1:1 + n_bytes]
    present_locals = []
    for byte_i, b in enumerate(bitmap):
        for bit in range(8):
            if b & (1 << bit):
                present_locals.append(byte_i * 8 + bit)
    entries = []
    pos = 1 + n_bytes
    for li in sorted(present_locals):
        rid = reverse.get(li)
        if rid is None:
            continue  # unknown local id — cannot decode (needs FULL resync)
        qb = payload[pos] if quality and pos < len(payload) else None
        if quality:
            pos += 1
        entries.append((rid, qb))
    return entries
