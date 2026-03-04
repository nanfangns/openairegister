from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.refresh_service import RefreshService
from services.register_service import RegisterService
from services.scheduler_service import SchedulerService
from services.store_service import StoreService


@dataclass
class AppContainer:
    store: StoreService
    register_service: RegisterService
    refresh_service: RefreshService
    scheduler_service: SchedulerService


def build_container(project_root: Path, data_dir: Path) -> AppContainer:
    store = StoreService(project_root=project_root, data_dir=data_dir)

    def event_hook(
        level: str,
        event: str,
        message: str,
        account_id: str,
        extra: dict[str, Any] | None,
    ) -> None:
        store.append_event(
            level=level,
            event=event,
            message=message,
            account_id=account_id,
            extra=extra,
        )

    register_service = RegisterService(event_hook=event_hook)
    refresh_service = RefreshService(store=store, event_hook=event_hook)
    scheduler_service = SchedulerService(
        store=store,
        register_service=register_service,
        refresh_service=refresh_service,
    )

    imported = store.sync_existing_token_files()
    if imported:
        store.append_event(
            level="info",
            event="bootstrap.imported_tokens",
            message=f"imported {imported} token files",
        )

    cfg = store.load_runtime_config()
    if bool(cfg.get("scheduler_recover_on_boot", True)):
        scheduler_service.start()

    return AppContainer(
        store=store,
        register_service=register_service,
        refresh_service=refresh_service,
        scheduler_service=scheduler_service,
    )
