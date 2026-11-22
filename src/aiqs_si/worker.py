"""Dedicated execution boundary; queue access is independent of MCP intake."""
from aiqs_si_core_intake.prayer import validate_prayer
from .pricing import CreditPrice, irradiation_quote
import hashlib
import json
import threading


class Worker:
    def __init__(self, queue, device, *, outbox=None, execution_log=None):
        self.store, self.device = queue, device
        self.outbox = outbox
        self.execution_log = execution_log

    def observe(self, method, *args, **kwargs):
        if self.execution_log is not None:
            try:
                getattr(self.execution_log, method)(*args, **kwargs)
            except Exception:
                pass  # Observability must not alter device cleanup or delivery.

    def finish(self, job, state, result, *, scan_receipt=False):
        if self.outbox is not None:
            if scan_receipt:
                digest = hashlib.sha256(json.dumps(result, sort_keys=True, allow_nan=False).encode()).hexdigest()
                self.outbox.put(job, 'scan_receipt', {'digest': digest})
            else:
                self.outbox.put(job, state, result)
        self.store.finish(job, state, result)
        if self.outbox is not None:
            self.outbox.acknowledge(job)
        fields = {'state': state}
        if isinstance(result, dict):
            if isinstance(result.get('reason'), str):
                fields['reason'] = result['reason']
            for key in ('completed_count', 'requested_count', 'simulated_count'):
                if key in result:
                    fields[key] = result[key]
        if self.execution_log is not None:
            fields['elapsed_sec'] = self.execution_log.elapsed_since_claim()
        self.observe('emit', 'job_finished', **fields)
        self.observe('emit', 'delivery_acknowledged', state=state)

    def run_one(self):
        if self.outbox is not None:
            pending = self.outbox.pending()
            if pending:
                for job, state, result in pending:
                    if state == 'scan_receipt':
                        self.store.reconcile_scan_receipt(job, result['digest'])
                    else:
                        self.store.finish(job, state, result)
                    self.outbox.acknowledge(job)
                return True
        job = self.store.claim_next()
        if job is None:
            self.observe('idle')
            return False
        self.observe('claim', job, self.device.mode)
        # Independent liveness covers long scans as well as per-shot execution.
        self.store.heartbeat(job['id'])
        stopped = threading.Event()
        def keep_alive():
            while not stopped.wait(30):
                try:
                    self.store.heartbeat(job['id'])
                except Exception:
                    # Loss of contact is classified by the server; never retry a job.
                    continue
        thread = threading.Thread(target=keep_alive, daemon=True)
        thread.start()
        try:
            return self._run_claimed(job)
        finally:
            stopped.set()
            thread.join(timeout=20)

    def _run_claimed(self, job):
        try:
            validate_prayer(job["payload"])
            if job["payload"].get("operation") == "irradiation":
                saved_quote = job["payload"].get("quote")
                if not isinstance(saved_quote, dict):
                    raise ValueError("Shot reservation does not match execution")
                # Validate against the quote accepted at submission, never a
                # fresh quote: a later rate revision (including the end of a
                # campaign) must not reprice or reject an accepted job.
                # Quotes record the rate in tenths; quotes accepted
                # before that carry a whole-credit rate, scaled exactly.
                tenths = saved_quote.get("credits_per_shot_tenths")
                if type(tenths) is not int:
                    legacy_rate = saved_quote.get("credits_per_shot")
                    tenths = legacy_rate * 10 if type(legacy_rate) is int else None
                accepted = CreditPrice(
                    version=saved_quote.get("price_version"),
                    yen_per_credit=saved_quote.get("yen_per_credit"),
                    credits_per_shot_tenths=tenths,
                    campaign=saved_quote.get("campaign"))
                quote = irradiation_quote(job["payload"].get("irradiation_count"), accepted)
                if job.get("cost") != quote["credits"] or any(
                        saved_quote.get(key) != quote[key] for key in ("count", "credits", "strength")):
                    raise ValueError("Shot reservation does not match execution")
        except ValueError:
            self.finish(job["id"], "failed", {"reason": "required_prayer_inputs_missing"})
            return True
        if job["payload"].get("mode") != self.device.mode:
            self.finish(job["id"], "failed", {"reason": "device_mode_mismatch"})
            return True
        if self.device.mode == "hardware" and job["payload"].get("execution_settings") != self.device.execution_settings.as_dict():
            self.finish(job["id"], "failed", {"reason": "execution_settings_mismatch"})
            return True
        context = None
        if job["payload"].get("target_ids"):
            try:
                context = self.store.get_context(job["id"])
                if not isinstance(context, dict) or not context.get("bundle_hmac"):
                    raise ValueError("Invalid target context")
            except (KeyError, ValueError):
                self.finish(job["id"], "failed", {"reason": "target_context_unavailable"})
                return True
        if job["payload"].get("operation") == "scan":
            try:
                catalog_digest = job["payload"]["catalog_digest"]
                catalog = self.store.get_catalog(catalog_digest)
            except (KeyError, ValueError):
                # No device call occurred; release the reservation, don't substitute data.
                self.finish(job["id"], "failed", {"reason": "catalog_unavailable_or_invalid"})
                return True
        try:
            operation = job["payload"]["operation"]
            if operation == "scan":
                result = {"mode": self.device.mode, "physical_output_verified": False,
                          "intention_usage": "recorded_only",
                          "catalog_digest": catalog_digest,
                          "scan": self.device.scan(catalog, observer_coordinate=context) if context else self.device.scan(catalog)}
                self.observe('scan', result['scan'])
            elif operation == "irradiation":
                options = {}
                if getattr(self.device, "supports_execution_control", False):
                    def control(completed=None):
                        if completed is not None:
                            event = 'shot_completed' if self.device.mode == 'hardware' else 'shot_simulated'
                            self.observe('emit', event, completed_count=completed)
                        return self.store.execution_control(job["id"], completed)
                    options["control"] = control
                result = self.device.irradiate(job["payload"]["entries"], count=job["payload"]["irradiation_count"], **options)
            else:
                raise ValueError("Unknown operation")
        except Exception:
            self.finish(job["id"], "unknown", {"reason": "execution_outcome_unknown"})
            return True
        # Persistence failure propagates: running jobs cannot be automatically retried.
        state = "completed"
        if operation == "irradiation" and result.get("completed_count", job["payload"]["irradiation_count"]) < job["payload"]["irradiation_count"]:
            state = "cancelled"
        if operation == "irradiation" and job['payload'].get('input_mode') == 'request-images-v1':
            # Keep terminal outbox receipts free of prayer content and item lists.
            result = {key: result[key] for key in ('mode', 'completed_count', 'requested_count',
                'simulated_count', 'stop_command_acknowledged', 'physical_output_verified',
                'executed_physically', 'reason') if key in result}
        self.finish(job["id"], state, result, scan_receipt=(operation == 'scan' and job['payload'].get('input_mode') == 'request-images-v1'))
        return True
