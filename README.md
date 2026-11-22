# Aiqs SI — open shell

Aiqs SI is a service that lets AI agents request scans and
irradiation sessions against a dedicated resonance device, exposed
through an MCP server with OAuth-protected HTTP transport. This
repository contains the **open shell** of that service: the MCP
server, the HTTP composition, the account/auth web UI, and the
agent guide — everything an agent developer needs to understand,
test and improve the client-facing surface.

## What is NOT in this repository

The resonance core is proprietary and is distributed only to
production hosts, in destination-specific packages:

- the resonance matching / QRS engine,
- the morphic-field catalog dataset and its snapshots,
- the dedicated-device (diode) layer and the worker composition
  that drives real hardware,
- the internal server-to-server delegation API and the linking
  client for the companion service,
- production operating parameters (prices, grant amounts, quotas).

Where the shell composes against those parts, this repository
carries **clearly-marked deterministic stubs** instead:

- `src/aiqs_si_core_intake/` — stub prayer handling and
  observer-coordinate binding,
- `src/aiqs_si_device/` — a synthetic device stub whose scan
  results are marked `score_kind="stub"` and never mirror the real
  ranking,
- `src/aiqs_si/dplanet.py` — linking-client interface stub
  (linking is unconfigured in the open shell),
- `src/aiqs_si/internal_api.py` — empty internal-API mount.

Stub outputs are synthetic. They exist so the shell imports, runs
and is testable end to end; they must never be presented as real
scan inputs or results.

## Operating parameters are configuration, not code

Prices, grant amounts, scan limits and execution profiles are
loaded from JSON environment policies (`AIQS_SI_PRICING_POLICY_JSON`,
`AIQS_SI_GRANT_POLICY_JSON`, `AIQS_SI_LIMIT_POLICY_JSON`,
`AIQS_SI_CORE_EXECUTION_PROFILES_JSON`). The built-in defaults in
`src/aiqs_si/policies.py` are **synthetic example values**; the
production values are operator configuration and are not published.

## Layout

- `src/aiqs_si/` — the shell package (MCP server, service intake,
  store/ledger, serializers, worker API and worker framework,
  onboarding, billing integration points).
- `auth-ui/` — the browser auth/account UI and the agent guide
  (`auth-ui/public/ai-agent-guide.txt`).
- The MCP connection guide for agents is the agent guide in
  `auth-ui/public/ai-agent-guide.txt`.
- `tests/` — the subset of the test suite that runs against the
  open shell (stub core, synthetic parameters).

## Quick start

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e .
PYTHONPATH=src:tests python -m unittest discover -s tests -p "test_*.py"
cd auth-ui && npm ci && npm test
```

## Security

See [SECURITY.md](SECURITY.md). Please report vulnerabilities
through GitHub's private vulnerability reporting for this
repository — not as public issues. Reports about credit, grant or
referral abuse must also stay out of public issues.

## License and trademarks

Licensed under the Apache License, Version 2.0 — see
[LICENSE](LICENSE) and [NOTICE](NOTICE). The names "Aiqs SI" /
"アイクスSI" and the product logo are trademarks of the operator
and are **not** licensed under the Apache License. The resonance
core described above is proprietary and is not licensed under the
Apache License; it is simply not part of this repository.
