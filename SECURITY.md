# Security policy

## Reporting a vulnerability

Please report vulnerabilities through **GitHub Private
Vulnerability Reporting** for this repository (the "Report a
vulnerability" button under the Security tab). Do not open a
public issue for a vulnerability.

We aim to acknowledge a report within 7 days and to agree on a
disclosure timeline with the reporter.

## What must never be filed as a public issue

- Anything involving real credentials, tokens or keys.
- Abuse of credits, grants, referral rewards, invitation codes or
  pricing on the production service — report these privately the
  same way as a vulnerability. A public write-up of a working
  abuse path harms the service's users before it can be fixed.
- Personal data of service users.

## Known limitations

### Session identity fence: two storage-shape variants (V1 / V2)

The browser shell keeps a session identity "fence" that decides
whether a stored session may still be used after shared browser
storage has been touched. Two adversarial storage shapes are
known to slip past the fence's current clauses:

- **V1 — clear-with-retained-lineage**: after a session record
  is captured, deleting it and replacing the epoch companion
  with a `clear` record that *keeps the same session id (sid)*
  leaves the account-side fence supplying the old access token
  while the app side treats the session as gone. The real clear
  path always writes a fresh sid, so the application itself
  never produces this shape.
- **V2 — missing-revision substitution**: replacing the session
  record with one that carries a different refresh token and
  *no revision (rev) field* passes both the same-revision
  clause and the token ratchet clause, because each clause's
  precondition assumes the other field is present.

**Threat model.** Both variants require an attacker who can
already write arbitrary shapes into the *user's own browser
storage* in a specific sequence. An actor with that capability
can read the stored tokens directly, so these variants do not
grant access beyond what that actor already has. The fence is
one layer of a defence-in-depth design — server-side claim
binding, claim generation checks and lease verification remain
authoritative — not the sole barrier. We publish with these
variants documented rather than holding the release, and we
track a clause redesign (full identity comparison including
revision presence, and explicit handling of foreign `clear`
shapes) as follow-up work.

If you find additional fence variants, please report them
privately as described above.

## Scope notes

This repository is the open shell of the Aiqs SI service. The
resonance core, catalog dataset, device layer and internal APIs
are proprietary and out of scope here — findings that depend on
them cannot be reproduced from this repository; report them
privately with that note and they will be routed internally.
