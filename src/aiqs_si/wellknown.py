"""OAuth discovery compatibility routes.

The MCP SDK serves the RFC 9728 protected-resource metadata only at the
path-suffixed URL (/.well-known/oauth-protected-resource/mcp). Some clients
also request the bare path, and RFC 8414 clients build the authorization
server metadata URL by path insertion
(/.well-known/oauth-authorization-server/realms/si) instead of the OIDC
suffix style the authorization server itself serves. Both documents are
served here with values identical to the canonical ones: the PRM document
is built from the same verifier configuration the SDK document uses, and
the AS document is the issuer's own discovery document, proxied verbatim
and briefly cached.
"""
import time
from urllib.parse import urlsplit
import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route


def protected_resource_document(verifier):
    # Field-for-field the document the SDK serves at the suffixed path
    # (measured live 2026-11-21), so both paths return the same JSON.
    return {
        "resource": verifier.audience,
        "authorization_servers": [verifier.issuer],
        "scopes_supported": list(verifier.authorization_scopes),
        "bearer_methods_supported": ["header"],
    }


class AuthorizationServerMetadata:
    """Verbatim proxy of the issuer's OIDC discovery document."""

    def __init__(self, issuer, *, transport=None, clock=time.monotonic,
                 ttl=300.0, timeout=5.0):
        self.issuer = issuer
        self.discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        self.transport = transport
        self.clock = clock
        self.ttl = ttl
        self.timeout = timeout
        self.document = None
        self.expires = 0.0

    async def get(self):
        if self.document is not None and self.clock() < self.expires:
            return self.document
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False,
                                     trust_env=False, transport=self.transport) as client:
            response = await client.get(self.discovery_url)
            response.raise_for_status()
            document = response.json()
        if not isinstance(document, dict) or document.get("issuer") != self.issuer:
            raise ValueError("Invalid discovery document")
        self.document, self.expires = document, self.clock() + self.ttl
        return document


def wellknown_routes(verifier, *, transport=None, clock=time.monotonic):
    metadata = AuthorizationServerMetadata(verifier.issuer, transport=transport, clock=clock)
    issuer_path = urlsplit(verifier.issuer).path.rstrip("/")
    authorization_server_path = "/.well-known/oauth-authorization-server" + issuer_path

    async def protected_resource(request):
        return JSONResponse(protected_resource_document(verifier))

    async def authorization_server(request):
        try:
            document = await metadata.get()
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"error": "authorization_server_metadata_unavailable"},
                                status_code=502)
        return JSONResponse(document)

    return [Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
            Route(authorization_server_path, authorization_server, methods=["GET"])]
