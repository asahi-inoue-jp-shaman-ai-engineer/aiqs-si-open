"""Completion notifications for standalone-app jobs (foundation
work item 2, 2026-11-23): at submission the user chooses "wait"
(stay on the waiting screen) or "notify" (leave; a completion mail
with a resume link brings them back). A wait-mode job whose result
is never viewed escalates to the same mail, so a result is never
silently dropped.

Division of labour: the Store owns the durable per-job notification
record (choice, lifecycle, resume-token hash) and every state
transition; this module owns rendering and transport. Delivery is
fail-soft by design -- an unconfigured or failing mailer leaves rows
queued and can never fail, block or roll back a job.

The recipient address comes only from the verified access token's
email claim, captured at submission and stored bound to the single
job's notification record; it is erased when the record reaches a
terminal state (sent / suppressed / expired). The service keeps no
other copy of user email addresses.

Transport is the Resend HTTP API (the same provider that already
sends the auth mail via Supabase's custom SMTP, from asi@iboga.jp).
The API key is read from the environment at composition time only
(build_notification_delivery), never logged or returned.
"""
import hashlib
import os
import secrets
import string
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

RESEND_API_KEY_ENV = "AIQS_SI_RESEND_API_KEY"
NOTIFY_FROM_ENV = "AIQS_SI_NOTIFY_FROM"
NOTIFY_BASE_URL_ENV = "AIQS_SI_NOTIFY_BASE_URL"
MAIL_TEMPLATE_DIR_ENV = "AIQS_SI_MAIL_TEMPLATE_DIR"

DEFAULT_FROM = "アイクスSI <asi@iboga.jp>"
DEFAULT_BASE_URL = "https://asi.iboga.jp"
RESEND_ENDPOINT = "https://api.resend.com/emails"

# A wait-mode job whose result is still unviewed this long after it
# finished is treated as "the user left": escalate to mail.
ESCALATION_GRACE_SECONDS = 900
# Armed/queued records die this long after the job finished; a
# completion mail days later would be noise, not a rescue.
NOTIFICATION_TTL_SECONDS = 48 * 3600
# The resume link stays redeemable for a week (the app also offers
# the same result through history, so the link is a shortcut, not
# the only path).
RESUME_TOKEN_TTL_SECONDS = 7 * 24 * 3600
RESUME_TOKEN_TTL_DAYS = RESUME_TOKEN_TTL_SECONDS // 86400
MAX_DELIVERY_ATTEMPTS = 5
CLAIM_LIMIT_PER_CYCLE = 20
# Wall-clock budget for one delivery cycle, checked between
# messages; a single send is bounded by the transport itself.
CYCLE_DEADLINE_SECONDS = 120
# A claimed row whose sender never reported back (crash between
# claim and outcome) is reclaimed after this long.
CLAIM_STALE_SECONDS = 600

OPERATION_LABELS = {"scan": "スキャン", "irradiation": "照射"}

DEFAULT_TEMPLATES = {
    "completed_subject": "【アイクスSI】{operation_label}が完了しました",
    "completed_body": (
        "アイクスSIの{operation_label}が完了しました。\n"
        "結果の確認はこちらから開けます（ログインが必要です）。\n"
        "\n"
        "{resume_url}\n"
        "\n"
        "このリンクは{expires_days}日間有効です。\n"
    ),
    "ended_subject": "【アイクスSI】{operation_label}が終了しました",
    "ended_body": (
        "アイクスSIの{operation_label}が終了しました。\n"
        "状態の確認はこちらから開けます（ログインが必要です）。\n"
        "\n"
        "{resume_url}\n"
        "\n"
        "このリンクは{expires_days}日間有効です。\n"
    ),
}

TEMPLATE_KEYS = tuple(DEFAULT_TEMPLATES)


class MailDeliveryError(Exception):
    """A single send failed. The message is a fixed transport/status
    class only -- never the recipient, subject or body."""


def plausible_email(value):
    """The one shape check shared by token-claim capture, the store
    and the API: a short string with exactly one @ and no spaces."""
    return (isinstance(value, str) and 3 <= len(value) <= 254
            and value.count("@") == 1
            and not any(char.isspace() for char in value)
            and not value.startswith("@") and not value.endswith("@"))


class ResendMailer:
    """One-shot sends over the Resend HTTP API. Transport injection
    mirrors onboarding_provider: tests pass a fake transport, and no
    code path here ever sees a real key in tests."""

    def __init__(self, api_key, sender, *, client=None, transport=None):
        if client is not None and transport is not None:
            raise ValueError("Choose one upstream transport boundary")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Resend API key required")
        if not isinstance(sender, str) or not sender.strip():
            raise ValueError("Notification sender required")
        self._api_key = api_key
        self._sender = sender
        self._client = client
        self._transport = transport

    @property
    def sender(self):
        """The configured From, exposed so the delivery can persist
        it in the immutable envelope before the first attempt."""
        return self._sender

    def send(self, *, to, subject, text, idempotency_key=None, sender=None):
        if not plausible_email(to):
            raise MailDeliveryError("invalid_recipient")
        body = {"from": sender if sender is not None else self._sender,
                "to": [to], "subject": subject, "text": text}
        headers = {"Authorization": "Bearer " + self._api_key,
                   "Content-Type": "application/json"}
        if idempotency_key is not None:
            # Stable per job across every retry, with an identical
            # payload (the resume token is minted once and reused),
            # so the provider's dedup window turns an ambiguous
            # accept-then-timeout into at-most-one delivery.
            headers["Idempotency-Key"] = idempotency_key

        def request(client):
            try:
                response = client.post(RESEND_ENDPOINT, json=body,
                                       headers=headers, timeout=10,
                                       follow_redirects=False)
            except httpx.HTTPError:
                raise MailDeliveryError("transport") from None
            if response.status_code not in (200, 201):
                raise MailDeliveryError("status_%d" % response.status_code)
            return response

        if self._client is not None:
            return request(self._client)
        with httpx.Client(trust_env=False, transport=self._transport) as client:
            return request(client)


ALLOWED_FIELDS = frozenset({"operation_label", "resume_url", "expires_days"})
MAX_TEMPLATE_CHARS = 4096


def _template_is_safe(text):
    """An override may use only the three documented placeholders,
    as bare fields — no attribute or index traversal, no format
    specs beyond plain substitution — and must stay small. Anything
    else keeps the built-in wording for that key: a wording file
    must never break or bend delivery."""
    if not isinstance(text, str) or not text.strip() \
            or len(text) > MAX_TEMPLATE_CHARS:
        return False
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError:
        return False
    for _literal, field, spec, _conversion in parts:
        if field is None:
            continue
        if field not in ALLOWED_FIELDS or spec:
            return False
    return True


def load_templates(directory=None):
    """Built-in copy, optionally overridden per key by UTF-8 files
    named <key>.txt in the operator's template directory, so the
    final wording (owned by the UI/UX room) drops in without a code
    change. An unreadable, oversized or unsafe override keeps the
    default for that key."""
    templates = dict(DEFAULT_TEMPLATES)
    if directory:
        base = Path(directory)
        for key in TEMPLATE_KEYS:
            try:
                text = (base / (key + ".txt")).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _template_is_safe(text):
                templates[key] = text
    return templates


def render_completion(templates, *, outcome, operation, resume_url):
    """(subject, text) for one completion mail. Any rendering
    failure in an override falls back to the built-in wording for
    that part instead of failing the send. The subject is a single
    line by construction: CR/LF can never reach the provider."""
    kind = "completed" if outcome == "completed" else "ended"
    values = {"operation_label": OPERATION_LABELS.get(operation, "処理"),
              "resume_url": resume_url,
              "expires_days": RESUME_TOKEN_TTL_DAYS}

    def render(part):
        try:
            return templates[kind + "_" + part].format_map(values)
        except (KeyError, IndexError, ValueError, AttributeError):
            return DEFAULT_TEMPLATES[kind + "_" + part].format_map(values)

    subject = render("subject")
    subject = subject.replace("\r", " ").replace("\n", " ").strip()
    return subject, render("body")


class NotificationDelivery:
    """One maintenance-cycle step: escalate stale wait-mode records,
    claim due mail, render, send, record the outcome. Per-message
    transport failures are recorded on the row (bounded retries with
    backoff in the Store) and never propagate."""

    def __init__(self, store, mailer, *, base_url=DEFAULT_BASE_URL,
                 templates=None, clock=time.time):
        origin = _validate_base_url(base_url)
        if origin is None:
            raise ValueError("Notification base URL must be a plain HTTPS origin")
        self.store = store
        self.mailer = mailer
        self.base_url = origin
        self.templates = dict(DEFAULT_TEMPLATES) if templates is None else templates
        self.clock = clock

    def resume_url(self, token):
        return self.base_url + "/app#resume=" + token

    def run_once(self):
        now = self.clock()
        self.store.escalate_completion_notifications(now)
        sent = 0
        claimed = self.store.claim_completion_notifications(
            now, limit=CLAIM_LIMIT_PER_CYCLE)
        deadline = now + CYCLE_DEADLINE_SECONDS
        for row in claimed:
            if self.clock() > deadline:
                # The cycle budget is spent; unstarted claims go
                # stale and are reclaimed by a later cycle. The
                # production transport bounds a single send (10 s
                # HTTP timeout), and the maintenance scheduler
                # additionally isolates this cycle behind its own
                # timeout, so a wedged sender cannot stall the loop.
                break
            try:
                delivered = self._deliver_one(row, now)
            except Exception:
                # Per-message isolation is a hard requirement: a
                # rendering defect or a mailer raising outside its
                # contract must not abort the batch or wedge later
                # cycles. The failure is recorded under a fixed
                # class only, fenced to this claim.
                try:
                    self.store.mark_notification_failed(
                        row["job"], "internal_error",
                        claim_id=row["claim_id"], now=now)
                except Exception:
                    pass
                continue
            if delivered:
                sent += 1
        return sent

    def _deliver_one(self, row, now):
        # The resume token is minted once per notification and
        # reused by every retry, so the payload is identical across
        # attempts and the provider idempotency key below dedups an
        # ambiguous accept. A row that already carries a token (a
        # retry, or a reclaimed claim) reuses it as-is.
        token = row.get("resume_token")
        if not token:
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            if not self.store.set_notification_resume_token(
                    row["job"], token, token_hash,
                    now + RESUME_TOKEN_TTL_SECONDS,
                    claim_id=row["claim_id"]):
                return False  # Fenced out: a newer claim owns it now.
        # The delivery envelope (sender, subject, body) is rendered
        # once and persisted before the first attempt; retries send
        # the stored envelope verbatim, so configuration changes
        # between attempts can never turn the stable idempotency
        # key into a payload conflict at the provider.
        if row.get("mail_subject") is not None:
            subject = row["mail_subject"]
            text = row["mail_body"]
            sender = row.get("mail_sender")
        else:
            subject, text = render_completion(
                self.templates, outcome=row["outcome"],
                operation=row["operation"],
                resume_url=self.resume_url(token))
            sender = getattr(self.mailer, "sender", None)
            if not self.store.set_notification_envelope(
                    row["job"], claim_id=row["claim_id"], subject=subject,
                    body=text, sender=sender):
                return False  # Fenced out: a newer claim owns it now.
        # Last check before transport starts: a result the owner
        # viewed while this row waited in the claimed batch is not
        # mailed, and a claim that is no longer ours sends nothing.
        if self.store.notification_delivery_gate(
                row["job"], claim_id=row["claim_id"]) != "send":
            return False
        try:
            self.mailer.send(to=row["email"], subject=subject, text=text,
                             sender=sender,
                             idempotency_key="aiqs-completion-" + row["job"])
        except MailDeliveryError as error:
            self.store.mark_notification_failed(
                row["job"], str(error), claim_id=row["claim_id"], now=now)
            return False
        return self.store.mark_notification_sent(
            row["job"], claim_id=row["claim_id"], now=now)


def _validate_base_url(value):
    """The normalized origin, or None: HTTPS only, a real host and
    port, no credentials, query, fragment or path beyond '/', and
    no whitespace or control characters. Parsing failures are a
    validation result, never an exception — a malformed operator
    value must not be able to prevent application startup."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(char.isspace() or ord(char) < 0x20
                            for char in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port  # raises on malformed port text
    except ValueError:
        return None
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        return None
    host = parsed.hostname
    if ":" in host:  # IPv6 literal: brackets are part of the origin.
        host = "[" + host + "]"
    origin = "https://" + host
    if port is not None:
        origin += ":%d" % port
    return origin


def _base_url_from_env(environ):
    value = environ.get(NOTIFY_BASE_URL_ENV)
    origin = _validate_base_url(value)
    return origin if origin is not None else DEFAULT_BASE_URL


def build_notification_delivery(store, environ=None, *, mailer=None):
    """NotificationDelivery for the composition root, or None while
    no sender is configured -- the fail-soft pre-launch state in
    which choices are still recorded and escalate, but nothing is
    promised or sent. An injected mailer (tests) bypasses the env
    key. The key value is read here and nowhere else."""
    environ = os.environ if environ is None else environ
    if mailer is None:
        key = environ.get(RESEND_API_KEY_ENV)
        if not isinstance(key, str) or not key.strip():
            return None
        sender = environ.get(NOTIFY_FROM_ENV)
        mailer = ResendMailer(
            key, sender if isinstance(sender, str) and sender.strip()
            else DEFAULT_FROM)
    templates = load_templates(environ.get(MAIL_TEMPLATE_DIR_ENV))
    return NotificationDelivery(store, mailer,
                                base_url=_base_url_from_env(environ),
                                templates=templates)
