"""OAuth resource-server boundary. No authorization server or token issuance here."""
from urllib.parse import urlsplit
from uuid import UUID
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from .mcp_server import create_account_server
from mcp.server.transport_security import TransportSecuritySettings

SCOPE = "aiqs:use"


class UserAccessToken(AccessToken):
    issuer: str
    subject: str
    identity_issuer: str
    upstream_subject: str | None = None
    display_name: str | None = None


class JWTVerifier:
    def __init__(self, *, public_key, issuer, audience, required_scope=SCOPE, signing_algorithm="RS256", supabase_clients=None, identity_issuer=None, broker_identity_issuer=None):
        if signing_algorithm not in ("RS256", "ES256"):
            raise ValueError("Unsupported signing algorithm")
        self.signing_algorithm = signing_algorithm
        if identity_issuer is not None and broker_identity_issuer is not None:
            raise ValueError("Identity modes are mutually exclusive")
        canonical_issuer = broker_identity_issuer or identity_issuer
        if broker_identity_issuer is not None and (required_scope != SCOPE or supabase_clients is not None or broker_identity_issuer == issuer):
            raise ValueError("Broker identity requires a separate SI user authority")
        self.broker_identity_issuer = broker_identity_issuer
        for url in (issuer, audience, *([canonical_issuer] if canonical_issuer is not None else [])):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
                raise ValueError("Issuer and audience must be absolute HTTPS URLs")
        self.key, self.issuer, self.audience = public_key, issuer, audience
        if identity_issuer is not None and (required_scope != SCOPE or supabase_clients is not None or identity_issuer == issuer):
            raise ValueError("Canonical identity mapping requires a separate SI user authority")
        self.identity_issuer = canonical_issuer or issuer
        self.mapped_identity = identity_issuer is not None
        self.required_scope = required_scope
        self.supabase_clients = None
        self.token_type = "at+jwt"
        self.authorization_scopes = [required_scope]
        if supabase_clients is not None:
            if signing_algorithm != "ES256" or required_scope != SCOPE or not supabase_clients:
                raise ValueError("Supabase user profile requires ES256, aiqs:use and explicit clients")
            clients = frozenset(supabase_clients)
            for client in clients:
                if not isinstance(client, str) or str(UUID(client)) != client:
                    raise ValueError("Supabase client IDs must be canonical UUIDs")
            if audience in clients or audience == "authenticated":
                raise ValueError("Use a dedicated SI resource audience")
            self.supabase_clients = clients
            self.token_type = "jwt"
            # Provider's authorization endpoint accepts OIDC scopes, not aiqs:use.
            # Signed aiqs:use is STILL mandatory below; openid never grants SI access.
            self.authorization_scopes = ["openid"]

    async def verify_token(self, token):
        try:
            header = jwt.get_unverified_header(token)
            token_type = header.get("typ")
            if not isinstance(token_type, str) or token_type.lower() != self.token_type:
                return None  # Provider profile is operator-selected, never token-selected.
            claims = jwt.decode(token, self.key, algorithms=[self.signing_algorithm], issuer=self.issuer,
                                audience=self.audience,
                                options={"require": ["exp", "iat", "iss", "aud", "sub", "client_id"], "strict_aud": True})
            if not all(isinstance(claims[k], str) and claims[k] for k in ("sub", "client_id")):
                return None
            # The trusted SI issuer must preserve the verified upstream UUID.
            # Never select the identity namespace from an AI-controlled claim.
            if self.mapped_identity and str(UUID(claims["sub"])) != claims["sub"]:
                return None
            upstream_subject = None
            if self.broker_identity_issuer is not None:
                upstream_subject = claims.get("si_upstream_sub")
                if not isinstance(upstream_subject, str) or str(UUID(upstream_subject)) != upstream_subject:
                    return None
                if str(UUID(claims["sub"])) != claims["sub"]:
                    return None
            if type(claims["exp"]) is not int or type(claims["iat"]) is not int:
                return None
            scope = claims.get("scope", "")
            if not isinstance(scope, str) or self.required_scope not in scope.split():
                return None
            if self.supabase_clients is not None:
                if (claims["client_id"] not in self.supabase_clients or
                    claims["aud"] != self.audience or claims.get("role") != "authenticated" or
                    claims.get("is_anonymous") is not False or "openid" not in scope.split()):
                    return None
                for field in ("sub", "session_id"):
                    value = claims.get(field)
                    if not isinstance(value, str) or str(UUID(value)) != value:
                        return None
            name = claims.get("name")
            return UserAccessToken(token=token, client_id=claims["client_id"], scopes=scope.split(),
                                   expires_at=claims["exp"], resource=self.audience,
                                   issuer=self.issuer, subject=claims["sub"], identity_issuer=self.identity_issuer,
                                   upstream_subject=upstream_subject,
                                   display_name=name if isinstance(name, str) and name else None)
        except (jwt.PyJWTError, ValueError, TypeError):
            return None


def resolve_account(store, token):
    """Resolve only already-verified tokens; broker mode never provisions accounts."""
    upstream_subject = getattr(token, "upstream_subject", None)
    if upstream_subject is not None:
        owner = store.bind_verified_identity(token.issuer, token.subject,
                                             token.identity_issuer, upstream_subject)
    else:
        owner = store.register_identity(token.identity_issuer, token.subject)
    # Opportunistic display-name capture for the worker host's local log:
    # only when the verified token itself carries a name claim.
    display_name = getattr(token, "display_name", None)
    if isinstance(display_name, str) and display_name:
        try:
            store.set_account_display(owner, display_name)
        except (ValueError, KeyError):
            pass
    return owner


def create_http_server(store, verifier, *, service=None, embedded_test_worker=None, billing=None):
    def current_owner():
        token = get_access_token()
        if not isinstance(token, UserAccessToken):
            raise PermissionError("Authenticated access token required")
        return resolve_account(store, token)

    server = create_account_server(store, current_owner, service=service,
                                 billing=billing,
                                 embedded_test_worker=(service is None or service.mode == "synthetic") if embedded_test_worker is None else embedded_test_worker,
                                 token_verifier=verifier,
                                 auth=AuthSettings(issuer_url=verifier.issuer,
                                                   resource_server_url=verifier.audience,
                                                   required_scopes=verifier.authorization_scopes,
                                                   validate_token_resource=True))
    return server


def create_mcp_http_app(server):
    resource = urlsplit(str(server.settings.auth.resource_server_url))
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", resource.netloc],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         f"{resource.scheme}://{resource.netloc}"])
    # Five existing 5 MiB images expand under base64; retain that input contract.
    return server.streamable_http_app(stateless_http=True, json_response=True,
        max_request_body_size=40 * 1024 * 1024, transport_security=security)
