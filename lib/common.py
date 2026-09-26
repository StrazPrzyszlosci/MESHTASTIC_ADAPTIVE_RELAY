import random
import os
import math

import numpy as np

from lib import phy
from lib.point import Point

def find_random_position(conf, node_configs) -> (float, float):
    """Given a simulation config and list of existing node configs/nodes, find a
    randomly chosen position for the next node such that it is within the
    simulation bounds, within radio range of at least one existing node, and
    not within the minimum distance of any existing node.

    Arguments:
    conf -- Config object defining simulation
    node_configs -- list of NodeConfig objects, or node objects, defining pre-existing nodes

    Returns:
    (x, y) - x and y coordinates of location for next node
    """
    foundMin = True
    foundMax = False
    tries = 0
    position = None
    while not (foundMin and foundMax):
        a = random.random()
        b = random.random()
        posx = a*conf.XSIZE+conf.OX-conf.XSIZE/2
        posy = b*conf.YSIZE+conf.OY-conf.YSIZE/2
        pos_candidate = Point(posx, posy, conf.HM)
        if len(node_configs) > 0:
            # Reset PER candidate: the old sticky flag meant one MINDIST
            # violation rejected every later candidate -> position stayed
            # None -> crash on .x (tight layouts, e.g. sparse with seed 18).
            foundMin = True
            for n in node_configs:
                dist = n.position.euclidean_distance(pos_candidate)
                if dist < conf.MINDIST:
                    foundMin = False
                    break
                pathLoss = phy.estimate_path_loss(conf, dist, conf.FREQ)
                rssi = conf.PTX + 2*conf.GL - pathLoss
                # At least one node should be able to reach it
                if rssi >= conf.current_preset["sensitivity"]:
                    foundMax = True
            if foundMin and foundMax:
                position = pos_candidate
        else:
            position = pos_candidate
            foundMin = True
            foundMax = True
        tries += 1
        if tries > 1000:
            print('Could not find a location to place the node. Try increasing XSIZE/YSIZE or decreasing MINDIST.')
            break
    if position is None:
        # Deterministic fallback: place toward the area center from an
        # existing node at <= 0.5*MAXRANGE — guaranteed link and in-bounds,
        # no RNG dependency (keeps runs reproducible instead of crashing).
        if node_configs:
            anchor = node_configs[0].position
            dx, dy = conf.OX - anchor.x, conf.OY - anchor.y
            norm = math.hypot(dx, dy)
            if norm < 1e-9:
                dx, dy, norm = 1.0, 0.0, 1.0
            r = 0.5 * phy.MAXRANGE
            if r < 2 * conf.MINDIST:
                r = 2 * conf.MINDIST
            position = Point(anchor.x + r * dx / norm,
                             anchor.y + r * dy / norm, conf.HM)
        else:
            position = Point(conf.OX, conf.OY, conf.HM)
    return max(-conf.XSIZE/2, position.x), max(-conf.YSIZE/2, position.y)

# TODO: once lib/interactive no longer uses this, we can remove this and put all distance calculation in Point
def calc_dist(x0, x1, y0, y1, z0=0, z1=0):
    return np.sqrt(((abs(x0-x1))**2)+((abs(y0-y1))**2)+((abs(z0-z1)**2)))

def setup_asymmetric_links(conf, nodes):
    """updates conf to populate LINK_OFFSET member to simulate asymmetric links
    """
    asymLinkRng = random.Random(conf.SEED)
    totalPairs = 0
    symmetricLinks = 0
    asymmetricLinks = 0
    noLinks = 0
    for i in range(conf.NR_NODES):
        for b in range(conf.NR_NODES):
            if i != b:
                if conf.MODEL_ASYMMETRIC_LINKS:
                    conf.LINK_OFFSET[(i, b)] = asymLinkRng.gauss(conf.MODEL_ASYMMETRIC_LINKS_MEAN, conf.MODEL_ASYMMETRIC_LINKS_STDDEV)
                else:
                    conf.LINK_OFFSET[(i, b)] = 0

    for a in range(conf.NR_NODES):
        for b in range(conf.NR_NODES):
            if a != b:
                # Calculate constant RSSI in both directions
                nodeA = nodes[a]
                nodeB = nodes[b]
                distAB = nodeA.position.euclidean_distance(nodeB.position)
                pathLossAB = phy.estimate_path_loss(conf, distAB, conf.FREQ, nodeA.position.z, nodeB.position.z)

                offsetAB = conf.LINK_OFFSET[(a, b)]
                offsetBA = conf.LINK_OFFSET[(b, a)]

                rssiAB = conf.PTX + nodeA.antennaGain - pathLossAB - offsetAB
                rssiBA = conf.PTX + nodeB.antennaGain - pathLossAB - offsetBA

                canAhearB = (rssiAB >= conf.current_preset["sensitivity"])
                canBhearA = (rssiBA >= conf.current_preset["sensitivity"])

                totalPairs += 1
                if canAhearB and canBhearA:
                    symmetricLinks += 1
                elif canAhearB or canBhearA:
                    asymmetricLinks += 1
                else:
                    noLinks += 1

    return totalPairs, symmetricLinks, asymmetricLinks, noLinks
