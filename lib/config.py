from enum import Enum

class Config:

    class ROUTER_TYPE(Enum):
        MANAGED_FLOOD = 'MANAGED_FLOOD'
        ADAPTIVE_RELAY = 'ADAPTIVE_RELAY'

    def __init__(self):
        self.MODEL = 5  # Path loss model to use (see README)

        self.XSIZE = 15000  # horizontal size of the area to simulate in m
        self.YSIZE = 15000  # vertical size of the area to simulate in m
        self.OX = 0.0  # origin x-coordinate
        self.OY = 0.0  # origin y-coordinate
        self.MINDIST = 10  # minimum distance between each node in the area in m

        self.GL = 0  # antenna gain of each node in dBi
        self.HM = 1.0  # height of each node in m

        ### Meshtastic specific ###
        self.hopLimit = 3  # default 3
        self.router = False  # set role of each node as router (True) or normal client (False)
        self.maxRetransmission = 3  # default 3 -- not configurable by Meshtastic
        ### End of Meshtastic specific ###

        self.ONE_SECOND_INTERVAL = 1000
        self.TEN_SECONDS_INTERVAL = self.ONE_SECOND_INTERVAL * 10
        self.ONE_MIN_INTERVAL = self.TEN_SECONDS_INTERVAL * 6
        self.ONE_HR_INTERVAL = self.ONE_MIN_INTERVAL * 60

        ### Discrete-event specific ###
        self.ENABLE_CONNECTIVITY_MAP = True # use the connectivity map optimization
        self.CONNECTIVITY_MAP_RSSI_MARGIN = 8

        self.MODEM_PRESET = "LONG_FAST"  # LoRa modem preset to use (default LONG_FAST matches firmware)
        self.PERIOD = 100 * self.ONE_SECOND_INTERVAL  # mean period of generating a new message with exponential distribution in ms
        self.PACKETLENGTH = 40  # payload in bytes
        self.SIMTIME = 30 * self.ONE_MIN_INTERVAL  # duration of one simulation in ms
        self.INTERFERENCE_LEVEL = 0.05  # chance that at a given moment there is already a LoRa packet being sent on your channel, outside of the Meshtastic traffic. Given in a ratio from 0 to 1.
        self.COLLISION_DUE_TO_INTERFERENCE = False
        # Sim-level model of the firmware MeshPacket hopStart header field
        # (hops_away evidence source). False = ablation world: the field is
        # not carried, NodeDB-analog stays empty, all consumers degrade to
        # baseline behavior (used to prove graceful absence-of-data).
        self.MODEL_HOPSTART = True
        # Firmware-realistic ROUTER observability model: relayed copies do
        # not carry a usable transmitter identity (real rebroadcast keeps
        # from = originator; relay_node is only a 1-byte hint). When True,
        # the router sees txNodeId only for packets originated by the
        # 1-hop transmitter, and collided frames are not attributed to a
        # packet id. False (default) = full simulator observability.
        self.REALISTIC_WIRE = False
        # ADAPTIVE_RELAY realism knobs (defaults = historical upstream behavior)
        self.CAPTURE_THRESHOLD_DB = 6   # LoRa capture effect; 0 = OFF (both collide)
        self.CLOCK_DRIFT_PPM = 0       # per-node deterministic drift (+/-); 0 = perfect clocks
        self.DMs = False  # Set True for sending DMs (with random destination), False for broadcasts
        # from firmware RegionInfo regions[] in src/mesh/RadioInterface.cpp
        self.regions = {
            "US": {
                "freq_start": 902.0e6,
                "freq_end": 928.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 30,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "EU_433": {
                "freq_start": 433.0e6,
                "freq_end": 434.0e6,
                "duty_cycle": 10,
                "spacing": 0,
                "power_limit": 10,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "EU_868": {
                "freq_start": 869.4e6,
                "freq_end": 869.65e6,
                "duty_cycle": 10,
                "spacing": 0,
                "power_limit": 27,
                "audio_permitted": False,
                "frequency_switching": False,
                "wide_lora": False
            },
            "CN": {
                "freq_start": 470.0e6,
                "freq_end": 510.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 19,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "JP": {
                "freq_start": 920.5e6,
                "freq_end": 923.5e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 13,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "ANZ": {
                "freq_start": 915.0e6,
                "freq_end": 928.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 30,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "ANZ_433": {
                "freq_start": 433.05e6,
                "freq_end": 434.79e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 14,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "RU": {
                "freq_start": 868.7e6,
                "freq_end": 869.2e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 20,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "KR": {
                "freq_start": 920.0e6,
                "freq_end": 923.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 23,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "TW": {
                "freq_start": 920.0e6,
                "freq_end": 925.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 27,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "IN": {
                "freq_start": 865.0e6,
                "freq_end": 867.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 30,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "NZ_865": {
                "freq_start": 864.0e6,
                "freq_end": 868.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 36,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "TH": {
                "freq_start": 920.0e6,
                "freq_end": 925.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 16,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "UA_433": {
                "freq_start": 433.0e6,
                "freq_end": 434.7e6,
                "duty_cycle": 10,
                "spacing": 0,
                "power_limit": 10,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "UA_868": {
                "freq_start": 868.0e6,
                "freq_end": 868.6e6,
                "duty_cycle": 1,
                "spacing": 0,
                "power_limit": 14,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "MY_433": {
                "freq_start": 433.0e6,
                "freq_end": 435.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 20,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "MY_919": {
                "freq_start": 919.0e6,
                "freq_end": 924.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 27,
                "audio_permitted": True,
                "frequency_switching": True,
                "wide_lora": False
            },
            "SG_923": {
                "freq_start": 917.0e6,
                "freq_end": 925.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 20,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "PH_433": {
                "freq_start": 433.0e6,
                "freq_end": 434.7e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 10,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "PH_868": {
                "freq_start": 868.0e6,
                "freq_end": 869.4e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 14,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "PH_915": {
                "freq_start": 915.0e6,
                "freq_end": 918.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 24,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "KZ_433": {
                "freq_start": 433.075e6,
                "freq_end": 434.775e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 10,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "KZ_863": {
                "freq_start": 863.0e6,
                "freq_end": 868.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 30,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "NP_865": {
                "freq_start": 865.0e6,
                "freq_end": 868.0e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 30,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "BR_902": {
                "freq_start": 902.0e6,
                "freq_end": 907.5e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 30,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": False
            },
            "LORA_24": {
                "freq_start": 2400.0e6,
                "freq_end": 2483.5e6,
                "duty_cycle": 100,
                "spacing": 0,
                "power_limit": 10,
                "audio_permitted": True,
                "frequency_switching": False,
                "wide_lora": True
            },
        }
        self.REGION = self.regions["US"]  # Select a different region here
        self.CHANNEL_NUM = 27  # Channel number

        self.GUI_ENABLED = True # whether to update/save the Tk/Matplotlib node-placement graph during CLI simulation
        self.PLOT = True # whether to plot the time schedule of packets after the simulation
        ### End of discrete-event specific ###

        ### PHY parameters (normally no change needed) ###
        self.PTX = self.REGION["power_limit"]

        # Modem presets from firmware RadioInterface::applyModemConfig() in src/mesh/RadioInterface.cpp
        # minimum sensitivity from https://www.rfwireless-world.com/calculators/LoRa-Sensitivity-Calculator.html, using a Noise Figure (NF) of 6dB
        # minimum received power for CAD: 3dB less than sensitivity
        # TODO: the 'bw' parameter is changed based on the region's 'wide_lora' setting. Implement this.
        # Note: we store bandwidth here in Hz, but the firmware uses KHz.
        self.MODEM_PRESETS = {
            "SHORT_TURBO": {
                "bw": 500e3,
                "cr": 5,
                "sf": 7,
                "sensitivity": -118.5,
                "cad_threshold": -121.5
            },
            "SHORT_FAST": {
                "bw": 250e3,
                "cr": 5,
                "sf": 7,
                "sensitivity": -121.5,
                "cad_threshold": -124.5
            },
            "SHORT_SLOW": {
                "bw": 250e3,
                "cr": 5,
                "sf": 8,
                "sensitivity": -124.0,
                "cad_threshold": -127.0
            },
            "MEDIUM_FAST": {
                "bw": 250e3,
                "cr": 5,
                "sf": 9,
                "sensitivity": -126.5,
                "cad_threshold": -129.5
            },
            "MEDIUM_SLOW": {
                "bw": 250e3,
                "cr": 5,
                "sf": 10,
                "sensitivity": -129.0,
                "cad_threshold": -132.0
            },
            "LONG_TURBO": {
                "bw": 500e3,
                "cr": 8,
                "sf": 11,
                "sensitivity": -128.5,
                "cad_threshold": -131.5
            },
            "LONG_FAST": {
                "bw": 250e3,
                "cr": 5,
                "sf": 11,
                "sensitivity": -131.5,
                "cad_threshold": -134.5
            },
            "LONG_MODERATE": {
                "bw": 125e3,
                "cr": 8,
                "sf": 11,
                "sensitivity": -134.5,
                "cad_threshold": -137.5
            },
            "LONG_SLOW": {
                "bw": 125e3,
                "cr": 8,
                "sf": 12,
                "sensitivity": -137.0,
                "cad_threshold": -140.0
            },
            # It doesn't appear like this is a preset that actually exists in the firmware now?
            "VERY_LONG_SLOW": {
                "bw": 62.5e3,
                "sf": 12,
                "cr": 8,
                "sensitivity": -140.0,
                "cad_threshold": -143.0
            }
        }

        self.FREQ = self.REGION["freq_start"] + self.MODEM_PRESETS[self.MODEM_PRESET]["bw"] * self.CHANNEL_NUM
        self.HEADERLENGTH = 16  # number of Meshtastic header bytes
        self.ACKLENGTH = 2  # ACK payload in bytes
        self.NOISE_LEVEL = -119.25  # some noise level in dB, based on SNR_MIN and minimum receiver sensitivity
        self.GAMMA = 2.08  # PHY parameter
        self.D0 = 40.0  # PHY parameter
        self.LPLD0 = 127.41  # PHY parameter
        self.NPREAM = 16   # number of preamble symbols from RadioInterface.h
        ### End of PHY parameters ###

        # Misc
        self.SEED = 44  # random seed to use
        # End of misc

        # Initializers
        self.NR_NODES = None
        # End of initializers

        ############################
        ####### ROUTER TYPE ########
        ############################
        # This can also be overwritten by scenarios defined in batchSim.py
        # or by passing this as the second command line param to loraMesh.py
        self.SELECTED_ROUTER_TYPE = self.ROUTER_TYPE.MANAGED_FLOOD

        #####################################################
        ####### ASYMMETRIC LINK SIMULATION VARIABLES ########
        #####################################################
        # Set this to True to enable the asymmetric link model
        # Adds a random offset to the link quality of each link
        self.MODEL_ASYMMETRIC_LINKS = True
        self.MODEL_ASYMMETRIC_LINKS_MEAN = 0
        self.MODEL_ASYMMETRIC_LINKS_STDDEV = 2

        #################################################
        ####### MOVING NODE SIMULATION VARIABLES ########
        #################################################
        self.MOVEMENT_ENABLED = True
        # The average number of meters a human walks in a minute
        self.WALKING_METERS_PER_MIN = 96
        # The average number of meters a human bikes in a minute
        self.BIKING_METERS_PER_MIN = 390
        # The average number of meters a human drives in a minute
        self.DRIVING_METERS_PER_MIN = 1500
        # The % of nodes that end up mobile in the simulation 0.4 = ~40%
        self.APPROX_RATIO_NODES_MOVING = 0.3
        # The % of mobile nodes that have GPS enabled 0.5 = 50%
        self.APPROX_RATIO_OF_NODES_MOVING_W_GPS_ENABLED = 0.3

        # 100 meters
        self.SMART_POSITION_DISTANCE_THRESHOLD = 100
        # 30s minimum time in firmware
        self.SMART_POSITION_DISTANCE_MIN_TIME = 30 * self.ONE_SECOND_INTERVAL
        # This mirrors the firmware's approach to monitoring channel utilization
        self.CHANNEL_UTILIZATION_PERIODS = 6

        #####################################################
        ####### ADAPTIVE_RELAY PARAMETERS (AR_*) ############
        #####################################################
        # All thresholds/timings of the ADAPTIVE_RELAY router live here so they
        # can be tuned on DEV seeds and frozen for VALIDATION without code
        # changes. Feature flags double as ablation switches (see REPORT.md).
        # NOTE: AR_PARAMS may be overridden per-run via JSON (adaptive runners).
        self.AR_PARAMS = {
            # --- neighbor / route memory ---
            'neighbor_expiry_ms': 300000,    # drop neighbor unseen for this long
            'route_expiry_ms': 300000,       # drop route to an origin after this
            'obs_prune_interval': 50,        # run expiry check every N observations

            # --- PDR / ETX / neuron-inspired weights ---
            'pdr_alpha': 0.3,                # EWMA factor for relay success/failure
            'pdr_floor': 0.05,               # PDR never below this (ETX stays finite)
            'pdr_init': 0.8,                 # optimistic start for unseen relays
            'weight_enabled': True,          # ablation: global weight reinforcement
            'weight_init': 0.5,
            'weight_lr_pos': 0.10,           # reinforcement on relay success
            'weight_lr_neg': 0.20,           # penalty on relay failure
            'weight_decay_lambda': 0.05,     # per-hour decay without observations

            # --- PRIMARY / SILENT BACKUP (unicast DM) ---
            'backup1_slots': 2.0,            # T1 = backup1_slots * (airtime+slot)
            'backup2_slots': 4.0,            # T2 = backup2_slots * (airtime+slot)
            'echo_timeout_ms': 12000,        # fixed watchdog timeout (used when
                                             # echo_timeout_factor <= 0)
            'echo_timeout_factor': 4.0,      # timeout = factor * packet airtime
                                             # (must cover MAC CW backoff + airtime
                                             #  on the busiest realistic hop)
            'switch_margin': 0.10,           # hysteresis: challenger must beat PRIMARY by this
            'backup_diversity_weight': 0.5,  # how much path-diversity matters for BACKUP pick
            'backups_enabled': True,         # ablation: silent backups on/off
            'max_hop_retries': 2,            # hop-level failover attempts before mini-flood
            'primary_bypass_dupe_cancel': False,  # ABLATED (OFF): letting the designated
                                             # primary keep its TX despite duplicates
                                             # helps linear (+8% reach) but collapses
                                             # bridge/hub (retransmission storms,
                                             # latency x10) — dupe-cancel is what
                                             # terminates waves. See REPORT.md.

            # --- broadcast self-election tiers (activation thresholds) ---
            'tau_primary': 0.75,             # score >= this -> immediate relay
            'tau_backup1': 0.55,             # defer T1, cancel on echo
            'tau_backup2': 0.40,             # defer T2, cancel on echo
            'inhibition_enabled': True,      # ablation: lateral inhibition (cancel on echo)
            'mpr_enabled': True,             # ablation: coverage/MPR logic on/off

            # --- score weights (relay ranking) ---
            'w_reliability': 0.35,
            'w_etx': 0.25,
            'w_rssi': 0.10,
            'w_stability': 0.10,
            'w_new_coverage': 0.15,
            'w_unique_coverage': 0.5,        # bridge protection boost (multiplier)

            # --- confidence ---
            'confidence_enabled': True,
            'conf_low': 0.33,                # below -> LOW (more redundancy)
            'conf_high': 0.66,               # above -> HIGH (aggressive suppression)
            'conf_obs_full': 60,             # observations for full confidence
                                             # (v0.3 fix: slower growth — early
                                             #  observations keep LOW/MEDIUM conf)
            'extra_redundancy_low_conf': 1,  # extra backups/relays at LOW confidence

            # --- fallback ---
            'mini_flood_ttl': 2,             # max TTL for mini-flood escalation
            'low_conf_ttl_bonus': 2,         # extra radius at LOW confidence

            # --- v0.5 probe-first (stale-triggered route discovery) ---
            'probe_enabled': True,           # probe before first DM on cold route
            'probe_ttl': 0,                  # 0 = full hopLimit flood
            'probe_timeout_ms': 8000,        # wait for response, then send anyway
            'probe_cooldown_ms': 30000,      # no re-probe for this dest within
            'probe_route_stale_ms': 120000,  # route older than this = stale
            'probe_len': 12,                 # probe payload bytes
            'probe_resp_len': 12,            # probe response payload bytes

            # --- v0.5 reach-first / linear guard ---
            'linear_guard': True,            # conditional PRIMARY dupe-ignore
            'hub_retry_limit': 1,            # retry budget shrinks near hub

            # --- v0.6 FieldMesh/MeshCore-inspired ---
            'position_scope': 'local',       # 'local' (never relay positions)
                                             #  | 'global' (relay like traffic)
            'eligibility_enabled': True,     # mobile/fatigue nodes not PRIMARY
            'velocity_walk_threshold': 1.5,  # m/s above which a node is "mobile"
            'eligibility_floor': 0.5,        # below -> never designated PRIMARY
            'client_repeat_enabled': True,   # last-resort emergency retransmission
            'client_repeat_max_per_min': 1,  # hard storm guard
            'emergency_enabled': True,       # SOS bypass (inhibition off)
            'emergency_ratio': 0.0,          # test hook: fraction marked SOS

            # --- v0.8 LPR (Local Potential Relay) ---
            'enable_lpr': False,             # ablation: potential-field scoring
            'lpr_decay_lambda': 0.05,
            'lpr_hysteresis': 0.1,
            'lpr_congestion_weight': 0.5,
            'lpr_battery_weight': 0.3,       # battery not modeled (documented)
            'lpr_uncertainty_weight': 0.3,
            'lpr_class_weight': 0.2,
            'lpr_score_weight': 0.5,
            'lpr_residual_probe': 1.5,       # residual -> probe trigger
            'lpr_mobility_weight': 0.05,     # RSSI-derivative mobility penalty

            # --- v0.9 Deterministic Ripple Flood ---
            'ripple_enabled': False,         # ABLATED (OFF): deterministic ripple delay
                                             #  + layer inhibition + echo-probe burst all
                                             #  regress dense (-18% reach); see REPORT.md
            'ripple_alpha': 0.5,             # hop-layer spacing (x base)
            'ripple_k': 10,                  # ID tie-breaker modulus
            'ripple_jitter_step_ms': 8.0,    # per-ID step (deterministic)

            # --- v0.9 CEF (Critical Epidemic Forwarding) ---
            'enable_cef': False,           # ABLATION: replace weak/med/strong by a
                                           #  marginal-gain threshold (CEF)
            'cef_theta_base': 0.7,
            'cef_theta_min': 0.2,
            'cef_theta_max': 2.0,
            'cef_collision_weight': 0.5,
            'cef_duplicate_weight': 0.6,
            'cef_reach_guard_min_gain': 0.15,
            'cef_suppress_dup_threshold': 2,   # after this many copies, no forward
            'cef_min_relays_b4_strong': 3,   # redundancy floor before strong suppression

            # --- v0.9 CEF tactical layer (Meshtastic role aware) ---
            'cef_role_cost': {              # role -> cost multiplier
                'ROUTER': 1.0,
                'ROUTER_CLIENT': 1.5,      # v0.10 audit: 2.0 was too high in dense
                'CLIENT_BASE': 5.0,
                'CLIENT_MUTE': 20.0,
                'SENSOR': 20.0,
                'UNKNOWN': 5.0,
            },
            'cef_class_weights': {          # packet class -> weight W_class
                'EMERGENCY': 10.0,
                'DM': 1.5,
                'BROADCAST': 1.0,
                'TELEMETRY': 0.2,
                'SENSOR': 0.2,
                'POSITION': 0.2,           # telemetry-like
            },
            'cef_A_ref_ms': 150.0,          # reference airtime for normalization
            'cef_gamma': 1.8,               # channel saturation exponent (v0.10: 1.5->1.8)
            'cef_blind_gain': 0.5,          # fallback when no knowledge
            'cef_k_suppress': 1.0,          # v0.12 B4: theta *= (1 + k*pressure)
            'cef_k_reach': 0.8,              # v0.12 B4: theta *= (1 - k*reach_guard)
            'cef_hash_k': 10,               # v0.12 B5: packet-hash slot modulus

            # --- v0.13 C1: frontier-aware additive theta (mode-gated) ---
            'cef_theta_mode': 'v012',        # 'v013' = additive frontier-aware
            'cef_k_redundancy': 0.4,          # v0.13: theta += k*suppression_pressure
            'cef_k_dense': 0.4,               # v0.13: theta += k*density_score (deg/ref)
            'cef_k_front': 0.5,               # v0.13: theta -= k*frontier_risk
            'cef_k_lowconf': 0.3,            # v0.13: theta -= k*(1-confidence)
            'cef_dense_degree_ref': 12,       # v0.13: degree normalization (B6 lesson)
            # --- v0.13 C3: tiered hash-slot jitter (burst de-correlation) ---
            'cef_slot_jitter': False,          # True = deterministic tiered slots
            'cef_slot_count': 8,                # PRIMARY 0-2 / BACKUP 3-5 / weak 6-7
            'cef_slot_width_ms': 15.0,          # slot width (8x15 = 0-105 ms span)
            # --- v0.13 C1b: frontier rescue (fall through to tiered echo-cancel) ---
            'cef_frontier_override': False,      # True = frontier nodes escape cef_cost
            'cef_frontier_threshold': 0.4,     # frontier_risk needed for rescue
            'cef_frontier_delay_mult': 3.0,    # C1c: rescued defers wait longer than
                                               # CEF forwards (deferred-time evidence)
            'cef_frontier_min_regret': 0.4,   # C1e: rescue needs local regret evidence
            'cef_frontier_min_checks': 3,     # C1e: min regret observations
            'cef_frontier_regret_alpha': 0.2, # C1e: regret EWMA alpha
            # v0.13 C4: FW+-style DRF hop bias (0 = legacy alpha*airtime) ---
            'ripple_hop_bias_ms': 0,          # >0 = fixed ms per hop (FW+: 20-60)
            # --- v0.14 D0: TOP-K bounded neighbor state (firmware RAM) ---
            'topk_enabled': False,            # False = ALL (current behavior)
            'topk_k': 8,                      # CORE budget
            'topk_protect': True,            # never evict frontier/bridge links
            'topk_w_pdr': 1.0,                # CORE rank: w_p*PDR
            'topk_w_stab': 0.5,               # CORE rank: w_s*Stability
            'topk_w_nov': 1.0,                # CORE rank: w_n*Novelty
            'topk_w_etx': 0.5,                # CORE rank: -w_e*ETX
            'topk_protect_mode': 'generous',  # 'strict' = bottleneck-segment only
            # --- v0.14: mobility self-demotion (frequent movers act CLIENT_MUTE) ---
            'mobility_enabled': False,        # observe the mobility score
            'mobility_demote': False,         # act as CLIENT_MUTE when score high
            'mobility_threshold': 0.5,        # demotion threshold
            'mobility_alpha': 0.2,            # score EWMA alpha
            'mobility_dist_norm_m': 300.0,    # own-position delta normalization
            # --- v0.14 D2-D4: minimal SHEPHERD-COLLECT (CEF <-> CEF_F4 by lease) ---
            'shepherd_enabled': False,        # OFF until validated
            'shepherd_lease_ms': 45000.0,      # COLLECT lease (30/60/120 sweep candidates)
            'shepherd_regret_threshold': 0.5, # sustained local regret triggers candidacy
            'shepherd_min_checks': 3,         # min regret observations
            'shepherd_max_degree': 16,        # ultra-dense silence guard (B6 lesson)
            'shepherd_timer_base_ms': 3000.0, # election timer (x (1.5 - score))
            'shepherd_scope_alt_max': 1.5,    # D4-quorum: lease valid only in thin segments
            'shepherd_rescue_budget': 4.0,     # D3-budget: rescue tokens per lease window
            'shepherd_rescue_regen_ms': 15000.0,  # D3-budget: lazy token regeneration
            # --- v0.14 CEF_BOOST: traffic-jam mode (sustained-evidence FSM) ---
            'cef_boost_enabled': False,
            'boost_chanutil_min': 0.30,       # real-LoRa congestion reference (firmware metric)
            'boost_busy_pkts_min': 20,        # packets HEARD per window (RX evidence; maps to real channelUtil)
            'boost_degree_min': 12,           # high local redundancy
            'boost_frontier_max': 0.3,        # low frontier risk
            'boost_stable_windows': 3,        # hysteresis: N consecutive jam windows
            'boost_dupe_ratio_min': 0.8,     # copies/forwards >= 0.8 = redundant
            # --- v0.14 N4: airtime token bucket (ms, AIMD, structural) ---
            'n4_enabled': False,               # OFF: packet-count D3 bucket stays
            'n4_base_tokens_ms': 3000.0,        # base bucket = airtime ms
            'n4_regen_period_ms': 30000.0,      # base regen period
            'n4_util_low': 0.30,               # below: regen x (1+alpha) (AIMD-AI)
            'n4_util_high': 0.60,              # above: bucket x beta (AIMD-MD)
            'n4_alpha': 0.5,                   # additive increase factor
            'n4_beta': 0.7,                    # multiplicative decrease factor
            'n4_struct_w': 1.0,               # frontier-risk cap multiplier weight
            # --- v0.14 N1: receiver-evidence census (CBB / N1a/b/c) ---
            'n1_enabled': False,              # replaces broadcast decision paths
            'n1_mode': 'fixed',               # fixed | degree | adaptive | ndb
            # --- v2 NodeDB analog (passive hops_away; firmware NodeDB.cpp) ---
            'ndb_enabled': False,       # master switch; off == baseline
            'ndb_max_entries': 64,      # bounded origin table (TOP-K discipline)
            'ndb_min_origins': 8,       # evidence floor before any profile decision
            'ndb_far_hops': 4,          # origin counts as "far" at >= this
            'ndb_far_ratio': 0.35,      # far-origins share -> chain shield
            'ndb_thin_n2_ratio': 1.5,   # unique-2hop < 1.5*degree -> thin shield
            'ndb_k2_shield': False,      # rank2-fallback K2 raised by thin/chain evidence
            'n1_k': 3,                        # CBB suppression threshold (copies)
            'n1_w_toa': 1.0,                  # census window in ToA multiples
            'n1_max_census_ids': 8,           # static census bound (nRF52840)
            'n1_max_pending': 64,             # static pending bound
            # --- v0.14 N3: ranked whisper (ToA windows; census shared with N1) ---
            'n3_enabled': False,
            'n3_gap_toa': 1.25,               # inter-rank gap in ToA multiples
            'n3_t0_frac': 0.25,               # RANK0 window in ToA multiples
            'n3_min_link_margin_db': 6.0,      # reliability floor (bounded edge)
            'n3_edge_margin_db': 12.0,         # weak/strong split for SNR policies
            'n3_k2': 2,                        # RANK2 fires only in near-silence
            # --- v0.14 N3: ranked whisper — forwarder ordering policy ---
            # designated: PRIMARY/B1 first (current AR). edge_first: low-SNR
            # forwarders earlier (= Meshtastic MT). strong_first: strong links
            # earlier (= MeshCore rxdelay). Evidence compare = E6.
            'n3_order_policy': 'designated',

            # --- v0.12b HERD layer (B2/B3; ablatable, OFF by default) ---
            'enable_herd': False,           # cohesion/order wired into decisions
            'herd_order_min_strong': 0.6,   # routing_order needed for STRONG inhib.
            'herd_order_min_medium': 0.35,  # routing_order needed for MEDIUM
            'herd_cohesion_weak_threshold': 0.6,  # cohesion -> +1 echo tolerance
            'herd_repulsion_jitter_weight': 30.0, # repulsion -> extra jitter ms
            'sf_gatekeeper': True,          # store & forward protection
            'zero_jitter_battery': True,    # no jitter for CLIENT_MUTE/SENSOR
            'asymmetric_rssi_min': -110,    # v0.11: -115 was too strict (bridge/hub
                                            #  have -100..-110 links that ARE asymmetric
            'asymmetric_pdr_max': 0.35,     # v0.11: 0.2 too strict; blind = weak rssi
                                            #  AND weak pdr (was blocking good failover)
            'asymmetric_block_fallback': True,  # don't mini-flood blind asym metrics

            # --- v0.10 fixes ---
            'sparse_guard': True,           # sparse/low-conf: WEAK + theta*0.6 + no CEF
            'sparse_degree_threshold': 5,   # local_degree <= this -> sparse guard
            'sparse_theta_scale': 0.6,      # theta *= this in sparse guard
            'require_two_copies_cancel': True,  # 2 independent copies before cancel
            'unique_coverage_bonus_extra': 0.2,  # +0.2 in activation score
            'cef_min_degree': 8,            # CEF gating: only at high local degree
            'cef_min_conf': 0.66,           # CEF gating: only at >= MEDIUM confidence
            'cef_jitter_max_ms': 100.0,     # v0.11: 50 -> 100 (dense coll +28% too high)
            'cef_jitter_step_ms': 5.0,      # per-ID deterministic step
            'linear_tx_budget': True,       # tx_per_delivered-aware TX reduction
            'tx_per_delivered_threshold': 1.2,  # above -> theta*1.1, retries 1

            # --- v0.9 Echo-Probe (cold-start 1-hop neighbor discovery) ---
            'echo_probe_enabled': False,     # ABLATED (OFF): cold-start ACK burst
                                             #  collides with first waves (dense -4.4pt)
            'echo_probe_period_ms': 30000,
            'probe_max_time_ms': 300000,     # stop probing after this
            'probe_ack_len': 8,              # PROBE_ACK payload bytes
            'fallback_fail_streak': 2,       # consecutive failures before mini-flood

            # --- density adaptation ---
            'density_sparse': 4,             # <= this many neighbors = sparse behavior
            'density_dense': 12,             # >= this = dense (aggressive suppression)

            # --- segmentation (local sectors) ---
            'segmentation': 'adaptive',      # 'adaptive' | 'off' | 'geo' | 'topo'
            'sector_count': 4,               # max sectors (geo mode; adaptive granularity)
            'seg_pos_ratio': 0.6,            # adaptive: geo when >= this fraction of
                                             #  neighbors has learned positions
            'seg_min_per_sector': 3,         # adaptive geo: >= this many positioned
                                             #  neighbors per sector
            'seg_overlap_threshold': 0.5,    # Jaccard >= => same topological segment
            'seg_recompute_s': 30,           # min interval between recomputes
            'seg_hysteresis': 0.05,          # min fraction of changed neighbors
                                             #  to apply re-segmentation

            # --- v0.3 NEIGHBORINFO (explicit local neighbor-table exchange) ---
            'ni_mode': 'passive_only',       # 'passive_only' | 'neighborinfo'
            'ni_min_period_ms': 60000,       # never send more often than this
            'ni_min_period_sparse_ms': 45000,  # sparse: NI is cheap here
            'ni_min_period_dense_ms': 180000,  # dense: full-table NI doubles
                                               #  airtime — slow down (measured)
            'ni_max_period_ms': 300000,      # always refresh at least this often
            'ni_max_neighbors': 12,          # size cap per NI (bridges first)
            'ni_view_mode': 'replace',       # 'replace' | 'union' | 'passive'
                                             #  (ablacja: union zawyża pokrycie)
            'ni_quality': True,              # quality bytes in NI (False = ID only)
            'ni_full_every': 5,              # periodic FULL resync every N NIs
            'ni_check_interval_ms': 5000,    # loop check interval (+seeded jitter)
            'ni_header_bytes': 16,           # NEIGHBORINFO header size (airtime cost)
            'ni_bytes_per_neighbor': 5,      # 4B id + 1B quantized quality
            'ni_expiry_ms': 300000,          # advertised info expires with this

            # --- v0.3 adaptive inhibition / functional segmentation ---
            'inhibition_strength_mode': 'adaptive',  # 'adaptive' | 'fixed_medium'
            'alt_paths_dense': 3,            # >= this many alts -> strong inhibition
            'alt_paths_sparse': 1,           # <= this -> weak inhibition (bridge-like)
            'unique_dupe_bypass': False,     # ABLATED: MAC dupe-cancel bypass for
                                             # unique-coverage nodes — breaks wave
                                             # termination (storms); protection is
                                             # provided post-defer instead
            'fatigue_enabled': False,        # ablation: homeostasis/relay_fatigue
            'fatigue_penalty': 0.10,         # max penalty in activation score
            'fatigue_decay_s': 60000,        # fatigue decays over this time

            # --- gossip variants (research question, sec. 28) ---
            'gossip_mode': 'none',           # 'none' | 'gossip' | 'backup_plus_gossip'
            'gossip_probability': 0.15,

            # --- refractory / anti-oscillation ---
            'refractory_enabled': False,     # experimental, ablate before adopting
            'refractory_ms': 30000,

            # RNG salt for AR-internal randomized jitter (seeded per node)
            'rng_salt': 1337,
        }

    @property
    def current_preset(self):
        """Returns the currently selected modem preset configuration"""
        return self.MODEM_PRESETS[self.MODEM_PRESET]

    # Function that needs to be run to ensure the router dependent variables change appropriately
    def update_router_dependencies(self):
        # Example: Overwrite hop limit in the case of X new awesome routing algorithm
        # if self.SELECTED_ROUTER_TYPE == self.ROUTER_TYPE.AWESOME_ROUTER:
        #     Change config values if necessary for your router here
        return

# single module-level config for all users to reference unambiguously
CONFIG = Config()
