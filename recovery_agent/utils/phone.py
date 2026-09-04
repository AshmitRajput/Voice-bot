"""
voice_bot/utils/phone.py

Single source of truth for Indian phone-number normalization.

🔥 Every Customer.phone_number in the DB must end up as "+91XXXXXXXXXX"
(13 chars) -- not "XXXXXXXXXX", not "91XXXXXXXXXX", not anything with
spaces/dashes. Customer.save() (models.py), the admin form (admin.py),
and plivo_call()'s "to" input (views_voice.py) all funnel through this
ONE function, so storage, display, and outbound dialing never drift
out of sync with each other -- fix the format in one place, it's fixed
everywhere.
"""

import re


class PhoneNormalizationError(ValueError):
    """Raised when a value can't be normalized to +91XXXXXXXXXX."""


def normalize_indian_phone(raw: str) -> str:
    """
    Accepts:
      - a plain 10-digit number    ("8118860799")
      - 12 digits with country code, no plus ("918118860799")
      - separators/spaces          ("81188 60799", "8118-860799")
      - an already-normalized value ("+918118860799") -- idempotent,
        safe to call again on something this function already returned
        (needed because Customer.save() normalizes unconditionally,
        even when the value already came out of the admin form's
        clean() step normalized).

    Always returns "+91XXXXXXXXXX".

    Raises PhoneNormalizationError for anything that isn't a valid
    10-digit Indian mobile number underneath -- callers decide how to
    surface that (a form ValidationError, an API 400, a log + skip).
    """
    if not raw or not str(raw).strip():
        raise PhoneNormalizationError("phone number is required")

    digits = re.sub(r"\D", "", str(raw))

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 13 and digits.startswith("091"):
        # rare copy-paste artifact: trunk "0" + "91" + 10 digits
        digits = digits[3:]

    if len(digits) != 10:
        raise PhoneNormalizationError(
            f"'{raw}' is not a valid 10-digit Indian mobile number"
        )

    return f"+91{digits}"