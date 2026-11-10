import math


def format_timestamp(seconds):
    """Human-readable whole seconds, with hours that do not wrap at 24."""
    try:
        value = float(seconds)
        if not math.isfinite(value):
            return "--:--:--"
        total = max(0, math.floor(value))
    except (TypeError, ValueError, OverflowError):
        return "--:--:--"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
