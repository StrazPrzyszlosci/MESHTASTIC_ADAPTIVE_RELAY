import logging
import math
import random

from lib.config import CONFIG

# TODO: if our config deviates from the default, we WILL get incorrect results.
# refactor things to take a config object
conf = CONFIG

logger = logging.getLogger(__name__)

# checked as of tag v2.7.15.567b8ea in meshtastic-firmware repo
NUM_SYM_CAD = 2
NUM_SYM_CAD_24GHZ = 4

#                           CAD duration   +     airPropagationTime+TxRxTurnaround+MACprocessing
def get_current_slot_time(): # from RadioInterface::computeSlotTimeMsec
    # all times in ms
    sum_prop_turnaround_mac_time = 0.2 + 0.4 + 7
    firmware_bw = conf.current_preset["bw"] / 1000 # convert Hz to KHz to match firmware
    symbol_time = (2.0 ** conf.current_preset["sf"]) / firmware_bw

    if conf.REGION['wide_lora']:
        # TODO: currently wide_lora isn't fully implemented
        # currently only 2.4GHz LoRa
        return (NUM_SYM_CAD_24GHZ + (2 * conf.current_preset['sf'] + 3) / 32) * symbol_time + sum_prop_turnaround_mac_time
    else:
        return max(2.25, NUM_SYM_CAD + 0.5) * symbol_time + sum_prop_turnaround_mac_time


def check_collision(conf, env, packet, rx_nodeId, packetsAtN):
    # Check for collisions at rx_node
    col = 0
    if conf.COLLISION_DUE_TO_INTERFERENCE:
        if random.randrange(10) <= conf.INTERFERENCE_LEVEL * 10:
            packet.collidedAtN[rx_nodeId] = True

    if packetsAtN[rx_nodeId]:
        for other in packetsAtN[rx_nodeId]:
            if frequency_collision(packet, other) and sf_collision(packet, other):
                if timing_collision(conf, env, packet, other):
                    logger.debug(f'Packet nr. {packet.unique_packet_seq} from {packet.txNodeId} and packet nr. {other.unique_packet_seq} from {other.txNodeId} will collide at node {rx_nodeId}!')
                    c = power_collision(packet, other, rx_nodeId)
                    # mark all the collided packets
                    for p in c:
                        p.collidedAtN[rx_nodeId] = True
                        if p == packet:
                            col = 1
                else:
                    pass  # no timing collision
        return col
    return 0


def frequency_collision(p1, p2):
    if abs(p1.freq - p2.freq) <= 120 and (p1.bw == 500 or p2.bw == 500):
        return True
    elif abs(p1.freq - p2.freq) <= 60 and (p1.bw == 250 or p2.bw == 250):
        return True
    elif abs(p1.freq - p2.freq) <= 30:
        return True
    return False


def sf_collision(p1, p2):
    return p1.sf == p2.sf


def power_collision(p1, p2, rx_nodeId):
    """LoRa capture effect. ADAPTIVE_RELAY: threshold is a config param
    (CAPTURE_THRESHOLD_DB; 0 = OFF -> both collide). Default 6 dB preserves
    the upstream behavior."""
    powerThreshold = getattr(conf, 'CAPTURE_THRESHOLD_DB', 6) or 6
    d = p1.rssiAtN[rx_nodeId] - p2.rssiAtN[rx_nodeId]
    if not getattr(conf, 'CAPTURE_THRESHOLD_DB', 6) or abs(d) < powerThreshold:
        # packets are too close to each other, both collide
        # return both packets as casualties
        return (p1, p2)
    if d < powerThreshold:
        # p2 overpowered p1, return p1 as casualty
        return (p1,)
    # p2 was the weaker packet, return it as a casualty
    return (p2,)


def timing_collision(conf, env, p1, p2):
    """ assuming p1 is the freshly arrived packet, check if the packet collides 
        or not (when only the first n - 5 preamble symbols overlap)
    """
    Tpreamb = 2 ** p1.sf / (1.0 * p1.bw) * (conf.NPREAM - 5)
    p1_cs = env.now + Tpreamb
    if p1_cs < p2.endTime:  # p1 collided with p2 and lost
        return True
    return False


def is_channel_active(node, env):
    if random.randrange(10) <= node.conf.INTERFERENCE_LEVEL * 10:
        return True
    for p in node.packets:
        if p.detectedByN[node.nodeid]:
            # You will miss detecting a packet if it has just started before you could do CAD
            if p.startTime + get_current_slot_time() <= env.now <= p.endTime:
                return True
    return False


def airtime(conf, sf, cr, pl, bw):
    pl = pl + conf.HEADERLENGTH  # add Meshtastic header length
    H = 0  # implicit header disabled (H=0) or not (H=1)
    DE = 0  # low data rate optimization enabled (=1) or not (=0)

    if bw == 125e3 and sf in [11, 12]:  # low data rate optimization
        DE = 1
    if sf == 6:  # can only have implicit header with SF6
        H = 1

    Tsym = (2.0 ** sf) / bw
    Tpream = (conf.NPREAM + 4.25) * Tsym
    payloadSymbNB = 8 + max(math.ceil((8.0 * pl - 4.0 * sf + 28 + 16 - 20 * H) / (4.0 * (sf - 2 * DE))) * (cr + 4), 0)
    Tpayload = payloadSymbNB * Tsym

    return (Tpream + Tpayload) * 1000


def estimate_path_loss(conf, dist, freq, txZ=None, rxZ=None, model=None):
    '''Calculate path loss between transmitter and receiver using a specific model

    Arguments:
    conf -- config object
    dist -- distance between nodes in meters
    freq -- frequency in MHz
    txZ -- height of transmitter. Default: conf.HM
    rxZ -- height of receiver. Default: conf.HM
    model -- choice of model (currently integer in [0,6], default: conf.MODEL)

    Returns:
    path loss as float
    '''
    if txZ is None:
        txZ = conf.HM
    if rxZ is None:
        rxZ = conf.HM
    if model is None:
        model = conf.MODEL

    # With randomized movements we may end up on top of another node which is problematic for log(dist)
    dist = max(dist, .001)

    # Log-Distance model
    if model == 0:
        Lpl = conf.LPLD0 + 10 * conf.GAMMA * math.log10(dist / conf.D0)

    # Okumura-Hata model
    elif 1 <= model <= 4:
        # small and medium-size cities
        if model == 1:
            ahm = (1.1 * (math.log10(freq) - 6.0) - 0.7) * rxZ - (1.56 * (math.log10(freq) - 6.0) - 0.8)
            C = 0
        # metropolitan areas
        elif model == 2:
            if freq <= 200000000:
                ahm = 8.29 * ((math.log10(1.54 * rxZ)) ** 2) - 1.1
            elif freq >= 400000000:
                ahm = 3.2 * ((math.log10(11.75 * rxZ)) ** 2) - 4.97
            C = 0
        # suburban environments
        elif model == 3:
            ahm = (1.1 * (math.log10(freq) - 6.0) - 0.7) * rxZ - (1.56 * (math.log10(freq) - 6.0) - 0.8)
            C = -2 * ((math.log10(freq) - math.log10(28000000)) ** 2) - 5.4
        # rural area
        elif model == 4:
            ahm = (1.1 * (math.log10(freq) - 6.0) - 0.7) * rxZ - (1.56 * (math.log10(freq) - 6.0) - 0.8)
            C = -4.78 * ((math.log10(freq) - 6.0) ** 2) + 18.33 * (math.log10(freq) - 6.0) - 40.98

        A = 69.55 + 26.16 * (math.log10(freq) - 6.0) - 13.82 * math.log10(txZ) - ahm
        B = 44.9 - 6.55 * math.log10(txZ)
        Lpl = A + B * (math.log10(dist) - 3.0) + C

    # 3GPP model
    elif 5 <= model < 7:
        # Suburban Macro
        if model == 5:
            C = 0  # dB
        # Urban Macro
        elif model == 6:
            C = 3  # dB

        Lpl = (44.9 - 6.55 * math.log10(txZ)) * (math.log10(dist) - 3.0) \
            + 45.5 + (35.46 - 1.1 * rxZ) * (math.log10(freq) - 6.0) \
            - 13.82 * math.log10(rxZ) + 0.7 * rxZ + C
    else:
        raise ValueError(f"Unsupported path loss model: {model}")

    return Lpl


# TODO: take conf as parameter so we don't use this module's default conf
def zero_link_budget(dist):
    return conf.PTX + 2 * conf.GL - estimate_path_loss(conf, dist, conf.FREQ) - conf.current_preset["sensitivity"]


def rootFinder(func, x0, args=(), tol=1, maxiter=100):
  """Newton-Raphson root finder."""
  x = x0
  for _ in range(maxiter):
      fx = func(x, *args)
      dfx = (func(x + 1e-6, *args) - fx) / 1e-6
      if dfx == 0:
          print("Warning: could not estimate max. range")
          return x
      x_new = x - fx / dfx
      if abs(x_new - x) < tol:
          return x_new
      x = x_new
  print("Warning: could not estimate max. range")
  return x

# TODO: take conf as parameter so we don't use this module's default conf
def zero_link_budget_with_gain(dist, gain):
    return conf.PTX + gain - estimate_path_loss(conf, dist, conf.FREQ) - conf.current_preset["sensitivity"]

def estimate_max_range(gain):
    return rootFinder(zero_link_budget_with_gain, 1500, args=(gain,))

# TODO: take conf as parameter so we don't use this module's default conf
MAXRANGE = rootFinder(zero_link_budget, 1500)
