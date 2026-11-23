"""Machine-checkable intake capability gate (Astra final round #4).

The intake package exists in two forms: the real core-intake wheel
(real prayer validation and profile binding) and the deliberately
weakened stubs published with the OSS shell. Both expose the same
function names, so call sites cannot tell them apart by import
alone. The real package therefore declares INTAKE_KIND /
INTAKE_CAPABILITIES in aiqs_si_core_intake/__init__.py; the stubs
declare INTAKE_KIND = "stub" (and any build that omits the marker
is treated as a stub — fail-closed).

Hardware mode requires the real intake: Service refuses to
construct a hardware service on top of a stub, and the Worker
refuses to run with a hardware device on top of a stub. Synthetic
(demo) mode remains available with either, and the active kind is
exposed in capability responses so the mode is never misreported.
"""

_REQUIRED = frozenset({"prayer_validation", "profile_binding"})

# The observer-coordinate context contract, held on THIS side of the
# boundary: the worker validates a job's target context against
# these identifiers, and the identifiers must be checkable even when
# the installed intake is a stub that does not define them (the
# published stub's profile_binding has no such constant, and the
# worker module must still import and run its synthetic path there).
# The real intake package defines the same values; a test pins the
# two sides together so they cannot drift apart.
OBSERVER_COORDINATE_MODEL_ID = "profile_bound_diode_mapper_v1"
OBSERVER_COORDINATE_VERSION = "observer_coordinate_v1"


def intake_kind():
    """'real' when the installed intake package declares the full
    real capability set; 'stub' otherwise (including no marker)."""
    try:
        from aiqs_si_core_intake import INTAKE_CAPABILITIES, INTAKE_KIND
    except Exception:
        return "stub"
    if INTAKE_KIND != "real":
        return "stub"
    try:
        if not _REQUIRED <= set(INTAKE_CAPABILITIES):
            return "stub"
    except TypeError:
        return "stub"
    return "real"


def intake_is_real():
    return intake_kind() == "real"
