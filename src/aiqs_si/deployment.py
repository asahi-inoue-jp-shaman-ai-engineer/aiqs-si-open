"""Production-environment signals for the synthetic-device interlock.

Owner decision (2026-11-23): the synthetic (simulated) device is a
development and test tool; in production it is useless and must never
be selectable or able to slip in. A screen cannot be trusted to
reveal after the fact that a worker ran synthetically, so the
interlock is fail-closed in code: entry points refuse to start a
synthetic configuration for production, and the worker refuses to
claim or execute with a synthetic device in a production
environment even if it was constructed some other way.

Three independent signals mark an environment as production; any
one tripping is enough:

1. The explicit deployment marker ``AIQS_SI_ENVIRONMENT=production``
   (``prod`` is also accepted), set in the production service
   environment on the VPS and in the dedicated-PC worker wrapper.
2. The production service host itself: a worker pointed at, or an
   intake server issuing for, ``asi.iboga.jp`` is talking to
   production by definition, even if the explicit marker was lost
   in a configuration rollback. Entry points that know their
   production status from the issuer (http_server) pass it into
   Service explicitly.
3. The store's own durable designation: the production database
   carries a ``deployment_designation`` row marking itself
   production (see designate_production), so even a separate
   process that inherited neither the marker nor a URL — the stdio
   MCP entry, a local worker on the database file — is refused
   synthetic operation against it.

With no signal present the environment is not production and
the synthetic device stays available for development and tests,
exactly as before. The marker belongs in the service/wrapper
process environment only; setting it in an interactive shell would
(only) make that shell's test runs refuse synthetic devices.
"""
import os
import sqlite3
from urllib.parse import urlsplit

ENVIRONMENT_VARIABLE = "AIQS_SI_ENVIRONMENT"
PRODUCTION_ENVIRONMENT_VALUES = frozenset({"production", "prod"})
PRODUCTION_HOSTS = frozenset({"asi.iboga.jp"})

FORBIDDEN_MESSAGE = ("Synthetic device is forbidden in production: simulated "
                     "execution must never claim or run production jobs")


class SyntheticForbiddenError(RuntimeError):
    """A synthetic device was about to touch a production environment."""


class DesignationUnavailable(RuntimeError):
    """The store's production designation could not be read.

    Distinct from an established absence: callers must fail closed
    (refuse the synthetic operation) rather than treat an unreadable
    designation as "not production", and must not cache the outcome.
    """


def _canonical_host(url):
    """Canonical hostname for comparison, or None when unparseable.

    Equivalent spellings of the same host must not slip past the
    production check: the comparison lowercases, maps the Unicode
    full-stop separators IDNA allows (U+3002, U+FF0E, U+FF61) to
    ".", strips trailing root dots, and IDNA-encodes.
    """
    if not isinstance(url, str):
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if host is None:
        return None
    host = host.strip().lower()
    for separator in ("。", "．", "｡"):
        host = host.replace(separator, ".")
    host = host.strip(".")
    if not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    return host


def production_environment(environ=None, *, url=None):
    """True when the explicit marker or the target URL marks production."""
    env = os.environ if environ is None else environ
    value = env.get(ENVIRONMENT_VARIABLE)
    if isinstance(value, str) and value.strip().lower() in PRODUCTION_ENVIRONMENT_VALUES:
        return True
    if url:
        return _canonical_host(url) in PRODUCTION_HOSTS
    return False


def refuse_synthetic_in_production(environ=None, *, url=None):
    """Raise SyntheticForbiddenError when synthetic would touch production."""
    if production_environment(environ, url=url):
        raise SyntheticForbiddenError(FORBIDDEN_MESSAGE)


def store_designated_production(store):
    """True when the store's database durably designates itself production.

    The third, process-independent signal: a row in the database
    itself (see designate_production). A separate process that never
    inherited the deployment marker — the stdio MCP entry or a local
    worker pointed at the production database file — is still
    refused synthetic operation against that store.

    Returns False only for an *established* absence (no database
    file, no designation table, or no production row). A read that
    itself fails — locked, corrupt or otherwise unreadable database
    — raises DesignationUnavailable so callers fail closed instead
    of mistaking an unreadable designation for "not production".
    """
    path = getattr(store, "path", None)
    if not isinstance(path, str) or not path or not os.path.exists(path):
        return False
    try:
        db = sqlite3.connect(path, timeout=10)
        try:
            table = db.execute("SELECT name FROM sqlite_master WHERE "
                               "type = 'table' AND name = 'deployment_designation'"
                               ).fetchone()
            if table is None:
                return False
            row = db.execute("SELECT value FROM deployment_designation "
                             "WHERE key = 'deployment'").fetchone()
        finally:
            db.close()
    except sqlite3.Error as error:
        raise DesignationUnavailable(
            "Production designation could not be read") from error
    return row is not None and row[0] == "production"


def designate_production(db_path):
    """Durably mark a database as the production store (operator step).

    Part of the production release procedure; synthetic-capable
    constructors and workers refuse to operate against a designated
    store regardless of process environment.
    """
    db = sqlite3.connect(str(db_path), timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS deployment_designation "
                   "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT OR REPLACE INTO deployment_designation "
                   "(key, value) VALUES ('deployment', 'production')")
        db.commit()
    finally:
        db.close()
