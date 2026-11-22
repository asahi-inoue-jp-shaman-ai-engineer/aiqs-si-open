"""Prayer input handling — PUBLIC STUB.

The production module validates the prayer contract and composes
the self-love affirmation statement; that logic is part of the
proprietary intake. The stubs here enforce only the container
shape (a job payload is a mapping) and pass the affirmation input
through unchanged, so shell behaviour stays deterministic and
testable without the core.
"""


def validate_prayer(payload):
    """Stub validation: the payload must be a mapping.

    The production validator enforces the full prayer contract and
    raises ValueError on any violation; this stub intentionally
    checks nothing beyond the container type.
    """
    if not isinstance(payload, dict):
        raise ValueError("Prayer payload must be an object")
    return True


def affirmation(self_love_joy):
    """Stub affirmation: returns the supplied statement unchanged
    (empty string for a missing statement). The production module
    composes the statement; the stub performs no composition."""
    return self_love_joy if isinstance(self_love_joy, str) else ""
