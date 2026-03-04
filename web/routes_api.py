from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from web.container import AppContainer
from web.dependencies import get_container


router = APIRouter(prefix="/api", tags=["api"])


class ToggleJobRequest(BaseModel):
    job: str = Field(pattern="^(auto_register|auto_refresh)$")
    enabled: bool


class UpdateConfigRequest(BaseModel):
    pool_target: int | None = None
    register_concurrency: int | None = None
    auto_register_enabled: bool | None = None
    auto_refresh_enabled: bool | None = None
    scheduler_recover_on_boot: bool | None = None
    proxy: str | None = None


class DeleteAccountsRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)


def _status_counts(accounts: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"active": 0, "invalid": 0, "cooldown": 0, "unknown": 0}
    for account in accounts:
        status = str(account.get("status") or "unknown")
        if status not in counts:
            status = "unknown"
        counts[status] += 1
    return counts


def _unique_name(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name

    stem, suffix = name, ""
    if "." in name:
        idx = name.rfind(".")
        stem = name[:idx]
        suffix = name[idx:]

    seq = 2
    while True:
        candidate = f"{stem}_{seq}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        seq += 1


@router.get("/status")
def status(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    store = container.store
    scheduler = container.scheduler_service

    accounts = store.list_accounts()
    cfg = store.load_runtime_config()
    logs = store.read_events(limit=200)
    errors = [item for item in logs if str(item.get("level")) == "error"][-10:]

    return {
        "config": cfg,
        "pool": {
            "target": int(cfg.get("pool_target", 20)),
            "active": store.count_active_accounts(),
            "total": len(accounts),
            "status_counts": _status_counts(accounts),
        },
        "scheduler": scheduler.status(),
        "recent_errors": errors,
        "last_events": logs[-20:],
    }


@router.get("/accounts")
def list_accounts(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    accounts = sorted(
        container.store.list_accounts(),
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )
    return {
        "accounts": accounts,
        "count": len(accounts),
    }


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str, container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    account_id = account_id.strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")

    result = container.store.delete_account(account_id, delete_token_file=True)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="account not found")

    container.store.append_event(
        level="info",
        event="accounts.delete.one",
        message="deleted one account",
        account_id=account_id,
        extra={
            "deleted": int(result.get("deleted", 0)),
            "token_files_deleted": int(result.get("token_files_deleted", 0)),
        },
    )
    return result


@router.post("/accounts/delete-batch")
def delete_accounts_batch(
    payload: DeleteAccountsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    if not payload.account_ids:
        raise HTTPException(status_code=400, detail="account_ids is required")

    result = container.store.delete_accounts(payload.account_ids, delete_token_file=True)
    container.store.append_event(
        level="info",
        event="accounts.delete.batch",
        message="deleted accounts in batch",
        extra={
            "requested": int(result.get("requested", 0)),
            "deleted": int(result.get("deleted", 0)),
            "token_files_deleted": int(result.get("token_files_deleted", 0)),
            "not_found_count": len(result.get("not_found", [])),
        },
    )
    return {"ok": True, **result}


@router.post("/accounts/delete-all")
def delete_accounts_all(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    result = container.store.delete_all_accounts(delete_token_file=True)
    container.store.append_event(
        level="warn",
        event="accounts.delete.all",
        message="deleted all accounts",
        extra={
            "requested": int(result.get("requested", 0)),
            "deleted": int(result.get("deleted", 0)),
            "token_files_deleted": int(result.get("token_files_deleted", 0)),
        },
    )
    return {"ok": True, **result}


@router.get("/accounts/export")
def export_accounts_config(container: AppContainer = Depends(get_container)) -> Response:
    store = container.store
    accounts = sorted(
        store.list_accounts(),
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )

    archive = io.BytesIO()
    used_names: set[str] = set()
    exported = 0

    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, account in enumerate(accounts, start=1):
            account_id = str(account.get("account_id") or "")
            if not account_id:
                continue

            token_data = store.load_account_token(account_id)
            if not isinstance(token_data, dict) or not token_data:
                continue

            source_name = str(account.get("token_file") or "").strip()
            if source_name:
                file_name = Path(source_name).name
            else:
                email = str(account.get("email") or "unknown").replace("@", "_")
                file_name = f"token_{email}_{int(time.time())}_{idx}.json"

            if not file_name.endswith(".json"):
                file_name += ".json"

            file_name = _unique_name(file_name, used_names)
            token_text = json.dumps(token_data, ensure_ascii=False, separators=(",", ":"))
            zf.writestr(file_name, token_text.encode("utf-8"))
            exported += 1

    archive.seek(0)
    filename = f"oai_tokens_export_{int(time.time())}.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Exported-Count": str(exported),
    }

    return Response(content=archive.getvalue(), media_type="application/zip", headers=headers)


@router.post("/register/once")
def register_once(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    return container.scheduler_service.register_once(source="manual")


@router.post("/register/refill")
def register_refill(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    return container.scheduler_service.refill_pool(source="manual")


@router.post("/refresh/account/{account_id}")
def refresh_account(account_id: str, container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    account = container.store.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    result = container.refresh_service.refresh_account(account_id)
    if result is None:
        raise HTTPException(status_code=502, detail="refresh failed")

    return {
        "ok": True,
        "account": result,
    }


@router.post("/jobs/toggle")
def toggle_job(
    payload: ToggleJobRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.scheduler_service.toggle_job(payload.job, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/config")
def update_config(
    payload: UpdateConfigRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    patch: dict[str, Any] = {
        key: value
        for key, value in payload.model_dump().items()
        if value is not None
    }
    if not patch:
        raise HTTPException(status_code=400, detail="empty patch")

    cfg = container.store.update_runtime_config(patch)
    return {"ok": True, "config": cfg}


@router.get("/logs")
def logs(
    limit: int = Query(default=200, ge=1, le=1000),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    items = container.store.read_events(limit=limit)
    return {
        "logs": items,
        "count": len(items),
    }
