"""Internal delegation API — PUBLIC STUB.

The production internal API (server-to-server job delegation and
link/credit receivers used by a separate, non-public service) is not
part of the open-source shell: neither its endpoints nor its shared
key protocol are published. This stub keeps the composition surface
the HTTP application wires against:

- ``internal_link_key_from_env()`` returns None (no key configured),
- ``create_internal_app(...)`` returns an application with no
  routes, so the mount point exists but answers nothing.

While no key is configured the production composition also answers
404 for this surface; the open shell simply has no surface at all.
"""
import os

from starlette.applications import Starlette

LINK_KEY_ENV = "AIQS_SI_INTERNAL_LINK_KEY"


def internal_link_key_from_env(environ=None):
    """No internal link key exists in the open-source shell."""
    return None


def create_internal_app(store, link_key, service=None, *, referral_enabled=None):
    """Return an empty application: the internal delegation API is
    proprietary and is not implemented in the open-source shell."""
    return Starlette(routes=[])
