# UPSTREAM BASELINE

Official upstream snapshot used as the base of this repository.

- URL: https://github.com/meshtastic/Meshtasticator.git
- Branch: `master`
- Commit: `17ceb82` (17ceb8231079d87b070abc6132181e4c6b20202d)
- Commit date: 2026-07-28 21:06:57 +02:00
- Snapshot taken: 2026-09-25
- Working branch created for Adaptive Relay: `adaptive-relay`

## Environment

- Python 3.12.3, dependencies from upstream `requirements.txt`
  (simpy 4.1.1, numpy, matplotlib, pandas, PyPubSub, PyYAML, protobuf, meshtastic~=2.6.1)

## Upstream test suite

```
./.venv/bin/python -m unittest
Ran 31 tests in 20.506s
OK
```

## MF sanity run (official, unmodified code)

Command:

```
./.venv/bin/python loraMesh.py 100 --no-gui --simtime-seconds 600
```

Default config: MANAGED_FLOOD, LONG_FAST, SEED=44, area 15x15 km,
period 100 s, packet 40 B, hop limit default, moving nodes enabled
upstream (32/100 moved in this run).

Results (deterministic for SEED=44):

```
Router Type: ROUTER_TYPE.MANAGED_FLOOD
Number of messages created: 513
Number of packets sent: 5485 to 543015 potential receivers
Number of collisions: 11376
Number of packets sensed: 61684
Number of packets received: 20415
Delay average (ms): 88068.19
Average Tx air utilization: 6.37 %
Percentage of packets that collided: 18.44
Average percentage of nodes reached: 18.79
Percentage of received packets containing new message: 46.75
Number of packets dropped by delay/hop limit: 11517
No links: 89.7 %
Number of moving nodes: 32
Number of moving nodes w/ GPS: 7
```

This is the reference MF baseline. After Adaptive Relay is integrated,
the identical command must reproduce these numbers bit-for-bit
(same PHY/MAC for MF and AR — see SIMULATOR_AUDIT.md).
