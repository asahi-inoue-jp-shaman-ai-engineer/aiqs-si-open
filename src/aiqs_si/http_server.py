"""Loopback HTTP entry point for the OAuth-protected development MCP."""
from aiqs_si.auth import create_mcp_http_app
import argparse
import json
import os
from pathlib import Path
from .auth import JWTVerifier, create_http_server
from .store import Store
from .service import Service
from .catalog import load_snapshot
from .execution_settings import ExecutionSettings
from .application import create_application
from .worker_api import WORKER_SCOPE
from .jwks import JWKSVerifier
from .dplanet import DPlanetClient, routing_enabled_from_env
from .internal_api import internal_link_key_from_env
from .account_api import payment_links_from_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--audience", required=True)
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--broker-identity-issuer", help="Fixed upstream namespace for verified broker identity assertions")
    identity.add_argument("--identity-issuer", help="Trusted upstream identity namespace; SI issuer must preserve verified upstream UUID subjects")
    keys = parser.add_mutually_exclusive_group(required=True)
    keys.add_argument("--public-key", help="Trusted issuer's RSA public PEM; never a private key")
    keys.add_argument("--jwks-url", help="Operator-configured issuer public signing-key endpoint")
    parser.add_argument("--signing-algorithm", choices=["RS256", "ES256"], default="RS256")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ui-directory", help="Verified public UI build; requires combined application")
    parser.add_argument("--hardware-settings", help="Explicit operator-selected execution settings JSON")
    parser.add_argument("--catalog")
    parser.add_argument("--catalog-sha256")
    parser.add_argument("--worker-audience")
    parser.add_argument("--allowed-worker", action="append")
    parser.add_argument('--stripe-test-billing', action='store_true', help='Explicit Stripe TEST Checkout/webhook candidate; never loads live keys')
    parser.add_argument('--billing-return-origin', help='Fixed HTTPS origin for Checkout return')
    parser.add_argument('--onboarding-manifest', help='Explicit deployment manifest for opt-in onboarding')
    parser.add_argument('--onboarding-binding', help='Independent existing identity namespace binding JSON')
    args = parser.parse_args()
    if bool(args.worker_audience) != bool(args.allowed_worker):
        parser.error("Worker audience and allowed-worker must be configured together")
    if args.ui_directory and not args.worker_audience:
        parser.error("UI directory requires combined application")
    if args.hardware_settings and not args.worker_audience:
        parser.error("Hardware intake requires a dedicated-worker API configuration")
    if args.stripe_test_billing and (not args.worker_audience or not args.billing_return_origin):
        parser.error('Test billing requires combined worker API and a fixed return origin')
    if args.billing_return_origin and not args.stripe_test_billing:
        parser.error('Return origin requires explicit test billing')
    from .onboarding_startup import load_onboarding
    try:
        onboarding = load_onboarding(args, os.environ)
    except ValueError:
        parser.error('Invalid onboarding startup configuration')
    key = Path(args.public_key).read_text(encoding="utf-8") if args.public_key else None
    if key and "PRIVATE KEY" in key:
        parser.error("Use a public key only")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    verifier_type = JWKSVerifier if args.jwks_url else JWTVerifier
    key_options = {"jwks_url": args.jwks_url} if args.jwks_url else {"public_key": key}
    key_options["signing_algorithm"] = args.signing_algorithm
    verifier = verifier_type(**key_options, issuer=args.issuer, audience=args.audience,
                             identity_issuer=args.identity_issuer, broker_identity_issuer=args.broker_identity_issuer)
    dplanet_client = DPlanetClient.from_env()
    try:
        payment_links = payment_links_from_env()
    except ValueError:
        parser.error('Payment Link configuration is invalid')
    store = Store(args.database, dplanet_client=dplanet_client,
                  dplanet_routing_enabled=routing_enabled_from_env())
    billing = None
    if args.stripe_test_billing:
        from .stripe_billing import StripeBilling
        try:
            billing = StripeBilling(store, secret_key=os.environ.get('AIQS_SI_STRIPE_TEST_SECRET_KEY', ''),
                webhook_secret=os.environ.get('AIQS_SI_STRIPE_TEST_WEBHOOK_SECRET', ''),
                return_origin=args.billing_return_origin)
        except ValueError:
            parser.error('Stripe TEST billing configuration is missing or invalid')
    service = None
    if args.hardware_settings:
        if not args.catalog or not args.catalog_sha256:
            parser.error("Hardware intake requires a pinned catalog snapshot")
        settings = ExecutionSettings(**json.loads(Path(args.hardware_settings).read_text(encoding="utf-8")))
        catalog, metadata = load_snapshot(args.catalog, args.catalog_sha256)
        service = Service(store, catalog, execution_settings=settings,
                          profile_salt=os.environ.get("AIQS_SI_PROFILE_SALT"))
    elif args.catalog or args.catalog_sha256:
        parser.error("Catalog options require explicit hardware settings")
    if args.worker_audience:
        import uvicorn
        worker_verifier = verifier_type(**key_options, issuer=args.issuer,
                                      audience=args.worker_audience, required_scope=WORKER_SCOPE)
        onboarding_options = (dict(onboarding_config=onboarding[0], onboarding_binding=onboarding[1],
                                  onboarding_hook_secret=onboarding[2]) if onboarding is not None else {})
        try:
            app = create_application(store, verifier, worker_verifier,
                                     allowed_workers=args.allowed_worker, service=service, billing=billing,
                                     ui_directory=args.ui_directory,
                                     internal_link_key=internal_link_key_from_env(),
                                     dplanet_client=dplanet_client, payment_links=payment_links,
                                     **onboarding_options)
        except Exception:
            if onboarding is not None:
                parser.error('Onboarding application could not be initialized')
            raise
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
        return
    server = create_http_server(store, verifier, service=service)
    import uvicorn
    from .request_limits import RequestBodyLimit
    app = create_mcp_http_app(server)
    app.add_middleware(RequestBodyLimit)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
