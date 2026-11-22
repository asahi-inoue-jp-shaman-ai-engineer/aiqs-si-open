"""Explicit onboarding startup validation; never creates a database or namespace pin."""
import base64
import json
import re
import sqlite3
from pathlib import Path
from contextlib import closing

from .deployment_config import DeploymentConfig


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate configuration field')
        result[key] = value
    return result


def load_onboarding(args, environ):
    if not args.onboarding_manifest and not args.onboarding_binding:
        return None
    try:
        if (not args.onboarding_manifest or not args.onboarding_binding or not args.worker_audience
                or not args.allowed_worker or args.identity_issuer is not None):
            raise ValueError()
        manifest = Path(args.onboarding_manifest).resolve(strict=True)
        pin_file = Path(args.onboarding_binding).resolve(strict=True)
        if manifest == pin_file:
            raise ValueError()
        config = DeploymentConfig.from_dict(json.loads(manifest.read_text(encoding='utf-8'), object_pairs_hook=_object))
        pin = json.loads(pin_file.read_text(encoding='utf-8'), object_pairs_hook=_object)
        if type(pin) is not dict or set(pin) != {'authority_issuer', 'upstream_issuer', 'federation_alias'}:
            raise ValueError()
        binding = (pin['authority_issuer'], pin['upstream_issuer'], pin['federation_alias'])
        config.assert_existing_binding(binding)
        if (args.issuer != config.authority_issuer or args.audience != config.resource_audience
                or args.broker_identity_issuer != config.upstream_issuer):
            raise ValueError()
        secret = environ.get('AIQS_SI_ONBOARDING_HOOK_SECRET', '')
        value = secret.removeprefix('v1,')
        if not value.startswith('whsec_') or not base64.b64decode(value[6:], validate=True):
            raise ValueError()
        database = Path(args.database).resolve(strict=True)
        if not database.is_file():
            raise ValueError()
        # Read-only preflight prevents Store's constructor from creating a new
        # database or initializing an unrelated SQLite file on an invalid start.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT manifest_digest FROM identity_migrations WHERE issuer=?',
                             (config.upstream_issuer,)).fetchone()
        if row is None or not isinstance(row[0], str) or re.fullmatch('[0-9a-f]{64}', row[0]) is None:
            raise ValueError()
        return config, binding, secret
    except Exception:
        raise ValueError('Invalid onboarding startup configuration') from None
