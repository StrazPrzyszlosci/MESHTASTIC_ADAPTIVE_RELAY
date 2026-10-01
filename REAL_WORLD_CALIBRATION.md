# REAL-WORLD CALIBRATION DATA

Firmware logs and network reports found in public sources, used to
cross-check this simulator's assumptions. Sources are quoted verbatim;
numbers below are real, not simulated.

## 1. hopStart is a real, observed header field

Production serial log (meshtastic/firmware#5754, RAK4631):

```
DEBUG | 18:39:17 189822 [NodeInfo] enqueue for send
  (id=0xca9ac710 fr=0x5570b7aa to=0xffffffff, WantAck=0, HopLim=4
   Ch=0x8 encrypted len=100 rxtime=1736015957 hopStart=4 priority=10)
```

- `hopStart` appears verbatim in real transmit logs → the simulator's
  `MeshPacket.hopStart` model (set at origin, copied unchanged by relays)
  mirrors a real field.
- The protobuf schema documents `hops_away` as **"0 if adjacent"**
  (mesh.proto: `uint32 hops_away = 9`), computed as `hop_start - hop_limit`
  with a has-flag guard — identical semantics to the NodeDB-analog in this
  repository.

## 2. Channel utilization spans 0.15%..50% in real networks

- Quiet/typical node (production log, meshtastic/firmware#5754):

  ```
  Total channel utilization: 3.15%
  Transmit air utilization: 0.15%
  ```

- Busy mountain-top ROUTER overlooking a large region
  (meshtastic/firmware#4466): channel utilization **"perpetually
  hovering around 50%"**, grown from ~20% within months; the node sees
  **more than 40 nodes**; the firmware itself gates telemetry/TX by
  channel utilization (`isTxAllowedChannelUtil()`).

Implications:

1. The CEF_BOOST congestion threshold (~35% busy evidence) sits inside
   the REAL observed range: it stays inert in quiet networks (0.15-3%)
   and engages exactly in the saturated-mesh regime that really exists
   (large-mesh routers at ~50%). Boost is an extreme-event mode — the
   real world confirms both ends.
2. Traffic-scale interpretation: this simulator's "1x load" (30 s
   generation period per node) produces channel pressure comparable to
   the BUSIEST real meshes, roughly 10-100x a quiet community network
   (real reports: ~30 active nodes, ~1 user message/hour, telemetry
   every 12 h, positions every ~15 min). The paired A/B comparisons in
   this repository remain internally valid; ABSOLUTE numbers should be
   read as the busy-mesh regime.

## 3. Real topology/pathology reports (matching the problem statement)

- "1-2 nodes are direct at 10 km... others 1-2-3-4 hops" — typical hops
  distribution (meshtastic/discussions#484).
- "Node just 3 km away, but I receive it with 3 hops... no message goes
  through successfully" — suboptimal relay selection in real networks
  (same discussion). This is precisely the failure class the ranked
  whisper (N3) is designed to address.
- Nodes occasionally heard 5 hops away / 50-80 km in mountainous terrain
  (r/meshtastic field reports) — consistent with the chain/corridor
  scenarios in this repository.
- Real network size: ~30 active nodes typical; >40 nodes on high
  vantage-point routers — matches this simulator's 25-50 node panels.

## 4. Data collection for the hardware testbed

The official Serial module has a LOG mode ("Mesh Packet Activity
Logger", meshtastic.org/docs/configuration/module/serial) that emits a
line per observed packet over UART — the recommended ground-truth
collector for an FW+ pilot: it yields exactly the passive evidence
streams this router consumes (decoded headers, RX time, per-packet
records).

## 5. Corrections adopted from these findings

- hops_away clamp fixed to allow 0 for adjacent nodes (protobuf: "0 if
  adjacent"); see the NodeDB audit notes.
- README caveats: absolute congestion levels in the panels map to the
  busy-mesh regime; quiet-network behavior of congestion modes is inert
  by design and confirmed by the 0.15-3.15% real-world utilization
  observations.
