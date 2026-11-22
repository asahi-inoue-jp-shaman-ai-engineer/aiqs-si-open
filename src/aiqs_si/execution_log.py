"""Local execution receipts and live display, from real worker events only.

Every record is emitted at the moment the worker observes the event it
describes: a claim taken from the server, a scan result returned by the
device, a shot the device confirmed, a delivery acknowledged. Nothing is
interpolated from clocks or invented to fill a quiet screen; when the
worker is idle or unreachable the display says so, with the age of the
newest information. Synthetic-mode events are labeled SIMULATED and are
never hardware evidence. Physical output and settlement stay marked
unverified here: settlement is confirmed on the server, not on this host.

The log is private to the worker host. Owner display fields come from the
server's claim response (a profile name plus the first eight characters
of the account id) and are shown only on this local display; they are
never written to GitHub or any shared channel.
"""
import argparse
import json
from pathlib import Path
import threading
import time
import uuid
import warnings

OBSERVATION_KEYS = ("device_open_returned", "driver_bytes_received",
                    "read_calls", "empty_reads", "read_errors",
                    "noise_bits_consumed", "quality_bytes_received",
                    "quality_read_calls", "command_on_bytes_returned")


class ExecutionLog:
    def __init__(self, path, *, mode=None):
        self.path = Path(path)
        self.run = uuid.uuid4().hex
        self.failed = False
        self.context = {}
        self.base_mode = mode
        self.idle_written = False
        self._claim_clock = None

    def idle(self):
        if not self.idle_written:
            self.context = {}
            self.emit('idle_no_job')
            self.idle_written = True

    def claim(self, job, mode):
        self.idle_written = False
        self._claim_clock = time.monotonic()
        payload = job.get('payload', {})
        def supplied(name):
            value = job.get(name)
            return value if isinstance(value, str) else None
        self.context = {
            'job_id': job.get('id'), 'mode': mode,
            'operation': payload.get('operation'),
            'owner_display_name': supplied('owner_display_name'),
            'owner_short_id': supplied('owner_short_id'),
            'requested_count': payload.get('irradiation_count'),
            'catalog_digest': payload.get('catalog_digest'),
            'cost_credits': job.get('cost'),
        }
        self.emit('claimed')

    def emit(self, event, **fields):
        # Callers supply only explicitly selected fields, never payloads/errors.
        try:
            record = dict(self.context, run=self.run, timestamp_epoch=time.time(),
                          event=event, physical_output_verified=False,
                          settlement='server_pending_unverified', **fields)
            if not record.get('mode'):
                record['mode'] = self.base_mode
            line = json.dumps(record, ensure_ascii=True, allow_nan=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except Exception:
            if not self.failed:
                warnings.warn('Local execution log unavailable; receipts may be missing.', RuntimeWarning)
            self.failed = True

    def elapsed_since_claim(self):
        if self._claim_clock is None:
            return None
        return round(time.monotonic() - self._claim_clock, 3)

    def scan(self, result):
        rows = [{key: row[key] for key in ('text', 'mf_name', 'qrs', 'hits') if key in row}
                for row in result.get('final_entries', [])]
        observation = result.get('device_observation')
        if isinstance(observation, dict):
            observation = {key: observation[key] for key in OBSERVATION_KEYS
                           if key in observation}
        else:
            observation = None
        self.emit('scan_result', ranking=rows, final_count=len(rows),
                  cycles=result.get('cycles'), total_mfs=result.get('total_mfs'),
                  device_observation=observation,
                  elapsed_sec=self.elapsed_since_claim())

    def shot_detail(self, shot):
        fields = {key: shot[key] for key in
                  ('shot_no', 'duration_sec', 'total_bytes', 'read_count',
                   'ones_ratio', 'quality_ok') if key in shot}
        self.emit('shot_detail', **fields)


def _safe(value):
    if value is None:
        return '未提供'
    return ''.join(c if c.isprintable() else repr(c)[1:-1] for c in str(value))


def display(record):
    def safe(value):
        if value is None:
            return '未提供'
        return ''.join(c if c.isprintable() else repr(c)[1:-1] for c in str(value))
    stamp = record.get('timestamp_epoch')
    when = time.strftime('%H:%M:%S', time.localtime(stamp)) if isinstance(stamp, (int, float)) else safe(stamp)
    mode_label = 'SIMULATED' if record.get('mode') == 'synthetic' else safe(record.get('mode'))
    lines = [f"[{when}] {mode_label} / {safe(record.get('event'))}",
             f"Account: {safe(record.get('owner_display_name'))} [{safe(record.get('owner_short_id'))}]",
             f"Job: {safe(record.get('job_id'))} | {safe(record.get('operation'))}"]
    settings = record.get('settings')
    if isinstance(settings, dict) and settings:
        parts = ' | '.join(
            f"{key} {safe(settings.get(key))}" for key in
            ('cycles', 'top_mfs', 'top_entries', 'qrs_threshold',
             'duration_sec', 'count') if key in settings)
        if parts:
            lines.append(f"Settings: {parts}")
    if record.get('elapsed_sec') is not None:
        lines.append(f"Elapsed: {safe(record.get('elapsed_sec'))}s")
    observation = record.get('device_observation')
    if isinstance(observation, dict):
        values = {key: safe(observation.get(key)) for key in OBSERVATION_KEYS}
        lines.append(
            "Device: bytes {driver_bytes_received} | reads {read_calls} "
            "(empty {empty_reads}, errors {read_errors}) | bits {noise_bits_consumed} "
            "| quality bytes {quality_bytes_received}".format(**values))
    if 'shot_no' in record:
        lines.append(
            f"Shot {safe(record.get('shot_no'))}/{safe(record.get('requested_count'))}: "
            f"{safe(record.get('duration_sec'))}s | bytes {safe(record.get('total_bytes'))} "
            f"| reads {safe(record.get('read_count'))} | ones {safe(record.get('ones_ratio'))} "
            f"| quality {'OK' if record.get('quality_ok') else 'NG'}")
    if 'completed_count' in record:
        label = 'Hardware read + stop receipts' if record.get('mode') == 'hardware' else 'SIMULATED'
        lines.append(f"{label}: {safe(record['completed_count'])}/{safe(record.get('requested_count'))}")
    if record.get('cost_credits') is not None:
        lines.append(f"Cost (accepted): {safe(record.get('cost_credits'))} credits")
    for rank, row in enumerate(record.get('ranking', []), 1):
        lines.append(f"{rank}. {safe(row.get('text'))} | MF {safe(row.get('mf_name'))} | QRS {safe(row.get('qrs'))} | hits {safe(row.get('hits'))}")
    lines.append('サーバ確定待ち（未確認）')
    return '\n'.join(lines)


def proof_block(record, *, diode_serial_hash=None):
    """Assemble one job's record with the same items as the server's
    execution_proof — the execution record handed to the user — filled
    from this host's own receipts. Items only the server can supply
    (request digest, completion receipt, settlement ledger, proof
    digest) are shown as not supplied / server pending, never invented.
    The diode serial hash is the worker's own device identity: the same
    sha256(serial) prefix the worker reports to the server at claim
    time, recovered from the log file name's identity segment.
    """
    def item(value):
        return 'not supplied' if value is None else _safe(value)
    stamp = record.get('timestamp_epoch')
    finished = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp))
                  if isinstance(stamp, (int, float)) else None)
    lines = ['--- execution proof (local record; same items as the user record) ---',
             f"job_id: {item(record.get('job_id'))}",
             f"operation: {item(record.get('operation'))}",
             f"state: {item(record.get('state'))}",
             f"cost_credits: {item(record.get('cost_credits'))}",
             f"request_digest: {item(record.get('request_digest') or record.get('digest'))}",
             'execution:',
             f"  mode: {item(record.get('mode'))}",
             f"  requested_count: {item(record.get('requested_count'))}",
             f"  completed_count: {item(record.get('completed_count'))}",
             f"  stop_requested: {item(record.get('stop_requested'))}",
             f"  stop_command_acknowledged: {item(record.get('stop_command_acknowledged'))}",
             '  worker: local worker (this host)',
             f"  finished_at: {item(finished)}",
             'device:',
             f"  diode_serial_hash: {item(diode_serial_hash)}",
             '  physical_output_verified: false',
             'completion_receipt: server pending (unverified)',
             'settlement: server pending (unverified)',
             'proof_digest: issued by the server (not supplied locally)',
             '---------------------------------------------------------------------']
    return '\n'.join(lines)


def diode_hash_from_path(path):
    """The log file name's first segment is the worker device identity:
    sha256(diode serial) truncated to 24 hex chars, the same value the
    server stores as diode_serial_hash for the job's execution proof."""
    stem = Path(path).name.split('.')[0]
    if len(stem) == 24 and all(c in '0123456789abcdef' for c in stem):
        return stem
    return None


def heartbeat_status(path, *, now=None):
    """(mode, state, stale, age) from the heartbeat file, or None."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        stamp = float(data['timestamp_epoch'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = max(0.0, (now or time.time()) - stamp)
    return (data.get('mode'), data.get('state'), age > 30, age)


def follow_heartbeat(path, *, once=False, out=print, poll=1.0):
    """Heartbeat viewer: prints only when the worker state changes.

    A steady heartbeat stays silent so real events are never buried; the
    transitions into and out of staleness (no update for over 30 seconds)
    are state changes and are printed once each.
    """
    last = object()
    while True:
        status = heartbeat_status(path)
        key = 'unavailable' if status is None else status[:3]
        if key != last:
            when = time.strftime('%H:%M:%S')
            if status is None:
                out(f"[{when}] HEARTBEAT: unavailable - execution state unknown", flush=True)
            else:
                mode, state, stale, age = status
                note = f' (STALE: no update for {age:.0f}s)' if stale else ''
                out(f"[{when}] HEARTBEAT: {mode} / {state}{note}", flush=True)
            last = key
        if once:
            return
        time.sleep(poll)


def follow(path, *, once=False, out=print):
    """Read-only receipt stream. No active-device claim is inferred from history."""
    out('LOCAL HISTORY ONLY | physical output: unverified | settlement: server pending (unverified)')
    out('Missing owner fields: not supplied. Synthetic events are not hardware evidence.')
    diode_hash = diode_hash_from_path(path)
    offset = 0
    while True:
        try:
            with Path(path).open('r', encoding='utf-8') as stream:
                if Path(path).stat().st_size < offset:
                    offset = 0
                stream.seek(offset)
                while True:
                    start = stream.tell()
                    line = stream.readline()
                    if not line.endswith('\n'):
                        offset = start
                        break
                    offset = stream.tell()
                    try:
                        record = json.loads(line)
                    except ValueError:
                        out('Unreadable receipt; no progress inferred.')
                        continue
                    out(display(record), flush=True)
                    if record.get('event') == 'job_finished':
                        out(proof_block(record, diode_serial_hash=diode_hash),
                            flush=True)
        except OSError:
            out('Log unavailable; execution state unknown.', flush=True)
        if once:
            return
        time.sleep(1)


def start_console_display(path):
    """Mandatory live display of the work log on the worker console.

    Exactly two log kinds exist: this work log (claims, device work,
    deliveries) and the heartbeat, which lives in its own file and is
    shown by its own viewer (--heartbeat-view). Nothing else is emitted,
    so the console is never buried under heartbeat lines.
    """
    thread = threading.Thread(target=follow, args=(path,),
                              daemon=True, name='execution-log-display')
    thread.start()
    return thread


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?', help='Execution log JSONL to follow')
    parser.add_argument('--heartbeat-view', metavar='PATH',
                        help='Watch a heartbeat file instead (state changes only)')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.heartbeat_view:
        follow_heartbeat(args.heartbeat_view, once=args.once)
        return
    if not args.path:
        parser.error('a log path is required (or use --heartbeat-view)')
    follow(args.path, once=args.once)


if __name__ == '__main__':
    main()
