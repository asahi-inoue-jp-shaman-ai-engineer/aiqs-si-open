"""Supervised maintenance; diagnostics never include database/error contents."""
import asyncio
import concurrent.futures
import logging
import sqlite3


class Maintenance:
    def __init__(self, store):
        self.store = store
        self.healthy = False
        # The notification cycle is isolated from the shared
        # executor (Astra round-4 F6): it runs on a dedicated
        # single thread, single-flight — while a cycle is still
        # running (possibly wedged in a blocking send), later
        # checks skip it instead of accumulating workers, and its
        # timeout is logged without failing health (delivery is
        # fail-soft; claims are fenced, so the retained cycle is
        # safe). Unrelated maintenance never waits on it.
        self.notification_timeout_seconds = 180
        self._notification_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="si-notify")
        self._notification_future = None

    async def check(self):
        healthy = True
        for name in ('expire_scans', 'expire_executions', 'retry_pending_dplanet_ops',
                     'run_deferred_link_transfers', 'reconcile_orphan_readings',
                     'run_notification_cycle'):
            method = getattr(self.store, name, None)
            if method is None:
                continue  # Optional store capability (e.g. partial doubles).
            if name == 'run_notification_cycle':
                # Single-flight on the dedicated executor: the
                # concurrent future is retained across checks, so a
                # cycle that outlives its timeout is awaited — never
                # duplicated — by later checks.
                if self._notification_future is None \
                        or self._notification_future.done():
                    self._notification_future = \
                        self._notification_executor.submit(method)
                try:
                    await asyncio.wait_for(
                        asyncio.wrap_future(self._notification_future),
                        timeout=self.notification_timeout_seconds)
                except asyncio.TimeoutError:
                    logging.getLogger(__name__).error(
                        'SI maintenance timed out: %s', name)
                except Exception:
                    healthy = False
                    logging.getLogger(__name__).error(
                        'SI maintenance failed: %s', name)
                continue
            for attempt in range(3):
                try:
                    await asyncio.to_thread(method)
                    break
                except sqlite3.OperationalError:
                    if attempt < 2:
                        await asyncio.sleep(0.1 * (attempt + 1))
                        continue
                except Exception:
                    pass
                healthy = False
                logging.getLogger(__name__).error('SI maintenance failed: %s', name)
                break
        self.healthy = healthy

    async def run(self):
        while True:
            await asyncio.sleep(60)
            await self.check()
