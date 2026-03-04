from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from services.register_service import CLIENT_ID, TOKEN_URL
from services.store_service import StoreService


class RefreshService:
    def __init__(
        self,
        store: StoreService,
        event_hook: Callable[[str, str, str, str, dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.store = store
        self._event_hook = event_hook

    def _emit(
        self,
        level: str,
        event: str,
        message: str,
        *,
        account_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._event_hook:
            self._event_hook(level, event, message, account_id, extra)

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _jwt_claims_no_verify(id_token: str) -> dict[str, Any]:
        if not id_token or id_token.count(".") < 2:
            return {}
        payload_b64 = id_token.split(".")[1]
        pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
        try:
            payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _post_form(url: str, data: dict[str, str], timeout: int = 30) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError(
                        f"refresh failed: {resp.status}: {raw.decode('utf-8', 'replace')}"
                    )
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise RuntimeError(
                f"refresh failed: {exc.code}: {raw.decode('utf-8', 'replace')}"
            ) from exc

    def refresh_with_token(self, token_data: dict[str, Any]) -> dict[str, Any]:
        refresh_token = str(token_data.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError("missing refresh_token")

        token_resp = self._post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )

        new_access = str(token_resp.get("access_token") or "").strip()
        if not new_access:
            raise RuntimeError("refresh response missing access_token")

        new_refresh = str(token_resp.get("refresh_token") or "").strip() or refresh_token
        id_token = str(token_resp.get("id_token") or token_data.get("id_token") or "").strip()
        expires_in = self._to_int(token_resp.get("expires_in"))

        claims = self._jwt_claims_no_verify(id_token)
        auth_claims = claims.get("https://api.openai.com/auth") or {}

        now = int(time.time())
        return {
            "id_token": id_token,
            "access_token": new_access,
            "refresh_token": new_refresh,
            "account_id": str(
                auth_claims.get("chatgpt_account_id")
                or token_data.get("account_id")
                or ""
            ).strip(),
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "email": str(claims.get("email") or token_data.get("email") or "").strip(),
            "type": str(token_data.get("type") or "codex"),
            "expired": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(now + max(expires_in, 0)),
            ),
        }

    def refresh_account(self, account_id: str) -> dict[str, Any] | None:
        token_data = self.store.load_account_token(account_id)
        if token_data is None:
            self._emit("error", "refresh.account.not_found", "account token not found", account_id=account_id)
            return None

        try:
            refreshed = self.refresh_with_token(token_data)
        except Exception as exc:
            self.store.update_account_status(account_id, status="invalid", error_message=str(exc))
            self._emit(
                "error",
                "refresh.account.failed",
                f"refresh failed: {exc}",
                account_id=account_id,
            )
            return None

        updated = self.store.save_token_for_account(account_id, refreshed)
        self._emit(
            "info",
            "refresh.account.success",
            "token refreshed",
            account_id=account_id,
        )
        return updated

    @staticmethod
    def _extract_status_code(response: Any) -> int | None:
        if response is None:
            return None
        if isinstance(response, tuple) and response:
            first = response[0]
            if isinstance(first, int):
                return first
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
        if isinstance(response, dict) and isinstance(response.get("status_code"), int):
            return int(response["status_code"])
        return None

    def use_token(
        self,
        account_id: str,
        request_callable: Callable[[str], Any],
        *,
        auto_refresh_enabled: bool,
    ) -> tuple[Any, bool]:
        token_data = self.store.load_account_token(account_id)
        if token_data is None:
            raise RuntimeError("account token not found")

        first_response = request_callable(str(token_data.get("access_token") or ""))
        first_code = self._extract_status_code(first_response)

        if first_code not in (401, 403):
            return first_response, False

        if not auto_refresh_enabled:
            return first_response, False

        refreshed = self.refresh_account(account_id)
        if refreshed is None:
            self.store.update_account_status(
                account_id,
                status="invalid",
                error_message="refresh failed after unauthorized response",
            )
            return first_response, False

        new_token = self.store.load_account_token(account_id)
        if new_token is None:
            return first_response, False

        second_response = request_callable(str(new_token.get("access_token") or ""))
        second_code = self._extract_status_code(second_response)
        if second_code in (401, 403):
            self.store.update_account_status(
                account_id,
                status="invalid",
                error_message="unauthorized after refresh retry",
            )
        return second_response, True
