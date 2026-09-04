"""
Validates that response_text follows the mixed-script rule -- Devanagari
Hindi by default, with a fixed set of terms kept in Latin script.

Rewritten vs previous version: ALLOWED_PROPER_NOUNS was the full Honda
vehicle lineup (Activa, Shine, Dio, Unicorn, SP125, Livo, Grazia,
Hornet, CB350) plus "aarohi" hardcoded. None of that applies to a
revenue-recovery agent. Replaced with recovery-domain terms; persona
name is now pulled from context (falls back to "riya") instead of
hardcoded, so it stays correct if the admin renames the persona.
"""

import json
import re

# Loanwords the persona keeps in Latin script even inside Devanagari text.
ALLOWED_LATIN_TERMS = {
    "emi", "upi", "payment", "link", "sms", "whatsapp", "email",
    "ok", "due", "call", "team", "customer", "manager", "account",
    "case", "id", "otp",
}

# Proper nouns that are always Latin. Persona name is added dynamically
# per-call via _build_allowed (context["persona_name"]); this is only the
# static fallback in case context doesn't carry one.
ALLOWED_PROPER_NOUNS = {
    "riya",
}

DEVANAGARI_RANGE = re.compile(r"[\u0900-\u097F]")
LATIN_RUN = re.compile(r"[A-Za-z]+")


def extract_response_text(payload):
    """
    Accepts a raw JSON string, a dict, or an already-extracted string, and
    returns just the spoken line. Exists because running the checker on raw
    JSON produces meaningless violations from the schema's own field names.
    """
    if isinstance(payload, dict):
        return payload.get("response_text", "")
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{"):
            try:
                return json.loads(stripped).get("response_text", "")
            except json.JSONDecodeError:
                return payload
        return payload
    return str(payload)


def _build_allowed(context=None):
    allowed = set(ALLOWED_LATIN_TERMS) | set(ALLOWED_PROPER_NOUNS)
    if context:
        # Per-call values. The customer's name and persona name vary, so
        # they can never be static allowlist entries -- without this the
        # checker flags every correctly-personalised response.
        for key in ("customer_name", "persona_name"):
            value = context.get(key)
            if value:
                allowed.add(str(value).lower())
                for token in str(value).split():
                    allowed.add(token.lower())
    return allowed


def check_response(payload, context=None) -> dict:
    """
    Returns a dict describing whether the spoken line follows the
    mixed-script rule. `context` is the same recovery-call context dict
    passed to build_turn_input, used to whitelist per-call proper nouns.
    """
    response_text = extract_response_text(payload)

    has_devanagari = bool(DEVANAGARI_RANGE.search(response_text))

    remaining = response_text
    for phrase in sorted(_build_allowed(context), key=len, reverse=True):
        remaining = re.sub(rf"\b{re.escape(phrase)}\b", " ", remaining,
                           flags=re.IGNORECASE)

    unexpected_latin = sorted(set(LATIN_RUN.findall(remaining)))

    return {
        "has_devanagari": has_devanagari,
        "unexpected_latin_terms": unexpected_latin,
        "likely_violation": (not has_devanagari) or bool(unexpected_latin),
        "text": response_text,
    }


def check_batch(payloads: list, contexts: list = None) -> dict:
    """
    payloads: list of response_text strings (or raw payloads).
    contexts: optional matching list of call context dicts, so per-call
              names are whitelisted correctly.
    """
    contexts = contexts or [None] * len(payloads)
    results = [check_response(p, c) for p, c in zip(payloads, contexts)]
    violations = [r for r in results if r["likely_violation"]]
    return {
        "total": len(results),
        "violations": len(violations),
        "violation_rate": round(len(violations) / len(results), 3) if results else 0,
        "details": violations,
    }