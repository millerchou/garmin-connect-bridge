"""Closed physical-field policy, manually reviewed against FIT Profile 21.218.0.

Adding an SDK field never changes this table. See FIT_SANITIZE_RULES.md.
Names document semantics only; decisions use numeric IDs, type, size and context.
"""

from dataclasses import dataclass

RULES_VERSION = "1.0.0"
PROFILE_VERSION = "21.218.0"
SDK_VERSION = "21.218.0"


@dataclass(frozen=True)
class Rule:
    name: str
    base: int
    minimum: int = 1
    maximum: int = 1


# Exactly one cycling session is required for all retained training fields.
# Arrays: power-phase endpoints / rider-position slots = 2; zones = 1..32.
RULES = {
    0: {  # file_id
        0: Rule("type", 0x00),
    },
    7: {  # zones_target
        1: Rule("max_heart_rate", 0x02),
        2: Rule("threshold_heart_rate", 0x02),
        3: Rule("functional_threshold_power", 0x84),
        5: Rule("hr_calc_type", 0x00),
        7: Rule("pwr_calc_type", 0x00),
    },
    8: {  # hr_zone
        1: Rule("high_bpm", 0x02),
        254: Rule("message_index", 0x84),
    },
    9: {  # power_zone
        1: Rule("high_value", 0x84),
        254: Rule("message_index", 0x84),
    },
    12: {  # sport
        0: Rule("sport", 0x00),
        1: Rule("sub_sport", 0x00),
    },
    18: {  # session
        0: Rule("event", 0x00),
        1: Rule("event_type", 0x00),
        2: Rule("start_time", 0x86),
        5: Rule("sport", 0x00),
        6: Rule("sub_sport", 0x00),
        7: Rule("total_elapsed_time", 0x86),
        8: Rule("total_timer_time", 0x86),
        9: Rule("total_distance", 0x86),
        10: Rule("total_cycles", 0x86),
        11: Rule("total_calories", 0x84),
        13: Rule("total_fat_calories", 0x84),
        14: Rule("avg_speed", 0x84),
        15: Rule("max_speed", 0x84),
        16: Rule("avg_heart_rate", 0x02),
        17: Rule("max_heart_rate", 0x02),
        18: Rule("avg_cadence", 0x02),
        19: Rule("max_cadence", 0x02),
        20: Rule("avg_power", 0x84),
        21: Rule("max_power", 0x84),
        22: Rule("total_ascent", 0x84),
        23: Rule("total_descent", 0x84),
        24: Rule("total_training_effect", 0x02),
        25: Rule("first_lap_index", 0x84),
        26: Rule("num_laps", 0x84),
        27: Rule("event_group", 0x02),
        28: Rule("trigger", 0x00),
        34: Rule("normalized_power", 0x84),
        35: Rule("training_stress_score", 0x84),
        36: Rule("intensity_factor", 0x84),
        37: Rule("left_right_balance", 0x84),
        45: Rule("threshold_power", 0x84),
        48: Rule("total_work", 0x86),
        57: Rule("avg_temperature", 0x01),
        58: Rule("max_temperature", 0x01),
        92: Rule("avg_fractional_cadence", 0x02),
        93: Rule("max_fractional_cadence", 0x02),
        94: Rule("total_fractional_cycles", 0x02),
        101: Rule("avg_left_torque_effectiveness", 0x02),
        102: Rule("avg_right_torque_effectiveness", 0x02),
        103: Rule("avg_left_pedal_smoothness", 0x02),
        104: Rule("avg_right_pedal_smoothness", 0x02),
        105: Rule("avg_combined_pedal_smoothness", 0x02),
        112: Rule("time_standing", 0x86),
        113: Rule("stand_count", 0x84),
        114: Rule("avg_left_pco", 0x01),
        115: Rule("avg_right_pco", 0x01),
        116: Rule("avg_left_power_phase", 0x02, 2, 2),
        117: Rule("avg_left_power_phase_peak", 0x02, 2, 2),
        118: Rule("avg_right_power_phase", 0x02, 2, 2),
        119: Rule("avg_right_power_phase_peak", 0x02, 2, 2),
        120: Rule("avg_power_position", 0x84, 2, 2),
        121: Rule("max_power_position", 0x84, 2, 2),
        122: Rule("avg_cadence_position", 0x02, 2, 2),
        123: Rule("max_cadence_position", 0x02, 2, 2),
        124: Rule("enhanced_avg_speed", 0x86),
        125: Rule("enhanced_max_speed", 0x86),
        137: Rule("total_anaerobic_training_effect", 0x02),
        139: Rule("avg_vam", 0x84),
        150: Rule("min_temperature", 0x01),
        168: Rule("training_load_peak", 0x85),
        169: Rule("enhanced_avg_respiration_rate", 0x84),
        170: Rule("enhanced_max_respiration_rate", 0x84),
        180: Rule("enhanced_min_respiration_rate", 0x84),
        181: Rule("total_grit", 0x88),
        183: Rule("jump_count", 0x84),
        187: Rule("avg_flow", 0x88),
        192: Rule("workout_feel", 0x02),
        193: Rule("workout_rpe", 0x02),
        253: Rule("timestamp", 0x86),
        254: Rule("message_index", 0x84),
    },
    19: {  # lap
        0: Rule("event", 0x00),
        1: Rule("event_type", 0x00),
        2: Rule("start_time", 0x86),
        7: Rule("total_elapsed_time", 0x86),
        8: Rule("total_timer_time", 0x86),
        9: Rule("total_distance", 0x86),
        10: Rule("total_cycles", 0x86),
        11: Rule("total_calories", 0x84),
        12: Rule("total_fat_calories", 0x84),
        13: Rule("avg_speed", 0x84),
        14: Rule("max_speed", 0x84),
        15: Rule("avg_heart_rate", 0x02),
        16: Rule("max_heart_rate", 0x02),
        17: Rule("avg_cadence", 0x02),
        18: Rule("max_cadence", 0x02),
        19: Rule("avg_power", 0x84),
        20: Rule("max_power", 0x84),
        21: Rule("total_ascent", 0x84),
        22: Rule("total_descent", 0x84),
        23: Rule("intensity", 0x00),
        25: Rule("sport", 0x00),
        33: Rule("normalized_power", 0x84),
        34: Rule("left_right_balance", 0x84),
        39: Rule("sub_sport", 0x00),
        41: Rule("total_work", 0x86),
        50: Rule("avg_temperature", 0x01),
        51: Rule("max_temperature", 0x01),
        80: Rule("avg_fractional_cadence", 0x02),
        81: Rule("max_fractional_cadence", 0x02),
        82: Rule("total_fractional_cycles", 0x02),
        91: Rule("avg_left_torque_effectiveness", 0x02),
        92: Rule("avg_right_torque_effectiveness", 0x02),
        93: Rule("avg_left_pedal_smoothness", 0x02),
        94: Rule("avg_right_pedal_smoothness", 0x02),
        95: Rule("avg_combined_pedal_smoothness", 0x02),
        98: Rule("time_standing", 0x86),
        99: Rule("stand_count", 0x84),
        100: Rule("avg_left_pco", 0x01),
        101: Rule("avg_right_pco", 0x01),
        102: Rule("avg_left_power_phase", 0x02, 2, 2),
        103: Rule("avg_left_power_phase_peak", 0x02, 2, 2),
        104: Rule("avg_right_power_phase", 0x02, 2, 2),
        105: Rule("avg_right_power_phase_peak", 0x02, 2, 2),
        106: Rule("avg_power_position", 0x84, 2, 2),
        107: Rule("max_power_position", 0x84, 2, 2),
        108: Rule("avg_cadence_position", 0x02, 2, 2),
        109: Rule("max_cadence_position", 0x02, 2, 2),
        110: Rule("enhanced_avg_speed", 0x86),
        111: Rule("enhanced_max_speed", 0x86),
        121: Rule("avg_vam", 0x84),
        124: Rule("min_temperature", 0x01),
        136: Rule("enhanced_avg_respiration_rate", 0x84),
        137: Rule("enhanced_max_respiration_rate", 0x84),
        149: Rule("total_grit", 0x88),
        151: Rule("jump_count", 0x84),
        154: Rule("avg_flow", 0x88),
        253: Rule("timestamp", 0x86),
        254: Rule("message_index", 0x84),
    },
    20: {  # record
        2: Rule("altitude", 0x84),
        3: Rule("heart_rate", 0x02),
        4: Rule("cadence", 0x02),
        5: Rule("distance", 0x86),
        6: Rule("speed", 0x84),
        7: Rule("power", 0x84),
        13: Rule("temperature", 0x01),
        29: Rule("accumulated_power", 0x86),
        30: Rule("left_right_balance", 0x02),
        43: Rule("left_torque_effectiveness", 0x02),
        44: Rule("right_torque_effectiveness", 0x02),
        45: Rule("left_pedal_smoothness", 0x02),
        46: Rule("right_pedal_smoothness", 0x02),
        53: Rule("fractional_cadence", 0x02),
        69: Rule("left_power_phase", 0x02, 2, 2),
        70: Rule("left_power_phase_peak", 0x02, 2, 2),
        71: Rule("right_power_phase", 0x02, 2, 2),
        72: Rule("right_power_phase_peak", 0x02, 2, 2),
        73: Rule("enhanced_speed", 0x86),
        78: Rule("enhanced_altitude", 0x86),
        108: Rule("enhanced_respiration_rate", 0x84),
        253: Rule("timestamp", 0x86),
    },
    21: {  # event
        0: Rule("event", 0x00),
        1: Rule("event_type", 0x00),
        3: Rule("data", 0x86),
        4: Rule("event_group", 0x02),
        253: Rule("timestamp", 0x86),
    },
    34: {  # activity
        0: Rule("total_timer_time", 0x86),
        1: Rule("num_sessions", 0x84),
        2: Rule("type", 0x00),
        3: Rule("event", 0x00),
        4: Rule("event_type", 0x00),
        5: Rule("local_timestamp", 0x86),
        6: Rule("event_group", 0x02),
        253: Rule("timestamp", 0x86),
    },
    216: {  # time_in_zone
        0: Rule("reference_mesg", 0x84),
        1: Rule("reference_index", 0x84),
        2: Rule("time_in_hr_zone", 0x86, 1, 32),
        5: Rule("time_in_power_zone", 0x86, 1, 32),
        6: Rule("hr_zone_high_boundary", 0x02, 1, 32),
        9: Rule("power_zone_high_boundary", 0x84, 1, 32),
        10: Rule("hr_calc_type", 0x00),
        11: Rule("max_heart_rate", 0x02),
        12: Rule("resting_heart_rate", 0x02),
        13: Rule("threshold_heart_rate", 0x02),
        14: Rule("pwr_calc_type", 0x00),
        15: Rule("functional_threshold_power", 0x84),
        253: Rule("timestamp", 0x86),
    },
}

# Explicitly deferred candidate. Other unapproved numbers are pending review.
DEFERRED = {(19, 40)}

# Known examples; absence here does NOT grant permission. No substring matching.
EXPLICIT_REMOVALS = {
    0: {1, 2, 3, 4, 5, 8},
    12: {3},
    18: {3, 4, 29, 30, 31, 32, 38, 39, 84, 110},
    19: {3, 4, 5, 6, 27, 28, 29, 30},
    20: {0, 1},
    142: {65},
    312: {17, 18, 19, 20},
}

# No device, profile, course, segment or split message is admitted.
EVENTS = frozenset({0, 8, 9, 13, 14, 15, 16, 17, 18, 19, 20, 23, 24, 25, 42, 43, 44})

# Closed cycling subtypes and contextual enums; invalid sentinels stay invalid.
CYCLING_SUBTYPES = frozenset(
    {0, 5, 6, 7, 8, 9, 10, 11, 12, 13, 28, 29, 46, 47, 48, 49, 58, 88, 123, 127}
)
ENUM_VALUES = {
    (7, 5): frozenset(range(4)),
    (7, 7): frozenset(range(2)),
    (216, 10): frozenset(range(4)),
    (216, 14): frozenset(range(2)),
    (18, 28): frozenset(range(4)),
    (19, 23): frozenset(range(7)),
    (34, 2): frozenset(range(2)),
    (12, 1): CYCLING_SUBTYPES,
    (18, 6): CYCLING_SUBTYPES,
    (19, 39): CYCLING_SUBTYPES,
}
