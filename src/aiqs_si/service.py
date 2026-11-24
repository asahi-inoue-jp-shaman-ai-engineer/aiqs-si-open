"""Application intake: identity and prices come from server, never tool inputs."""
import base64
import hashlib
import hmac
from aiqs_si_core_intake.profile_binding import build_observer_coordinate
from .profile_image import thumbnail_bytes
from aiqs_si_core_intake.prayer import affirmation, validate_prayer
from .deployment import (FORBIDDEN_MESSAGE, DesignationUnavailable,
                         SyntheticForbiddenError, production_environment,
                         store_designated_production)
from .intake_capability import intake_is_real, intake_kind
from .pricing import irradiation_quote


def _thumbnail(source, reasons, label):
    if not source:
        reasons.append(label + "_missing")
        return None
    return thumbnail_bytes(base64.b64decode(source, validate=True))


class Service:
    """Free scans; irradiation charges the shared session total for
    the active per-shot rate (the standard rate from the pricing
    policy) — see
    pricing.session_charge_credits."""

    def __init__(self, store, catalog, *, execution_settings=None, profile_salt=None,
                 production=False):
        # Durable production signals: the caller's explicit flag
        # (http_server derives it from the production issuer) and the
        # store's own designation. The designation read is only
        # load-bearing where a synthetic outcome is possible: with
        # the flag already set it is skipped entirely, and for a
        # hardware service an unreadable designation is deferred
        # (None) to _is_production instead of failing construction —
        # but a synthetic-capable construction (no execution
        # settings) fails closed on an unreadable designation, since
        # an unreadable store must never be mistaken for a
        # non-production one. Negative results are not trusted for
        # the process lifetime: _is_production re-reads the
        # designation until it turns positive.
        if production:
            self._designated_production = True
        else:
            try:
                self._designated_production = store_designated_production(store)
            except DesignationUnavailable as error:
                if execution_settings is None:
                    raise SyntheticForbiddenError(FORBIDDEN_MESSAGE) from error
                self._designated_production = None
        if execution_settings is None and (self._designated_production
                                          or production_environment()):
            # Without execution settings this service stamps every job
            # "synthetic". That fallback is a development convenience
            # only; in a production environment it must never be
            # constructed (fail-closed, owner decision 2026-11-23).
            raise SyntheticForbiddenError(FORBIDDEN_MESSAGE)
        self.store = store
        self.execution_settings = execution_settings
        # Hardware mode is a claim about the whole chain, intake
        # included (Astra final round #4): with the stub intake
        # package installed, prayer validation and profile binding
        # do not really happen, so a hardware service must refuse
        # to start. Synthetic (demo) mode stays available and is
        # reported as synthetic, with the intake kind exposed.
        self.intake_kind = intake_kind()
        if execution_settings is not None and not intake_is_real():
            raise ValueError(
                "Hardware mode requires the real intake package "
                "(stub intake cannot validate prayers or bind profiles)")
        self.mode = "hardware" if execution_settings is not None else "synthetic"
        self.catalog_digest = store.save_catalog(catalog)
        if profile_salt is not None and (not isinstance(profile_salt, str) or not profile_salt.strip()):
            raise ValueError("Profile salt must be nonempty")
        self.profile_salt = profile_salt

    def _is_production(self):
        if self._designated_production or production_environment():
            return True
        try:
            self._designated_production = store_designated_production(self.store)
        except DesignationUnavailable as error:
            raise SyntheticForbiddenError(FORBIDDEN_MESSAGE) from error
        return bool(self._designated_production)

    def submit_image_scan(self, owner, request_key, intention, images, *, self_love_joy,
                          delegation=None):
        """Accept images for this request without creating a reusable target profile.

        Preserve the profile binding algorithm, supplying image-derived identities
        and empty optional demographic fields. Raw images are not stored or returned.
        """
        statement = affirmation(self_love_joy)
        if not isinstance(intention, str) or not 1 <= len(intention.strip()) <= 4000:
            raise ValueError("intention must contain 1 to 4000 characters")
        if self.profile_salt is None:
            raise ValueError("Image binding is not configured")
        if not isinstance(images, list) or not 1 <= len(images) <= 5:
            raise ValueError("Provide one to five images")
        profiles = []
        for photo in images:
            if not isinstance(photo, str) or not photo or len(photo) > 7 * 1024 * 1024:
                raise ValueError("Invalid image input size")
            raw = base64.b64decode(photo, validate=True)
            if _thumbnail(photo, [], "image") is None:
                raise ValueError("Every image must be valid")
            identity = hmac.new(self.profile_salt.encode(), b"si-request-image-v1\0" + raw,
                                hashlib.sha256).hexdigest()
            profiles.append({"id": identity, "photo_url": photo})
        profiles.sort(key=lambda row: row["id"])
        targets = [row["id"] for row in profiles]
        if len(set(targets)) != len(targets):
            raise ValueError("Duplicate images are not accepted")
        previous = self.store.get_by_request(owner, request_key)
        if previous is not None:
            payload = previous["payload"]
            if payload.get("content_removed"):
                raise ValueError("Scan content expired; use a new request key for a free scan")
            validate_prayer(payload)
            if (payload.get("operation") != "scan" or payload.get("input_mode") != "request-images-v1"
                    or payload.get("intention") != intention or payload.get("target_ids") != targets
                    or payload.get("self_love_joy") != self_love_joy):
                raise ValueError("request_key already used for different input")
            return previous
        context = build_observer_coordinate(profiles, salt=self.profile_salt, image_thumb_bytes=_thumbnail)
        return self.store.submit(owner, request_key,
                                 {"operation": "scan", "intention": intention,
                                  "catalog_digest": self.catalog_digest, "target_ids": targets,
                                  "input_mode": "request-images-v1",
                                  "self_love_joy": self_love_joy, "self_love_affirmation": statement,
                                  "prayer_contract": "image-intention-joy-v1",
                                  "mode": self.mode, "policy": "free-scan-v1",
                                  "execution_settings": self.execution_settings.as_dict() if self.execution_settings else None},
                                 cost=0, private_context=context, delegation=delegation)

    def register_target(self, owner, request_key, *, name, birthday="", gender="", photo_base64="", photo2_base64=""):
        if self.profile_salt is None:
            raise ValueError("Target registration is not configured")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Target name required, maximum 200 characters")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Target name must be valid text") from None
        if not all(isinstance(value, str) and len(value) <= 32 for value in (birthday, gender)):
            raise ValueError("Invalid target fields")
        for photo in (photo_base64, photo2_base64):
            if not isinstance(photo, str) or len(photo) > 7 * 1024 * 1024:
                raise ValueError("Image input too large")
            if photo:
                _thumbnail(photo, [], "image")
        profile = dict(name=name, birthday=birthday, gender=gender,
                       photo_url=photo_base64, photo_url2=photo2_base64)
        return {"target_id": self.store.register_target(owner, request_key, profile)}

    def submit_scan(self, owner, request_key, intention, target_ids=None, *, self_love_joy=None,
                    delegation=None, app_intent_id=None):
        statement = affirmation(self_love_joy)
        if not isinstance(intention, str) or not 1 <= len(intention.strip()) <= 4000:
            raise ValueError("intention must contain 1 to 4000 characters")
        targets = [] if target_ids is None else target_ids
        if not isinstance(targets, list) or not 1 <= len(targets) <= 5 or not all(isinstance(t, str) and t for t in targets) or len(set(targets)) != len(targets):
            raise ValueError("Select one to five registered targets with images")
        targets = sorted(targets)
        previous = self.store.get_by_request(owner, request_key)
        if previous is not None:
            payload = previous["payload"]
            validate_prayer(payload)
            if payload.get("operation") != "scan" or payload.get("intention") != intention or payload.get("target_ids", []) != targets or payload.get("self_love_joy") != self_love_joy:
                raise ValueError("request_key already used for different input")
            return previous
        context = None
        if targets:
            if self.profile_salt is None:
                raise ValueError("Profile binding is not configured")
            profiles = [self.store.get_target(owner, target) for target in targets]
            for profile in profiles:
                photo = profile.get("photo_url") or profile.get("photo_url2")
                if not photo or _thumbnail(photo, [], "image") is None:
                    raise ValueError("Every prayer target must have a valid image")
            context = build_observer_coordinate(profiles, salt=self.profile_salt, image_thumb_bytes=_thumbnail)
        payload = {"operation": "scan", "intention": intention,
                   "catalog_digest": self.catalog_digest,
                   "target_ids": targets,
                   "self_love_joy": self_love_joy, "self_love_affirmation": statement,
                   "prayer_contract": "image-intention-joy-v1",
                   "mode": self.mode, "policy": "free-scan-v1",
                   "execution_settings": self.execution_settings.as_dict() if self.execution_settings else None}
        if app_intent_id is not None:
            # App submission provenance (foundation item 2, Astra
            # round-4 F11): the intent id is bound into the job at
            # creation, so crash recovery can prove this job belongs
            # to that intent — an unrelated job that merely shares
            # the request key never carries it.
            payload["app_intent"] = app_intent_id
        return self.store.submit(owner, request_key, payload, cost=0,
                                 private_context=context, delegation=delegation,
                                 require_intent_id=app_intent_id)

    def submit_irradiation(self, owner, request_key, scan_id, *, count, delegation=None,
                           app_intent_id=None, confirmation_id=None):
        self.store.expire_scans()
        quote = irradiation_quote(count)
        previous = self.store.get_by_request(owner, request_key)
        if previous is not None:
            payload = previous["payload"]
            if (payload.get("operation") != "irradiation" or payload.get("scan_id") != scan_id
                    or payload.get("irradiation_count") != count
                    or (app_intent_id is not None
                        and payload.get("app_intent") != app_intent_id)):
                raise ValueError("request_key already used for different input")
            if not payload.get("content_removed"):
                validate_prayer(payload)
            return previous  # Preserve the quote accepted before any price revision.
        scan = self.store.get(owner, scan_id)
        if scan["payload"].get("content_removed"):
            raise ValueError("Scan content expired; scan again for free")
        if scan["state"] != "completed" or scan["payload"]["operation"] != "scan":
            raise ValueError("A completed scan is required")
        # Short-circuit order matters: a hardware-stamped scan never
        # needs the production decision (or its designation read);
        # only a non-hardware stamp consults _is_production, whose
        # unreadable-designation failure then fails the submission
        # closed.
        if scan["payload"].get("mode") != "hardware" and self._is_production():
            # The irradiation inherits the scan's mode stamp. A
            # synthetic-stamped scan (legacy data, a restored backup)
            # must not seed new jobs in production; require a fresh
            # scan instead of letting the stamp slip through.
            raise ValueError("Synthetic scan cannot be irradiated in production; scan again")
        validate_prayer(scan["payload"])
        # One canonical selection for publication, confirmation
        # and execution (batch 2e, R5): the irradiation input is
        # the catalog-validated, deduplicated, publication-capped
        # set the owner saw and approved — never the worker's
        # raw stored list, which may carry entries publication
        # dropped (excess, duplicates, unresolvable ids).
        from .serializers import canonical_scan_entries
        entries = canonical_scan_entries(
            self.store, scan["payload"],
            scan["result"]["scan"]["final_entries"])
        if not entries:
            raise ValueError("Scan has no selected entries")
        payload = {"operation": "irradiation", "scan_id": scan_id,
                   "irradiation_count": count, "quote": quote,
                   "input_mode": scan["payload"].get("input_mode"),
                   "entries": entries, "mode": scan["payload"]["mode"],
                   "execution_settings": scan["payload"].get("execution_settings"),
                   "target_ids": scan["payload"].get("target_ids", []),
                   **{key: scan["payload"][key] for key in
                      ("intention", "self_love_joy", "self_love_affirmation", "prayer_contract")},
                   "policy": "shot-credit-v1"}
        if app_intent_id is not None:
            # Same provenance stamp as scans (round-6 F14): the
            # intent that authorized this creation is bound into
            # the job, so reconciliation can prove the binding
            # instead of trusting a request-key claim marker.
            payload["app_intent"] = app_intent_id
        return self.store.submit(owner, request_key, payload,
                                 cost=quote["credits"],
                                 private_context=self.store.get_context(scan_id) if scan["payload"].get("target_ids") else None,
                                 delegation=delegation,
                                 require_intent_id=app_intent_id,
                                 require_confirmation_id=confirmation_id)
