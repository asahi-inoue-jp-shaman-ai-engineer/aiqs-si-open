"""Explicit public UI routes; API paths never fall back to the SPA."""
from pathlib import Path
from urllib.parse import urlsplit
from starlette.responses import FileResponse, Response
from starlette.routing import Route


def ui_routes(directory, issuer, *, app_enabled=False):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir() or not (root / "index.html").is_file() or (root / "index.html").is_symlink():
        raise ValueError("UI build directory must contain index.html")
    provider = urlsplit(issuer)
    if provider.scheme != "https" or not provider.netloc or provider.username:
        raise ValueError("UI requires an HTTPS issuer")
    headers = {
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; manifest-src 'self'; connect-src 'self' https://" + provider.netloc + "; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
    }

    async def entry(request):
        return FileResponse(root / "index.html", headers=headers)

    async def serve_file(relative, from_assets):
        parts = Path(relative).parts
        if not parts or any(p.startswith('.') or ':' in p or '\\' in p for p in parts):
            return Response(status_code=404, headers=headers)
        base = root / "assets" if from_assets else root
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_relative_to(base.resolve()) or not candidate.is_file():
            return Response(status_code=404, headers=headers)
        if candidate.suffix not in {'.js', '.css', '.png', '.svg', '.webp', '.woff2', '.txt',
                                    '.webmanifest', '.ico'}:
            return Response(status_code=404, headers=headers)
        media_type = None
        if candidate.suffix == '.webmanifest':
            media_type = 'application/manifest+json'
        elif candidate.suffix == '.ico':
            media_type = 'image/x-icon'
        return FileResponse(candidate, headers=headers, media_type=media_type)

    async def public_file(request):
        if "asset" in request.path_params:
            return await serve_file(request.path_params["asset"], True)
        return await serve_file("ai-agent-guide.txt", False)

    async def manifest_file(request):
        return await serve_file('manifest.webmanifest', True)

    async def favicon_file(request):
        return await serve_file('favicon.ico', True)

    routes = [Route('/', entry), Route('/oauth/consent', entry),
              Route('/account', entry), Route('/account/link', entry),
              Route('/account/purchase', entry),
              Route('/ai-agent-guide.txt', public_file),
              Route('/manifest.webmanifest', manifest_file),
              Route('/favicon.ico', favicon_file),
              Route('/assets/{asset:path}', public_file)]
    if app_enabled:
        # The standalone app page rides the same SPA entry; it is
        # only served while the AIQS_SI_APP_ENABLED rollout switch
        # is on (si-standalone-app-design-20261122 §8).
        routes.append(Route('/app', entry))
    return routes
