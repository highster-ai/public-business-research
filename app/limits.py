"""Shared, release-tested limits for every lead-generation entry point."""

MAX_COUNT = 60
COUNT_CAP_ERROR = f"count must be between 1 and {MAX_COUNT}"


def count_error():
    """Return the canonical cap message."""
    return COUNT_CAP_ERROR


def validate_count(value):
    """Return a valid business count or raise the canonical public error."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("count must be an integer")
    if value < 1 or value > MAX_COUNT:
        raise ValueError(COUNT_CAP_ERROR)
    return value
