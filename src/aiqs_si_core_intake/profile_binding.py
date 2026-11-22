"""Observer-coordinate binding — PUBLIC STUB.

The production binding derives the observer coordinate from target
profiles with a keyed, salted construction that is part of the
proprietary intake and is never published. This stub returns a
deterministic synthetic coordinate: a plain SHA-256 over the
profile identifiers with a stub marker. It is NOT the production
construction and must not be used to compare, reproduce or
approximate production coordinates.
"""
import hashlib
import json


def build_observer_coordinate(profiles, *, salt=None, image_thumb_bytes=None):
    """Return a synthetic stub coordinate for the given profiles."""
    identifiers = []
    for profile in profiles or []:
        if isinstance(profile, dict):
            identifiers.append(str(profile.get("id") or ""))
    material = json.dumps(identifiers, separators=(",", ":")).encode()
    digest = hashlib.sha256(b"aiqs-si-open-stub-coordinate\0" + material).hexdigest()
    return {"bundle_hmac": digest, "stub": True,
            "profile_count": len(identifiers)}
