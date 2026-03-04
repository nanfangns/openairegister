from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from services.refresh_service import RefreshService
from services.register_service import RegisterService
from services.store_service import StoreService


class SchedulerService:
    def __init__(
        self,
        store: StoreService,
        register_service: RegisterService,
        refresh_service: RefreshService,
    ) -> None:
        self.store = store
        self.register_service = register_service
        self.refresh_service = refresh_service
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._register_lock = threading.Lock()

    def start(self) -> None:
        if self.scheduler.running:
            return
        self.scheduler.add_job(
            self._auto_register_tick,
            "interval",
            seconds=30,
            id="auto_register",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        self.store.append_event(
            level="info",
            event="scheduler.started",
            message="scheduler started",
        )

    def stop(self) -> None:
        if not self.scheduler.running:
            return
        self.scheduler.shutdown(wait=False)
        self.store.append_event(
            level="info",
            event="scheduler.stopped",
            message="scheduler stopped",
        )

    def _auto_register_tick(self) -> None:
        cfg = self.store.load_runtime_config()
        if not bool(cfg.get("auto_register_enabled", True)):
            return
        self.refill_pool(source="auto")

    def _register_once_with_retry(self, proxy: str | None) -> dict[str, Any] | None:
        delays = [0, 2, 4, 8]
        last_error = ""

        for attempt, delay in enumerate(delays, start=1):
            if delay > 0:
                time.sleep(delay)

            token_data = self.register_service.register_account(proxy)
            if token_data:
                account = self.store.save_account(token_data, status="active")
                self.store.append_event(
                    level="info",
                    event="register.success",
                    message="account registered",
                    account_id=str(account.get("account_id") or ""),
                    extra={"attempt": attempt, "email": account.get("email", "")},
                )
                return account

            last_error = f"register failed at attempt {attempt}"

        self.store.append_event(
            level="error",
            event="register.failed",
            message=last_error,
        )
        return None

    def register_once(self, *, source: str = "manual") -> dict[str, Any]:
        cfg = self.store.load_runtime_config()
        proxy = cfg.get("proxy")

        with self._register_lock:
            result = self._register_once_with_retry(proxy)

        return {
            "ok": result is not None,
            "source": source,
            "account": result,
        }

    def refill_pool(self, *, source: str = "manual") -> dict[str, Any]:
        if not self._register_lock.acquire(blocking=False):
            return {
                "ok": False,
                "source": source,
                "message": "register workers are already running",
            }

        try:
            cfg = self.store.load_runtime_config()
            target = max(1, int(cfg.get("pool_target", 20)))
            concurrency = max(1, int(cfg.get("register_concurrency", 5)))
            proxy = cfg.get("proxy")

            active_count = self.store.count_active_accounts()
            deficit = max(0, target - active_count)

            if deficit == 0:
                return {
                    "ok": True,
                    "source": source,
                    "target": target,
                    "active_before": active_count,
                    "registered": 0,
                    "failed": 0,
                    "active_after": active_count,
                }

            workers = min(concurrency, deficit)
            registered = 0
            failed = 0
            created_ids: list[str] = []

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(self._register_once_with_retry, proxy)
                    for _ in range(deficit)
                ]
                for future in as_completed(futures):
                    account = future.result()
                    if account is None:
                        failed += 1
                        continue
                    registered += 1
                    account_id = str(account.get("account_id") or "")
                    if account_id:
                        created_ids.append(account_id)

            active_after = self.store.count_active_accounts()
            self.store.append_event(
                level="info",
                event="register.refill.done",
                message="pool refill finished",
                extra={
                    "source": source,
                    "target": target,
                    "active_before": active_count,
                    "active_after": active_after,
                    "registered": registered,
                    "failed": failed,
                },
            )

            return {
                "ok": True,
                "source": source,
                "target": target,
                "active_before": active_count,
                "registered": registered,
                "failed": failed,
                "active_after": active_after,
                "created_account_ids": created_ids,
            }
        finally:
            self._register_lock.release()

    def toggle_job(self, job_name: str, enabled: bool) -> dict[str, Any]:
        if job_name not in {"auto_register", "auto_refresh"}:
            raise ValueError("job must be auto_register or auto_refresh")

        if enabled and not self.scheduler.running:
            self.start()

        patch = {}
        if job_name == "auto_register":
            patch["auto_register_enabled"] = bool(enabled)
        if job_name == "auto_refresh":
            patch["auto_refresh_enabled"] = bool(enabled)

        cfg = self.store.update_runtime_config(patch)

        self.store.append_event(
            level="info",
            event="jobs.toggled",
            message=f"{job_name}={'on' if enabled else 'off'}",
            extra={"config": cfg},
        )

        return {
            "ok": True,
            "job": job_name,
            "enabled": bool(enabled),
            "config": cfg,
        }

    def status(self) -> dict[str, Any]:
        auto_job = self.scheduler.get_job("auto_register") if self.scheduler.running else None
        return {
            "scheduler_running": self.scheduler.running,
            "register_busy": self._register_lock.locked(),
            "jobs": {
                "auto_register": {
                    "exists": auto_job is not None,
                    "next_run_time": auto_job.next_run_time.isoformat() if auto_job and auto_job.next_run_time else None,
                }
            },
        }
