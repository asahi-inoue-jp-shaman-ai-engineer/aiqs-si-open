"""D-Planet account-linking client — PUBLIC STUB.

The production client talks to a server-to-server internal API of a
separate, non-public service (D-Planet) and is not part of the
open-source shell: neither its endpoints nor its wire format are
published. This stub carries only the interface the shell composes
against. ``DPlanetClient.from_env()`` always returns None here, so
every linking feature reports itself unconfigured — the safe state —
and credit settlement stays on the local SI ledger.

Error contract (shared with the production client): failures are
raised as DPlanetError carrying an HTTP status (or None when the
peer is unreachable) and a human-readable message.
"""
import os

BASE_URL_ENV = "AIQS_SI_DPLANET_BASE_URL"
LINK_KEY_ENV = "AIQS_SI_DPLANET_LINK_KEY"
ROUTING_ENABLED_ENV = "AIQS_SI_DPLANET_ROUTING_ENABLED"

_NOT_AVAILABLE = "D-Planet linking is not available in the open-source shell"


def routing_enabled_from_env(environ=None):
    """Consumption routing switch; default OFF.

    While OFF, linked accounts keep settling against the SI balance.
    The switch is never inferred from the mere presence of a link.
    """
    environ = os.environ if environ is None else environ
    value = environ.get(ROUTING_ENABLED_ENV, "")
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


class DPlanetError(Exception):
    """The linking peer answered with an error, or could not be reached."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status  # HTTP status, or None when unreachable
        self.message = message


class DPlanetClient:
    """Interface stub. Instances are never created in the open shell
    (from_env returns None); every operation raises DPlanetError."""

    def __init__(self, base_url, link_key, *, transport=None, timeout=10.0):
        raise DPlanetError(None, _NOT_AVAILABLE)

    @classmethod
    def from_env(cls, environ=None):
        """The production client is proprietary; the open shell has
        no linking peer configured, so this always returns None."""
        return None

    def _unavailable(self):
        raise DPlanetError(None, _NOT_AVAILABLE)

    def verify_link_code(self, code, si_owner):
        self._unavailable()

    def finalize_link(self, reservation_id, si_owner):
        self._unavailable()

    def unlink(self, *, si_owner, link_id=None, player_id=None):
        self._unavailable()

    def balance(self, player_id):
        self._unavailable()

    def reserve(self, player_id, job_id, amount):
        self._unavailable()

    def settle(self, player_id, job_id):
        self._unavailable()

    def release(self, player_id, job_id, amount):
        self._unavailable()

    def transfer_in(self, player_id, link_id, amount, *, phase="transfer"):
        self._unavailable()

    def grant(self, player_id, op_key, amount, ledger_reason):
        self._unavailable()

    def resolve_referral_code(self, code):
        self._unavailable()

    def referral_redemption_status(self, player_id):
        self._unavailable()
