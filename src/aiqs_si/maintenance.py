"""Supervised maintenance; diagnostics never include database/error contents."""
import asyncio
import logging
import sqlite3


class Maintenance:
    def __init__(self, store):
        self.store = store
        self.healthy = False

    async def check(self):
        healthy = True
        for name in ('expire_scans', 'expire_executions', 'retry_pending_dplanet_ops',
                     'reconcile_orphan_readings'):
            method = getattr(self.store, name, None)
            if method is None:
                continue  # Optional store capability (e.g. partial doubles).
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
