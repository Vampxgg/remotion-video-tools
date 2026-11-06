"""Single-owner worker verification and durable health evidence."""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4


class BossWorkerHealth:
    """Health lifecycle shared by the BOSS pool; mutations hold its pool lock."""

    def _init_health(self, health_file):
        self._health_file = Path(health_file) if health_file else None
        self._health_records = {}
        self._verification_tasks = {}
        self._drain_tasks = set()
        self._unverified = set()
        if self._health_file is None:
            return
        self._unverified = {self._worker_id(w) for w in self._workers}
        try:
            saved = json.loads(self._health_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for worker in self._workers:
            worker_id = self._worker_id(worker)
            record = saved.get(worker_id, {})
            if record.get("binding") != self._health_binding(worker):
                continue
            self._health_records[worker_id] = record
            if record.get("reason_kind") in {"login_required", "account_restricted"}:
                self._login_required.add(worker_id)
                self._cooldown_reason[worker_id] = record.get("error", "")
            remaining = max(0, record.get("retry_at", 0) - time.time())
            if remaining:
                self._cooldown_until[worker_id] = time.monotonic() + remaining
            if record.get("state") in {"checking", "unknown_execution"}:
                record["state"] = "unknown"
                record["error"] = "服务重启，需重新验证"

    def _health_binding(self, worker):
        value = [self._worker_id(worker), getattr(worker, "profile_id", None),
                 getattr(worker, "_browser_host_port", None)]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def _save_health_locked(self):
        if self._health_file is None:
            return
        self._health_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._health_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._health_records, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self._health_file)

    def _record_health_locked(self, worker_id, **values):
        worker = self._find_worker(worker_id)
        record = self._health_records.setdefault(worker_id, {})
        record.update(values, binding=self._health_binding(worker))
        self._save_health_locked()

    def _quarantine_worker(self, worker_id, error):
        from services.boss_zhipin_client import _boss_access_error_kind
        kind = _boss_access_error_kind(error)
        if kind in {"login_required", "account_restricted"}:
            self._record_login_required(worker_id, reason=str(error))
            return
        with self._lock:
            self._unverified.add(worker_id)
            self._record_health_locked(worker_id, state="failed", reason_kind=kind,
                                       checked_at=time.time(), error=str(error)[:500])

    def _defer_worker_release(self, worker, operation, reason, *, release_slot=True):
        """Detach the caller, never ownership. Only real completion permits a probe."""
        key = self._worker_id(worker)
        with self._lock:
            self._recovering.add(key)
            self._unverified.add(key)
            self._record_health_locked(key, state="unknown_execution", error=reason[:500])

        async def drain():
            error = RuntimeError(reason)
            completed = False
            try:
                try:
                    await asyncio.shield(operation)
                except Exception as exc:
                    error = exc
                except asyncio.CancelledError:
                    if not operation.cancelled():
                        raise
                wait_idle = getattr(worker, "wait_for_idle", None)
                if wait_idle is not None:
                    late_errors = await wait_idle()
                    if late_errors:
                        from services.boss_zhipin_client import _boss_access_error_kind
                        error = next((e for e in late_errors if _boss_access_error_kind(e) in {
                            "login_required", "account_restricted",
                        }), late_errors[-1])
                self._quarantine_worker(key, error)
                completed = True
            finally:
                # Interrupted cleanup is fail-closed: never release an unknown owner.
                if completed:
                    with self._lock:
                        self._recovering.discard(key)
                        if release_slot:
                            self._in_flight[key] = max(0, self._in_flight.get(key, 0) - 1)

        task = asyncio.create_task(drain())
        self._drain_tasks.add(task)

        def finished(task):
            self._drain_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        task.add_done_callback(finished)

    def begin_verification(self, *, manual=False, worker_id=None):
        """Return existing operations on duplicate requests; never overlap collection."""
        chosen = [w for w in self._workers if worker_id is None or self._worker_id(w) == worker_id]
        if not chosen:
            raise ValueError("未知 worker")
        for worker in chosen:
            key = self._worker_id(worker)
            with self._lock:
                if key in self._recovering or self._in_flight.get(key, 0):
                    continue
                if self._cooldown_until.get(key, 0) > time.monotonic():
                    continue
                if key in self._login_required and not manual:
                    continue
                record = self._health_records.get(key, {})
                if not manual and record.get("state") in {"candidate", "healthy"} and key not in self._unverified:
                    continue
                self._recovering.add(key)
                self._record_health_locked(key, state="checking", operation_id=uuid4().hex,
                                           checked_at=time.time(), error=None)
            self._verification_tasks[key] = asyncio.create_task(self._verify_worker(worker))
        return self.worker_status()

    def verification_in_progress(self, worker_id=None):
        return any(not task.done() for key, task in self._verification_tasks.items()
                   if worker_id is None or key == worker_id)

    async def _probe_worker(self, worker):
        from services.boss_zhipin_client import _boss_access_error_kind
        try:
            await worker.probe_ready()
        except Exception as exc:
            if _boss_access_error_kind(exc) != "chrome_unhealthy" or self._runtime_manager is None:
                raise
            await asyncio.to_thread(
                self._runtime_manager.ensure_worker, self._runtime_config_for_worker(worker),
                proxy_id=getattr(worker, "proxy_id", None),
                chrome_proxy_server=getattr(worker, "chrome_proxy_server", None),
            )
            await worker.probe_ready()

    async def _verify_worker(self, worker):
        key = self._worker_id(worker)
        probe = None
        deferred = False
        try:
            probe = asyncio.create_task(self._probe_worker(worker))
            try:
                await asyncio.wait_for(asyncio.shield(probe), timeout=30)
            except asyncio.TimeoutError:
                with self._lock:
                    self._record_health_locked(key, state="unknown_execution",
                                               error="验证超时，等待底层执行结束")
                # The sync browser thread may still own its tab. Keep exclusivity.
                await asyncio.shield(probe)
            with self._lock:
                self._login_required.discard(key)
                self._unverified.discard(key)
                self._cooldown_until.pop(key, None)
                self._cooldown_reason.pop(key, None)
                self._record_health_locked(key, state="candidate", reason_kind=None,
                                           retry_at=0, error=None, checked_at=time.time())
        except asyncio.CancelledError:
            # Shutdown must not advertise this worker as reusable.
            with self._lock:
                self._unverified.add(key)
                self._record_health_locked(key, state="unknown_execution", error="验证执行被中断")
            if probe is not None:
                deferred = True
                self._defer_worker_release(worker, probe, "验证执行被中断", release_slot=False)
            raise
        except Exception as exc:
            from services.boss_zhipin_client import _boss_access_error_kind
            kind = _boss_access_error_kind(exc)
            if key in self._login_required and kind not in {"login_required", "account_restricted"}:
                # A failed transport/browser probe cannot erase an existing account quarantine.
                kind = self._health_records.get(key, {}).get("reason_kind") or "login_required"
            if kind in {"login_required", "account_restricted"}:
                self._record_login_required(key, reason=str(exc))
            else:
                self._record_cooldown(key, seconds=60, reason=str(exc))
            with self._lock:
                self._record_health_locked(key, state="failed", reason_kind=kind,
                                           checked_at=time.time(), error=str(exc)[:500])
        finally:
            with self._lock:
                if not deferred and (probe is None or probe.done()):
                    self._recovering.discard(key)
            self._verification_tasks.pop(key, None)
