# ZERO ORACLE AUDIT

Answer to the question: **does the Adaptive Relay router use any
information a real Meshtastic node could not have?**

Verdict: **PASS** — every decision input is firmware-realistic. This is
enforced three ways: (1) statically by
`tests/test_adaptive.py::TestNoOracle` (AST scan for forbidden simulator
globals), (2) by the grep sweeps below, (3) by code review of every
hook boundary in `lib/node.py`.

## Method

- AST scan (`TestNoOracle.FORBIDDEN_ATTRS`): `LINK_OFFSET`, `sensedByN`,
  `detectedByN`, `collidedAtN`, `receivedAtN`, `packetsAtN`,
  `estimate_path_loss`, `setup_asymmetric_links` — **zero references in
  `lib/adaptive.py`**.
- Text sweep of `lib/adaptive.py` for: `self.nodes[` iteration,
  `NR_NODES`, scenario names, `env.peek`/`env.schedule` (future-event
  access), collision flags — **zero hits**.
- Manual review of the 12 hook sites in `lib/node.py`: every value passed
  into `lib/adaptive.py` is either a decoded-packet field or a
  self-observation.

## Signal inventory (decision inputs)

| Signal | Where used | Available in real firmware? | Decision |
|---|---|---|---|
| `p.seq`, `p.txNodeId`, `p.origTxNodeId`, `p.destId`, `p.hopLimit`, `p.wantAck`, `p.packetLen` | learn/census/N3/watchdog (whole file) | YES — MeshPacket header fields | OK |
| own RX RSSI (`p.rssiAtN[self.nodeid]`, passed by node.py hook) | learn, PDR, rank ordering | YES — LoRa radio reports RSSI per packet | OK |
| `p.timeOnAir` | ToA-scaled windows (N3, CEF, N4) | YES — computed from modem preset + payload len (firmware `getAirTime`) | OK |
| `p.genTime` | probe bookkeeping | YES — packet timestamp field | OK |
| overheard copy count per seq (`note_rx_start/end` on sensed/collided copies) | census suppression, CEF redundancy | YES — CAD/sense events are observable (can't decode, can sense) | OK |
| own TX queue (`node.packets`), own send history | watchdog expectations, retransmit | YES — own TX state | OK |
| own channel utilization (`channel_utilization_percent`) | CEF_BOOST busy metric, N4 | YES — firmware ChannelUtilization dial | OK |
| own position (`node.position`) | segmentation (geo), mobility score | YES — own GPS/position (only for itself) | OK |
| sender position in POSITION packets (`p.pos_x/y`) | segmentation (geo mode) | YES — Meshtastic POSITION payload | OK |
| advertised neighbor tables (`p.ni_neighbors`, NEIGHBORINFO) | 2-hop knowledge (ablated OFF) | YES — explicit protocol addition (cost-metered) | OK |
| COLLECT/lease/rescue packets (SHEPHERD) | rescue layer | YES — explicit protocol addition | OK |
| relay designation list (`p.relay_designation`) | N3 ranked whisper | YES — protocol addition (analogue of firmware `relay_node` header, ordered) | OK |
| `node.role` / `is_client_mute` | eligibility, role cost | YES — device role config | OK |
| per-node RNG (`nodeRng`, `moveRng`, salted with `rng_salt` + `conf.SEED`) | jitter draws | SEED is a *reproducibility salt only*: on firmware it is replaced by per-device entropy; it carries no network information | OK (note) |
| own watchdog ACK/echo observations | PDR/ETX, PRIMARY failover | YES — own observations only | OK |
| own clock (`env.now`, timeouts) | all timers | YES — own clock | OK |
| `node.transmit(...)` / `MeshPacket(...)` construction | forwarding | simulator radio-medium API (same one MANAGED_FLOOD uses); not knowledge | OK |

## Explicitly NOT used (verified absent)

- Global topology / connectivity map (the map object is passed to
  `MeshPacket` only as the simulator's broadcast medium — identical to
  what MF's own forwarding does; the router logic never queries it).
- `sensedByN`/`receivedAtN`/`collidedAtN`/`detectedByN` of **other**
  nodes (own-entry collision flag is passed as a boolean by the node.py
  hook — "did *I* decode this" — which a real radio knows).
- True positions of other nodes (only own position + POSITION payloads).
- `LINK_OFFSET` (asymmetric-link simulation offsets).
- Scenario name, node count (`conf.NR_NODES`), hop count of the network.
- Future sim events, queue inspection, iteration over `self.nodes` as a
  knowledge source.
- Collision bookkeeping of other receivers.

## Known boundary notes (honest disclosure)

1. `conf.SEED` salts the router's private jitter RNG (reproducibility).
   On firmware this becomes per-device entropy; no network knowledge
   is carried. The `rng_salt` AR_PARAM exists to retune this cleanly.
2. The simulator computes `timeOnAir` centrally; real firmware computes
   the same value from the modem preset — same number, different place.
3. Watchdog "echo" observations rely on hearing one's own relayed copy
   (implicit ACK), which is exactly the firmware's implicit-ACK signal.
