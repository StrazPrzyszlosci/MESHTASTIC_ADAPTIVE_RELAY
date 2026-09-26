# FIRMWARE RESOURCE MODEL (preliminary, nRF52840)

Preliminary static-memory model for porting Adaptive Relay (research
default: N3S_SHEP2D) to nRF52840-class firmware. No C++ port is included
in this repository; this document sizes one.

Basis: the state containers in `lib/adaptive.py` (see `NeighborInfo` and
the router's bounded maps), TOP-K strict mode (K=8 CORE + PROTECTED),
and the measured neighbor-table occupancy from simulation (TOP-K strict:
mean **15.7 entries/node** in dense topologies at unchanged reach,
vs 26.6 unbounded).

## Per-neighbor record (CORE table)

`NeighborInfo` has 16 logical fields; a packed C struct needs:

| Field | Type | Bytes |
|---|---|---|
| node id | uint16 (sim) / uint32 NodeNum (firmware, short-code compressible) | 2-4 |
| last_seen, first_seen | uint32 ms (fold first_seen into flags) | 4 |
| rssi_ema | int8 dBm | 1 |
| pdr | uint8 (0-255 quantized) | 1 |
| etx | uint8 (x16 quantized) | 1 |
| stability | uint8 | 1 |
| weight | uint8 | 1 |
| weight_seg[4] | uint8 x4 | 4 |
| obs | uint16 | 2 |
| relay_attempts / successes | uint8 x2 | 2 |
| velocity | uint8 (m/s x10) | 1 |
| position | 2 x int16 m (only if POSITION packets observed) | 0-4 |
| **total** | | **20-24 B** |

snr_ema, rssi_slope etc. are derivable and not stored.

## Fixed per-node state

| Container | Bound | Packed size |
|---|---|---|
| CORE neighbors (TOP-K strict K=8) | 8 x 24 B | 192 B |
| PROTECTED entries (measured ~7.7 in dense) | 8 x 24 B | 192 B |
| 2-hop sets (CORE only) | 8 nids x 8 x 2 B | 128 B |
| routes (dest -> primary/backup1/backup2) | 16 recent origins x 12 B | 192 B |
| pending defer bookkeeping | 16 in-flight x 20 B (config cap 64) | 320 B (1.3 KB burst) |
| watchdog expected_relay | 16 x 16 B | 256 B |
| in-air copy counters | 16 seqs x uint16 | 32 B |
| min-hop-heard LRU | 32 x 6 B | 192 B |
| census state (suppression) | 8 ids + counters | 64 B |
| SHEPHERD (epoch, lease, score, budget) | scalars | 48 B |
| N4 token bucket | 3 scalars | 16 B |
| CEF_BOOST FSM (window counters) | 8 x uint16 | 32 B |
| segments/counters | 4 sectors | 32 B |
| software timers (watchdog, shepherd, prune, census, boost, ...) | ~9 x 12 B | 108 B |
| **total RAM** | | **~1.8 KB** (2.5 KB with pending burst) |

nRF52840 has 256 KB RAM; the router state is **< 1% of RAM**. All
containers are fixed-size arrays with expiry (no dynamic allocation).

## Flash

`lib/adaptive.py` is ~3.5 kLOC Python; the equivalent C++ (policy FSMs,
table pruning, codec) is estimated at **30-50 KB flash** including the
optional NEIGHBORINFO codec (~2 KB). Opt-in module: the firmware build
excludes it entirely when the feature flag is off.

## Protocol additions (airtime)

| Addition | Size | Rate | Airtime impact |
|---|---|---|---|
| N3 relay designation on forwarded copies | 1-3 node ids + rank bits ≈ 5-13 B | per designated forward | +1-2 LoRa symbols (≈ +28-57 ms at SF11/250 kHz) per forwarded copy |
| census suppression | 0 B (implicit) | - | removes redundant copies (measured: net TX **below** MF in dense; the designation overhead is paid only on copies that are actually sent) |
| SHEPHERD COLLECT/lease | ~8 B control packet | rare (rescue events, budget-capped 4.0) | negligible |
| NEIGHBORINFO (ablated OFF) | 16 B header + 5 B/neighbor | periodic if enabled | excluded from the default |

Net effect measured in simulation: N3S_SHEP2D transmits **at or below**
MANAGED_FLOOD levels in most topologies (TX savings statistically
significant in 9 of 17 validation scenarios, parity in dense); the +TX
cases are long chain topologies where extra copies buy reach. The
designation overhead is paid only on copies that are actually sent.
(CEF_BOOST — congestion mode — additionally cuts dense TX substantially
under sustained load.)

## Required firmware interfaces

1. Decoded-packet callback with header fields + RSSI (existing RX path).
2. Own TX/ACK history (implicit-ACK detection — exists as `was_seen_recently` analog).
3. Channel-utilization dial (firmware `ChannelUtilization` — exists).
4. TX scheduling hook with per-packet delay (exists: `setTransmitDelay` analog).
5. Monotonic ms clock + 3-8 software timers.
6. Optional: POSITION payload access for segmentation/mobility (ablatable).

## Remaining blockers before a port (not part of this repository)

- D5 failure-scenario campaign (kill/revive under load)
- D6 watchdog tri-state under half-duplex deafness
- protobuf schema for relay designation + COLLECT (3 candidate encodings)
- hardware test plan (two isolated pockets + a bridge node, the
  reproduction suite's core discriminator)
