"""Typed scan rejection codes (batch 2b item 7).

Scan intake used to collapse every failure into one opaque
"scan_rejected", so daily-quota exhaustion, a global exposure
budget stop, a server misconfiguration and a malformed request
were indistinguishable to the user and to diagnostics. The store
and service layers raise fixed, content-free messages (no user
input — intention text, image data — ever appears in them), so
this presentation-layer mapping keys on those stable prefixes.
"""


def scan_error_code(error):
    """The fixed API error code for one scan intake failure."""
    message = str(error)
    if message.startswith("Scan quota exhausted"):
        return "scan_quota_exhausted"
    if message.startswith("Scan exposure budget exhausted"):
        return "scan_budget_exhausted"
    if "is not configured" in message:
        return "scan_unavailable"
    return "scan_rejected"
