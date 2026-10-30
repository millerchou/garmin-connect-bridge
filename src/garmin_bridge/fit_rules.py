"""Reviewed messages permitted to anchor compressed FIT timestamps."""

# Unknown messages cannot seed the clock, regardless of parser Profile recognition.
TIMESTAMP_MESSAGES = frozenset([18, 19, 20, 21, 23, 34, 216, 313, 375])
