from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_CONFIG = {
    "pool_target": 20,
    "auto_register_enabled": True,
    "auto_refresh_enabled": True,
    "register_concurrency": 5,
    "scheduler_recover_on_boot": True,
    "proxy": None,
}


class StoreService:
    def __init__(
        self,
        project_root: Path,
        data_dir: Path | None = None,
        *,
        max_log_lines: int = 1000,
    ) -> None:
        self.project_root = project_root
        self.data_dir = data_dir or (project_root / "data")
        self.max_log_lines = max_log_lines
        self.accounts_dir = self.data_dir / "accounts"
        self.index_path = self.data_dir / "index" / "accounts_index.json"
        self.config_path = self.data_dir / "config" / "runtime_config.json"
        self.logs_path = self.data_dir / "logs" / "events.log"
        self._lock = threading.RLock()
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for p in [
            self.accounts_dir,
            self.index_path.parent,
            self.config_path.parent,
            self.logs_path.parent,
        ]:
            p.mkdir(parents=True, exist_ok=True)

        if not self.index_path.exists():
            self._write_json(self.index_path, {"accounts": [], "updated_at": self.utcnow()})

        if not self.config_path.exists():
            self._write_json(self.config_path, DEFAULT_RUNTIME_CONFIG)

        if not self.logs_path.exists():
            self.logs_path.write_text("", encoding="utf-8")

    @staticmethod
    def utcnow() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _normalize_runtime_config(self, cfg: Any) -> dict[str, Any]:
        merged = dict(DEFAULT_RUNTIME_CONFIG)
        if isinstance(cfg, dict):
            for key in DEFAULT_RUNTIME_CONFIG:
                if key in cfg:
                    merged[key] = cfg[key]

        merged["pool_target"] = max(1, int(merged.get("pool_target", 20)))
        merged["register_concurrency"] = max(1, int(merged.get("register_concurrency", 5)))
        merged["auto_register_enabled"] = bool(merged.get("auto_register_enabled", True))
        merged["auto_refresh_enabled"] = bool(merged.get("auto_refresh_enabled", True))
        merged["scheduler_recover_on_boot"] = bool(merged.get("scheduler_recover_on_boot", True))

        proxy_value = merged.get("proxy")
        merged["proxy"] = str(proxy_value).strip() if proxy_value else None
        return merged

    def load_runtime_config(self) -> dict[str, Any]:
        with self._lock:
            raw = self._read_json(self.config_path, dict(DEFAULT_RUNTIME_CONFIG))
            normalized = self._normalize_runtime_config(raw)
            if raw != normalized:
                self._write_json(self.config_path, normalized)
            return normalized

    def update_runtime_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cfg = self.load_runtime_config()
            for key, value in patch.items():
                if key in DEFAULT_RUNTIME_CONFIG:
                    cfg[key] = value
            cfg = self._normalize_runtime_config(cfg)
            self._write_json(self.config_path, cfg)
            return cfg

    def load_accounts_index(self) -> dict[str, Any]:
        with self._lock:
            idx = self._read_json(self.index_path, {"accounts": [], "updated_at": self.utcnow()})
            if not isinstance(idx, dict):
                idx = {"accounts": [], "updated_at": self.utcnow()}
            if not isinstance(idx.get("accounts"), list):
                idx["accounts"] = []
            idx.setdefault("updated_at", self.utcnow())
            return idx

    def save_accounts_index(self, idx: dict[str, Any]) -> None:
        with self._lock:
            idx["updated_at"] = self.utcnow()
            self._write_json(self.index_path, idx)

    def list_accounts(self, *, status: str | None = None) -> list[dict[str, Any]]:
        accounts = self.load_accounts_index().get("accounts", [])
        if status is None:
            return accounts
        return [a for a in accounts if str(a.get("status")) == status]

    def count_active_accounts(self) -> int:
        return len(self.list_accounts(status="active"))

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        for account in self.list_accounts():
            if str(account.get("account_id")) == account_id:
                return account
        return None

    def _to_project_relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.project_root.resolve()))
        except Exception:
            return str(path.resolve())

    def _resolve_path(self, token_path: str) -> Path:
        p = Path(token_path)
        if p.is_absolute():
            return p
        return self.project_root / p

    @staticmethod
    def _safe_email(email: str) -> str:
        cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in email)
        return cleaned or "unknown"

    def write_token_file(self, token_data: dict[str, Any]) -> Path:
        with self._lock:
            email = str(token_data.get("email") or "unknown")
            safe_email = self._safe_email(email)
            stamp = int(datetime.now(timezone.utc).timestamp())
            token_path = self.accounts_dir / f"token_{safe_email}_{stamp}.json"
            token_path.write_text(
                json.dumps(token_data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            return token_path

    def save_account(
        self,
        token_data: dict[str, Any],
        *,
        status: str = "active",
        token_path: Path | None = None,
        error_message: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            token_path = token_path or self.write_token_file(token_data)
            idx = self.load_accounts_index()
            accounts = idx["accounts"]

            account_id = str(token_data.get("account_id") or "").strip() or str(uuid.uuid4())
            email = str(token_data.get("email") or "").strip()

            now = self.utcnow()
            entry = None
            for item in accounts:
                if str(item.get("account_id")) == account_id or (
                    email and str(item.get("email")) == email
                ):
                    entry = item
                    break

            if entry is None:
                entry = {
                    "account_id": account_id,
                    "email": email,
                    "token_file": self._to_project_relative(token_path),
                    "status": status,
                    "last_success_at": now if status == "active" else "",
                    "last_error": error_message,
                    "last_error_at": now if error_message else "",
                    "created_at": now,
                    "updated_at": now,
                }
                accounts.append(entry)
            else:
                entry["email"] = email or entry.get("email", "")
                entry["token_file"] = self._to_project_relative(token_path)
                entry["status"] = status
                entry["updated_at"] = now
                if status == "active":
                    entry["last_success_at"] = now
                if error_message:
                    entry["last_error"] = error_message
                    entry["last_error_at"] = now

            self.save_accounts_index(idx)
            return entry

    def update_account_status(
        self,
        account_id: str,
        *,
        status: str,
        error_message: str = "",
    ) -> dict[str, Any] | None:
        with self._lock:
            idx = self.load_accounts_index()
            for entry in idx["accounts"]:
                if str(entry.get("account_id")) != account_id:
                    continue
                now = self.utcnow()
                entry["status"] = status
                entry["updated_at"] = now
                if status == "active":
                    entry["last_success_at"] = now
                if error_message:
                    entry["last_error"] = error_message
                    entry["last_error_at"] = now
                self.save_accounts_index(idx)
                return entry
        return None

    def load_account_token(self, account_id: str) -> dict[str, Any] | None:
        account = self.get_account(account_id)
        if not account:
            return None
        token_file = str(account.get("token_file") or "")
        if not token_file:
            return None
        token_path = self._resolve_path(token_file)
        if not token_path.exists():
            return None
        try:
            return json.loads(token_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_token_for_account(self, account_id: str, token_data: dict[str, Any]) -> dict[str, Any] | None:
        account = self.get_account(account_id)
        if account is None:
            return None
        token_path = self.write_token_file(token_data)
        return self.save_account(token_data, status="active", token_path=token_path)

    def _is_within_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.project_root.resolve())
            return True
        except Exception:
            return False

    def _remove_token_file_by_path(self, token_file: str) -> bool:
        if not token_file:
            return False
        token_path = self._resolve_path(token_file)
        if not token_path.exists():
            return False
        if not self._is_within_root(token_path):
            return False
        try:
            token_path.unlink()
            return True
        except Exception:
            return False

    def delete_account(self, account_id: str, *, delete_token_file: bool = True) -> dict[str, Any]:
        with self._lock:
            idx = self.load_accounts_index()
            accounts = idx["accounts"]

            target = None
            target_index = -1
            for i, entry in enumerate(accounts):
                if str(entry.get("account_id")) == account_id:
                    target = entry
                    target_index = i
                    break

            if target is None:
                return {"ok": False, "deleted": 0, "token_files_deleted": 0}

            token_removed = False
            if delete_token_file:
                token_removed = self._remove_token_file_by_path(str(target.get("token_file") or ""))

            del accounts[target_index]
            self.save_accounts_index(idx)
            return {
                "ok": True,
                "deleted": 1,
                "token_files_deleted": 1 if token_removed else 0,
            }

    def delete_accounts(
        self,
        account_ids: list[str],
        *,
        delete_token_file: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            normalized_ids: list[str] = []
            seen: set[str] = set()
            for raw in account_ids:
                account_id = str(raw).strip()
                if account_id and account_id not in seen:
                    normalized_ids.append(account_id)
                    seen.add(account_id)

            if not normalized_ids:
                return {
                    "requested": 0,
                    "deleted": 0,
                    "token_files_deleted": 0,
                    "not_found": [],
                }

            idx = self.load_accounts_index()
            accounts = idx["accounts"]

            by_id: dict[str, dict[str, Any]] = {
                str(entry.get("account_id")): entry for entry in accounts if str(entry.get("account_id"))
            }

            deleted = 0
            token_files_deleted = 0
            not_found: list[str] = []

            for account_id in normalized_ids:
                entry = by_id.get(account_id)
                if entry is None:
                    not_found.append(account_id)
                    continue
                if delete_token_file and self._remove_token_file_by_path(str(entry.get("token_file") or "")):
                    token_files_deleted += 1
                deleted += 1

            if deleted:
                keep_ids = set(normalized_ids)
                idx["accounts"] = [
                    entry for entry in accounts if str(entry.get("account_id")) not in keep_ids
                ]
                self.save_accounts_index(idx)

            return {
                "requested": len(normalized_ids),
                "deleted": deleted,
                "token_files_deleted": token_files_deleted,
                "not_found": not_found,
            }

    def delete_all_accounts(self, *, delete_token_file: bool = True) -> dict[str, Any]:
        with self._lock:
            idx = self.load_accounts_index()
            accounts = idx["accounts"]
            if not accounts:
                return {"requested": 0, "deleted": 0, "token_files_deleted": 0}

            token_files_deleted = 0
            if delete_token_file:
                for entry in accounts:
                    if self._remove_token_file_by_path(str(entry.get("token_file") or "")):
                        token_files_deleted += 1

            requested = len(accounts)
            idx["accounts"] = []
            self.save_accounts_index(idx)
            return {
                "requested": requested,
                "deleted": requested,
                "token_files_deleted": token_files_deleted,
            }

    def append_event(
        self,
        *,
        level: str,
        event: str,
        message: str,
        account_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            payload: dict[str, Any] = {
                "ts": self.utcnow(),
                "level": level,
                "event": event,
                "message": message,
            }
            if account_id:
                payload["account_id"] = account_id
            if extra:
                payload["extra"] = extra

            with self.logs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

            lines = self.logs_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > self.max_log_lines:
                trimmed = lines[-self.max_log_lines :]
                self.logs_path.write_text("\n".join(trimmed) + "\n", encoding="utf-8")

    def read_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            limit = max(1, min(1000, int(limit)))
            lines = self.logs_path.read_text(encoding="utf-8").splitlines()
            out: list[dict[str, Any]] = []
            for line in lines[-limit:]:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            return out

    def sync_existing_token_files(self) -> int:
        imported = 0
        root_candidates = list(self.project_root.glob("token_*.json"))
        account_candidates = list(self.accounts_dir.glob("token_*.json"))

        for src_path in root_candidates + account_candidates:
            try:
                target_path = self.accounts_dir / src_path.name
                if src_path.resolve() != target_path.resolve():
                    if not target_path.exists():
                        shutil.copy2(src_path, target_path)
                    src_path = target_path

                token_data = json.loads(src_path.read_text(encoding="utf-8"))
                self.save_account(token_data, status="active", token_path=src_path)
                imported += 1
            except Exception:
                continue

        return imported
