"""Pure, nonsecret deployment manifest contract; does not activate any service.

The initial hostname profile supports ASCII DNS names and canonical IP literals.
Internationalized domain names, including all xn-- A-labels, are unsupported.
"""
from dataclasses import dataclass
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit


_ERROR = 'Invalid deployment manifest'
_FIELDS = frozenset({'schema_version', 'authority_issuer', 'resource_audience',
                     'upstream_issuer', 'publishable_key'})


def _https(value, path_pattern):
    if (type(value) is not str or not value.isascii() or len(value) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or any(char in value for char in '%\\?#')):
        raise ValueError(_ERROR)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.username is not None
                or parsed.password is not None or not parsed.hostname
                or re.fullmatch(path_pattern, parsed.path) is None):
            raise ValueError(_ERROR)
        host = parsed.hostname
        if ':' in host:
            host = '[' + ipaddress.IPv6Address(host).compressed + ']'
        else:
            if len(host) > 253 or any(label.startswith('xn--') or
                                      re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None
                                      for label in host.split('.')):
                raise ValueError(_ERROR)
            if re.fullmatch(r'[0-9.]+', host):
                host = str(ipaddress.IPv4Address(host))
            elif re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]*)', host.rsplit('.', 1)[-1]):
                # Browsers treat a numeric/hex final label as an IPv4 candidate.
                # Accept only the canonical dotted-decimal form handled above.
                raise ValueError(_ERROR)
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError(_ERROR)
        origin = 'https://' + host + (f':{port}' if port is not None and port != 443 else '')
        if value != origin + parsed.path:
            raise ValueError(_ERROR)
        return origin
    except (ValueError, UnicodeError):
        raise ValueError(_ERROR) from None


@dataclass(frozen=True, slots=True)
class DeploymentConfig:
    schema_version: int
    authority_issuer: str
    resource_audience: str
    upstream_issuer: str
    publishable_key: str

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError(_ERROR)
        _https(self.authority_issuer, r'/realms/[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
        _https(self.resource_audience, r'/mcp')
        _https(self.upstream_issuer, r'/auth/v1')
        if self.authority_issuer == self.upstream_issuer:
            raise ValueError(_ERROR)
        if (type(self.publishable_key) is not str or
                re.fullmatch(r'sb_publishable_[A-Za-z0-9_-]{1,512}', self.publishable_key) is None):
            raise ValueError(_ERROR)

    @classmethod
    def from_dict(cls, manifest):
        if type(manifest) is not dict or set(manifest) != _FIELDS:
            raise ValueError(_ERROR)
        return cls(**manifest)

    @property
    def authority_origin(self):
        return _https(self.authority_issuer, r'/realms/[A-Za-z0-9][A-Za-z0-9_-]{0,127}')

    @property
    def sdk_base(self):
        return self.upstream_issuer[:-len('/auth/v1')]

    @property
    def get_user_url(self):
        return self.upstream_issuer + '/user'

    @property
    def csp_connect_origin(self):
        return self.sdk_base

    @property
    def onboarding_issuer(self):
        return self.upstream_issuer

    @property
    def broker_identity_issuer(self):
        return self.upstream_issuer

    @property
    def federation_alias(self):
        return 'si-upstream-' + hashlib.sha256(self.upstream_issuer.encode('utf-8')).hexdigest()

    @property
    def binding(self):
        """Identity namespace pin, not persisted or authorized by this parser."""
        return (self.authority_issuer, self.upstream_issuer, self.federation_alias)

    def assert_existing_binding(self, existing_binding):
        """Require an exact previously established pin; never rewrite it."""
        if type(existing_binding) is not tuple or existing_binding != self.binding:
            raise ValueError(_ERROR)
