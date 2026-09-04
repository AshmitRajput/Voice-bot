"""
utils/time_seconds.py — second-precision datetime formatting for admin display.

Django's default admin format (`DATETIME_FORMAT` / locale `P`) has NO
seconds token at all -- "Aug. 30, 2026, 6:53 p.m." is the best it can do
no matter what's actually stored (the DB value itself is full precision).
This gives admin.py an explicit formatter to opt specific fields into
seconds without touching global settings.
"""
from django.utils import timezone as dj_timezone


def _fmt_dt(value):
    """
    datetime -> "Aug. 30, 2026, 6:53:20 p.m." (local tz, seconds included).
    Returns "—" for None so admin list/detail views don't show blank cells.
    """
    if not value:
        return "—"
    local = dj_timezone.localtime(value)
    # %-I (no leading zero) is POSIX-only. Fall back to %I and strip a
    # leading zero manually so this also works unmodified on Windows.
    text = local.strftime("%b. %d, %Y, %I:%M:%S %p")
    text = text.replace(" 0", " ", 1) if text.split(", ")[-1].startswith("0") else text
    return text.replace("AM", "a.m.").replace("PM", "p.m.")


def epoch_offset_to_display(base_epoch, offset_seconds):
    """
    For ConversationTurn.turn_start_seconds/turn_end_seconds, which are
    stored as an offset from CallSession.started_at_epoch, not an absolute
    datetime. Reconstructs the absolute instant and formats it the same way.
    Returns "—" if either input is missing.
    """
    if base_epoch is None or offset_seconds is None:
        return "—"
    dt = dj_timezone.datetime.fromtimestamp(
        base_epoch + offset_seconds, tz=dj_timezone.utc
    )
    return _fmt_dt(dt)