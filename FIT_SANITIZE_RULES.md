# Physical FIT policy 1.0.0

Reviewed against official Garmin FIT Python SDK/Profile **21.218.0**. This is a
closed list of field numbers and exact wire base types; Profile recognition is
not permission. All rows require one cycling session (sport 2). Scalar fields
have exactly one element. Pair arrays have exactly two; zone arrays have 1–32.
Original bytes, including invalid sentinels, are retained; scale/offset below
describe the Profile and are **not** applied during transcription.

Core candidates: **152**. Candidate lap 19:40 (`num_active_lengths`) is deferred:
its swimming-length context is outside this cycling-only version. The other
151 are eligible subject to type, size, context and reference checks.

## Semantic and dependency review

The eligible core fields describe timing, sport, distance, speed, altitude, ascent,
work, calories, HR, cadence, power, temperature, breathing, pedalling dynamics,
training estimates and subjective numeric workout ratings. None is a geographic
coordinate, device/account identifier or free text. These remain private health
and training data: retaining altitude, exact time and physiology is **not anonymity**.
Training effect/load, normalized power, TSS, intensity factor, grit/flow and ratings
remain Garmin/source-reported values, not independently verified measurements.

The table below also records the individually reviewed additional fields:

- `file_id 0:0` only: file type must be activity (4). Manufacturer, product, serial,
  creation time, file number and product text are not needed to retain training
  semantics and are removed. No identity or replacement device is invented.
- `sport 12:0-1` and session/lap sport fields provide cycling context. Exactly one
  session with sport 2 is required. A lap explicitly declaring another sport
  rejects the file; a separate non-cycling sport message is removed. Subtypes are
  a closed list in `CYCLING_SUBTYPES`, not arbitrary future enum values.
- `session 18:254,25,26`, `lap 19:254`: retain physical indices and counts. The
  index mask is `0x0fff`, with only the `0x8000` selected flag additionally allowed.
  Duplicate/reserved indices cannot be reference targets. A valid first-lap/count
  pair must describe all retained laps with unique consecutive indices; otherwise
  both fields are removed. No laps are reindexed. Explicit invalid index sentinels
  stay invalid but cannot be reference targets. Invalid/absent first/count fields
  are not filled; a partially valid pair is removed and reported. With no laps,
  a real zero count is retained; any valid first-lap pointer is removed because
  no target exists. An explicitly invalid first-lap sentinel remains invalid.
- `activity 34:0-6,253`: file timer, session count, activity/event types, timestamps
  and event group. A valid session count must be 1. Original local timestamp is
  preserved without looking up a timezone. Session/lap/activity event context must
  match event 8/9/26; invalid sentinels are preserved, inconsistent context removed.
- `session 18:14-15`, `lap 19:13-14`, `record 20:2,6` are original speed/altitude
  encodings. Their enhanced components are **not** generated. If both original and
  enhanced physical fields occur, both original encodings are retained separately.
- `record 20:69-72` and the core session/lap power-phase arrays are angular pedal
  phase endpoints, not map coordinates. PCO fields measure pedal centre offset;
  power/cadence `position` arrays describe rider posture. The first implementation
  accepts exactly two entries for these arrays. Other array lengths are explicitly
  deferred (`array_shape`), not shortened, reordered, averaged or padded.
- `zones_target 7`, `hr_zone 8:1,254`, `power_zone 9:1,254` retain only numeric
  file-provided configuration and zone indices, with names removed. At most one
  zones_target snapshot is accepted; multiple snapshots remove it and the global
  HR/power zone messages as ambiguous. Global zone indices must be unique, 0–31.
  The configuration is only evidence contained in this historical activity file;
  it is not a current athlete profile, a claim of historically correct calibration,
  or a reason to populate missing fields in another record.
- `time_in_zone 216:0-1` must refer to a surviving unique session (18) or lap (19),
  using plain index bits (0–4095). An absent/invalid/unsupported/removed target
  removes the whole zone message, including its dependent arrays. HR/power duration
  and boundary arrays retain their original FIT zone-index order and invalid
  elements, 1–32 elements per field; arrays are never zipped together, relabelled,
  or assigned thresholds from another message. Missing boundaries/configuration
  remain missing. Inline thresholds/calculation enums are file-provided historical
  context only. Speed/cadence-zone arrays are not approved in this version.
- `event 21:0-1,4,253` provide a closed set of training-event types, event group and
  timestamp. Events outside `EVENTS` are removed altogether. Valid event_type is
  0–9; an invalid sentinel is retained but prevents payload retention. `event 21:3`
  is retained only for timer (event 0: data 0/1/2 or invalid), rider position
  (event 44: data 0–3 or invalid), or front/rear gear change (events 42/43: the
  original four packed 8-bit gear components). No component-derived fields are
  written. All other `data` contexts and all `data16` fields are removed/reported.
  Course-point indices and opaque event payloads are never forwarded.

Rules compare numeric message/field IDs, exact base type and byte-array shape
before inspecting any context value. Unknown/developer fields are opaque input
bytes; neither their value nor any developer-provided name is decoded or reported.
The extra enum constraints in `ENUM_VALUES` reject newly introduced meanings
without silently broadening approval. Invalid sentinels are not confused with 0.
Personal profiles, device settings, sensor identity, segment/split messages,
UUIDs and arbitrary text have no message-wide approval.

Explicit regressions include `lap 19:27-30`, `split 312:17-20`, and
`segment_lap 142:65` (UUID), as well as record/session coordinates and bounding
boxes. The explicit-removal examples are **not** the policy: anything absent from
the allowlist is removed even when its name has no sensitive substring. Conversely,
a non-geographic `position` name is not automatically forbidden.

Fixed reason codes are `approved`, `explicit_private_field`, `unreviewed_message`,
`unreviewed_field`, `developer_field`, `length_context`, `wire_type`, `array_shape`,
`event_context`, `event_payload`, `dependency_removed`, `ambiguous_context`,
`sport_context`, and `enum_context`. Reports include only numeric field identities,
counts and these constants. Unknown numbers/developer fields/type or shape
mismatches are marked pending review; they are still removed. Dependencies are
resolved conservatively and never fabricated. Rebuilding framing/CRC and
expanding supported compressed timestamps changes file representation but does
not apply the scale/offset below to physical field bytes. Absolute timestamp
anchors must themselves match an approved message/field/type; an unknown or
mistyped field 253 invalidates the anchor. A subsequent compressed record fails
until a valid approved absolute anchor appears, rather than propagating unknown
payload bytes or guessing an earlier timestamp.

## Complete physical table

| Message:field | Semantic name | Base type | Elements | Scale / offset | Scope |
| --- | --- | --- | --- | --- | --- |
| 0:0 | `type` | `enum` | 1 | 1 / 0 | additional context/training |
| 7:1 | `max_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 7:2 | `threshold_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 7:3 | `functional_threshold_power` | `uint16` | 1 | 1 / 0 | core |
| 7:5 | `hr_calc_type` | `enum` | 1 | 1 / 0 | core |
| 7:7 | `pwr_calc_type` | `enum` | 1 | 1 / 0 | core |
| 8:1 | `high_bpm` | `uint8` | 1 | 1 / 0 | additional context/training |
| 8:254 | `message_index` | `uint16` | 1 | 1 / 0 | additional context/training |
| 9:1 | `high_value` | `uint16` | 1 | 1 / 0 | additional context/training |
| 9:254 | `message_index` | `uint16` | 1 | 1 / 0 | additional context/training |
| 12:0 | `sport` | `enum` | 1 | 1 / 0 | additional context/training |
| 12:1 | `sub_sport` | `enum` | 1 | 1 / 0 | additional context/training |
| 18:0 | `event` | `enum` | 1 | 1 / 0 | additional context/training |
| 18:1 | `event_type` | `enum` | 1 | 1 / 0 | additional context/training |
| 18:2 | `start_time` | `uint32` | 1 | 1 / 0 | core |
| 18:5 | `sport` | `enum` | 1 | 1 / 0 | core |
| 18:6 | `sub_sport` | `enum` | 1 | 1 / 0 | core |
| 18:7 | `total_elapsed_time` | `uint32` | 1 | 1000 / 0 | core |
| 18:8 | `total_timer_time` | `uint32` | 1 | 1000 / 0 | core |
| 18:9 | `total_distance` | `uint32` | 1 | 100 / 0 | core |
| 18:10 | `total_cycles` | `uint32` | 1 | 1 / 0 | core |
| 18:11 | `total_calories` | `uint16` | 1 | 1 / 0 | core |
| 18:13 | `total_fat_calories` | `uint16` | 1 | 1 / 0 | core |
| 18:14 | `avg_speed` | `uint16` | 1 | 1000 / 0 | additional context/training |
| 18:15 | `max_speed` | `uint16` | 1 | 1000 / 0 | additional context/training |
| 18:16 | `avg_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 18:17 | `max_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 18:18 | `avg_cadence` | `uint8` | 1 | 1 / 0 | core |
| 18:19 | `max_cadence` | `uint8` | 1 | 1 / 0 | core |
| 18:20 | `avg_power` | `uint16` | 1 | 1 / 0 | core |
| 18:21 | `max_power` | `uint16` | 1 | 1 / 0 | core |
| 18:22 | `total_ascent` | `uint16` | 1 | 1 / 0 | core |
| 18:23 | `total_descent` | `uint16` | 1 | 1 / 0 | core |
| 18:24 | `total_training_effect` | `uint8` | 1 | 10 / 0 | core |
| 18:25 | `first_lap_index` | `uint16` | 1 | 1 / 0 | additional context/training |
| 18:26 | `num_laps` | `uint16` | 1 | 1 / 0 | additional context/training |
| 18:27 | `event_group` | `uint8` | 1 | 1 / 0 | additional context/training |
| 18:28 | `trigger` | `enum` | 1 | 1 / 0 | additional context/training |
| 18:34 | `normalized_power` | `uint16` | 1 | 1 / 0 | core |
| 18:35 | `training_stress_score` | `uint16` | 1 | 10 / 0 | core |
| 18:36 | `intensity_factor` | `uint16` | 1 | 1000 / 0 | core |
| 18:37 | `left_right_balance` | `uint16` | 1 | 1 / 0 | core |
| 18:45 | `threshold_power` | `uint16` | 1 | 1 / 0 | core |
| 18:48 | `total_work` | `uint32` | 1 | 1 / 0 | core |
| 18:57 | `avg_temperature` | `sint8` | 1 | 1 / 0 | core |
| 18:58 | `max_temperature` | `sint8` | 1 | 1 / 0 | core |
| 18:92 | `avg_fractional_cadence` | `uint8` | 1 | 128 / 0 | core |
| 18:93 | `max_fractional_cadence` | `uint8` | 1 | 128 / 0 | core |
| 18:94 | `total_fractional_cycles` | `uint8` | 1 | 128 / 0 | core |
| 18:101 | `avg_left_torque_effectiveness` | `uint8` | 1 | 2 / 0 | core |
| 18:102 | `avg_right_torque_effectiveness` | `uint8` | 1 | 2 / 0 | core |
| 18:103 | `avg_left_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 18:104 | `avg_right_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 18:105 | `avg_combined_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 18:112 | `time_standing` | `uint32` | 1 | 1000 / 0 | core |
| 18:113 | `stand_count` | `uint16` | 1 | 1 / 0 | core |
| 18:114 | `avg_left_pco` | `sint8` | 1 | 1 / 0 | core |
| 18:115 | `avg_right_pco` | `sint8` | 1 | 1 / 0 | core |
| 18:116 | `avg_left_power_phase` | `uint8` | 2 | 0.7111111 / 0 | core |
| 18:117 | `avg_left_power_phase_peak` | `uint8` | 2 | 0.7111111 / 0 | core |
| 18:118 | `avg_right_power_phase` | `uint8` | 2 | 0.7111111 / 0 | core |
| 18:119 | `avg_right_power_phase_peak` | `uint8` | 2 | 0.7111111 / 0 | core |
| 18:120 | `avg_power_position` | `uint16` | 2 | 1 / 0 | core |
| 18:121 | `max_power_position` | `uint16` | 2 | 1 / 0 | core |
| 18:122 | `avg_cadence_position` | `uint8` | 2 | 1 / 0 | core |
| 18:123 | `max_cadence_position` | `uint8` | 2 | 1 / 0 | core |
| 18:124 | `enhanced_avg_speed` | `uint32` | 1 | 1000 / 0 | core |
| 18:125 | `enhanced_max_speed` | `uint32` | 1 | 1000 / 0 | core |
| 18:137 | `total_anaerobic_training_effect` | `uint8` | 1 | 10 / 0 | core |
| 18:139 | `avg_vam` | `uint16` | 1 | 1000 / 0 | core |
| 18:150 | `min_temperature` | `sint8` | 1 | 1 / 0 | core |
| 18:168 | `training_load_peak` | `sint32` | 1 | 65536 / 0 | core |
| 18:169 | `enhanced_avg_respiration_rate` | `uint16` | 1 | 100 / 0 | core |
| 18:170 | `enhanced_max_respiration_rate` | `uint16` | 1 | 100 / 0 | core |
| 18:180 | `enhanced_min_respiration_rate` | `uint16` | 1 | 100 / 0 | core |
| 18:181 | `total_grit` | `float32` | 1 | 1 / 0 | core |
| 18:183 | `jump_count` | `uint16` | 1 | 1 / 0 | core |
| 18:187 | `avg_flow` | `float32` | 1 | 1 / 0 | core |
| 18:192 | `workout_feel` | `uint8` | 1 | 1 / 0 | core |
| 18:193 | `workout_rpe` | `uint8` | 1 | 1 / 0 | core |
| 18:253 | `timestamp` | `uint32` | 1 | 1 / 0 | core |
| 18:254 | `message_index` | `uint16` | 1 | 1 / 0 | additional context/training |
| 19:0 | `event` | `enum` | 1 | 1 / 0 | additional context/training |
| 19:1 | `event_type` | `enum` | 1 | 1 / 0 | additional context/training |
| 19:2 | `start_time` | `uint32` | 1 | 1 / 0 | core |
| 19:7 | `total_elapsed_time` | `uint32` | 1 | 1000 / 0 | core |
| 19:8 | `total_timer_time` | `uint32` | 1 | 1000 / 0 | core |
| 19:9 | `total_distance` | `uint32` | 1 | 100 / 0 | core |
| 19:10 | `total_cycles` | `uint32` | 1 | 1 / 0 | core |
| 19:11 | `total_calories` | `uint16` | 1 | 1 / 0 | core |
| 19:12 | `total_fat_calories` | `uint16` | 1 | 1 / 0 | core |
| 19:13 | `avg_speed` | `uint16` | 1 | 1000 / 0 | additional context/training |
| 19:14 | `max_speed` | `uint16` | 1 | 1000 / 0 | additional context/training |
| 19:15 | `avg_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 19:16 | `max_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 19:17 | `avg_cadence` | `uint8` | 1 | 1 / 0 | core |
| 19:18 | `max_cadence` | `uint8` | 1 | 1 / 0 | core |
| 19:19 | `avg_power` | `uint16` | 1 | 1 / 0 | core |
| 19:20 | `max_power` | `uint16` | 1 | 1 / 0 | core |
| 19:21 | `total_ascent` | `uint16` | 1 | 1 / 0 | core |
| 19:22 | `total_descent` | `uint16` | 1 | 1 / 0 | core |
| 19:23 | `intensity` | `enum` | 1 | 1 / 0 | core |
| 19:25 | `sport` | `enum` | 1 | 1 / 0 | core |
| 19:33 | `normalized_power` | `uint16` | 1 | 1 / 0 | core |
| 19:34 | `left_right_balance` | `uint16` | 1 | 1 / 0 | core |
| 19:39 | `sub_sport` | `enum` | 1 | 1 / 0 | core |
| 19:40 | `num_active_lengths` | `uint16` | 1 | 1 / 0 | deferred: length_context |
| 19:41 | `total_work` | `uint32` | 1 | 1 / 0 | core |
| 19:50 | `avg_temperature` | `sint8` | 1 | 1 / 0 | core |
| 19:51 | `max_temperature` | `sint8` | 1 | 1 / 0 | core |
| 19:80 | `avg_fractional_cadence` | `uint8` | 1 | 128 / 0 | core |
| 19:81 | `max_fractional_cadence` | `uint8` | 1 | 128 / 0 | core |
| 19:82 | `total_fractional_cycles` | `uint8` | 1 | 128 / 0 | core |
| 19:91 | `avg_left_torque_effectiveness` | `uint8` | 1 | 2 / 0 | core |
| 19:92 | `avg_right_torque_effectiveness` | `uint8` | 1 | 2 / 0 | core |
| 19:93 | `avg_left_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 19:94 | `avg_right_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 19:95 | `avg_combined_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 19:98 | `time_standing` | `uint32` | 1 | 1000 / 0 | core |
| 19:99 | `stand_count` | `uint16` | 1 | 1 / 0 | core |
| 19:100 | `avg_left_pco` | `sint8` | 1 | 1 / 0 | core |
| 19:101 | `avg_right_pco` | `sint8` | 1 | 1 / 0 | core |
| 19:102 | `avg_left_power_phase` | `uint8` | 2 | 0.7111111 / 0 | core |
| 19:103 | `avg_left_power_phase_peak` | `uint8` | 2 | 0.7111111 / 0 | core |
| 19:104 | `avg_right_power_phase` | `uint8` | 2 | 0.7111111 / 0 | core |
| 19:105 | `avg_right_power_phase_peak` | `uint8` | 2 | 0.7111111 / 0 | core |
| 19:106 | `avg_power_position` | `uint16` | 2 | 1 / 0 | core |
| 19:107 | `max_power_position` | `uint16` | 2 | 1 / 0 | core |
| 19:108 | `avg_cadence_position` | `uint8` | 2 | 1 / 0 | core |
| 19:109 | `max_cadence_position` | `uint8` | 2 | 1 / 0 | core |
| 19:110 | `enhanced_avg_speed` | `uint32` | 1 | 1000 / 0 | core |
| 19:111 | `enhanced_max_speed` | `uint32` | 1 | 1000 / 0 | core |
| 19:121 | `avg_vam` | `uint16` | 1 | 1000 / 0 | core |
| 19:124 | `min_temperature` | `sint8` | 1 | 1 / 0 | core |
| 19:136 | `enhanced_avg_respiration_rate` | `uint16` | 1 | 100 / 0 | core |
| 19:137 | `enhanced_max_respiration_rate` | `uint16` | 1 | 100 / 0 | core |
| 19:149 | `total_grit` | `float32` | 1 | 1 / 0 | core |
| 19:151 | `jump_count` | `uint16` | 1 | 1 / 0 | core |
| 19:154 | `avg_flow` | `float32` | 1 | 1 / 0 | core |
| 19:253 | `timestamp` | `uint32` | 1 | 1 / 0 | core |
| 19:254 | `message_index` | `uint16` | 1 | 1 / 0 | additional context/training |
| 20:2 | `altitude` | `uint16` | 1 | 5 / 500 | additional context/training |
| 20:3 | `heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 20:4 | `cadence` | `uint8` | 1 | 1 / 0 | core |
| 20:5 | `distance` | `uint32` | 1 | 100 / 0 | core |
| 20:6 | `speed` | `uint16` | 1 | 1000 / 0 | additional context/training |
| 20:7 | `power` | `uint16` | 1 | 1 / 0 | core |
| 20:13 | `temperature` | `sint8` | 1 | 1 / 0 | core |
| 20:29 | `accumulated_power` | `uint32` | 1 | 1 / 0 | core |
| 20:30 | `left_right_balance` | `uint8` | 1 | 1 / 0 | core |
| 20:43 | `left_torque_effectiveness` | `uint8` | 1 | 2 / 0 | core |
| 20:44 | `right_torque_effectiveness` | `uint8` | 1 | 2 / 0 | core |
| 20:45 | `left_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 20:46 | `right_pedal_smoothness` | `uint8` | 1 | 2 / 0 | core |
| 20:53 | `fractional_cadence` | `uint8` | 1 | 128 / 0 | core |
| 20:69 | `left_power_phase` | `uint8` | 2 | 0.7111111 / 0 | additional context/training |
| 20:70 | `left_power_phase_peak` | `uint8` | 2 | 0.7111111 / 0 | additional context/training |
| 20:71 | `right_power_phase` | `uint8` | 2 | 0.7111111 / 0 | additional context/training |
| 20:72 | `right_power_phase_peak` | `uint8` | 2 | 0.7111111 / 0 | additional context/training |
| 20:73 | `enhanced_speed` | `uint32` | 1 | 1000 / 0 | core |
| 20:78 | `enhanced_altitude` | `uint32` | 1 | 5 / 500 | core |
| 20:108 | `enhanced_respiration_rate` | `uint16` | 1 | 100 / 0 | core |
| 20:253 | `timestamp` | `uint32` | 1 | 1 / 0 | core |
| 21:0 | `event` | `enum` | 1 | 1 / 0 | core |
| 21:1 | `event_type` | `enum` | 1 | 1 / 0 | core |
| 21:3 | `data` | `uint32` | 1 | 1 / 0 | additional context/training |
| 21:4 | `event_group` | `uint8` | 1 | 1 / 0 | additional context/training |
| 21:253 | `timestamp` | `uint32` | 1 | 1 / 0 | core |
| 34:0 | `total_timer_time` | `uint32` | 1 | 1000 / 0 | additional context/training |
| 34:1 | `num_sessions` | `uint16` | 1 | 1 / 0 | additional context/training |
| 34:2 | `type` | `enum` | 1 | 1 / 0 | additional context/training |
| 34:3 | `event` | `enum` | 1 | 1 / 0 | additional context/training |
| 34:4 | `event_type` | `enum` | 1 | 1 / 0 | additional context/training |
| 34:5 | `local_timestamp` | `uint32` | 1 | 1 / 0 | additional context/training |
| 34:6 | `event_group` | `uint8` | 1 | 1 / 0 | additional context/training |
| 34:253 | `timestamp` | `uint32` | 1 | 1 / 0 | additional context/training |
| 216:0 | `reference_mesg` | `uint16` | 1 | 1 / 0 | core |
| 216:1 | `reference_index` | `uint16` | 1 | 1 / 0 | core |
| 216:2 | `time_in_hr_zone` | `uint32` | 1–32 | 1000 / 0 | core |
| 216:5 | `time_in_power_zone` | `uint32` | 1–32 | 1000 / 0 | core |
| 216:6 | `hr_zone_high_boundary` | `uint8` | 1–32 | 1 / 0 | core |
| 216:9 | `power_zone_high_boundary` | `uint16` | 1–32 | 1 / 0 | core |
| 216:10 | `hr_calc_type` | `enum` | 1 | 1 / 0 | core |
| 216:11 | `max_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 216:12 | `resting_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 216:13 | `threshold_heart_rate` | `uint8` | 1 | 1 / 0 | core |
| 216:14 | `pwr_calc_type` | `enum` | 1 | 1 / 0 | core |
| 216:15 | `functional_threshold_power` | `uint16` | 1 | 1 / 0 | core |
| 216:253 | `timestamp` | `uint32` | 1 | 1 / 0 | core |
