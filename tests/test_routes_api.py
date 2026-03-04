import io
import json
import unittest
import zipfile

from fastapi import HTTPException

from web.routes_api import (
    DeleteAccountsRequest,
    UpdateConfigRequest,
    delete_account,
    delete_accounts_all,
    delete_accounts_batch,
    export_accounts_config,
    update_config,
)


class _FakeStore:
    def __init__(self) -> None:
        self.config = {
            "pool_target": 20,
            "auto_register_enabled": True,
            "auto_refresh_enabled": True,
            "register_concurrency": 5,
            "scheduler_recover_on_boot": True,
            "proxy": None,
        }
        self.accounts = [
            {
                "account_id": "acc-1",
                "email": "a@example.com",
                "token_file": "data/accounts/token_same.json",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {
                "account_id": "acc-2",
                "email": "b@example.com",
                "token_file": "backup/token_same.json",
                "updated_at": "2026-01-02T00:00:00Z",
            },
        ]
        self.tokens = {
            "acc-1": {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "email": "a@example.com",
            },
            "acc-2": {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "email": "b@example.com",
            },
        }
        self.events = []

    def list_accounts(self):
        return list(self.accounts)

    def load_account_token(self, account_id: str):
        return self.tokens.get(account_id)

    def update_runtime_config(self, patch):
        self.config.update(patch)
        return dict(self.config)

    def append_event(self, **kwargs):
        self.events.append(kwargs)

    def delete_account(self, account_id: str, *, delete_token_file: bool = True):
        if account_id == "acc-404":
            return {"ok": False, "deleted": 0, "token_files_deleted": 0}
        return {"ok": True, "deleted": 1, "token_files_deleted": 1}

    def delete_accounts(self, account_ids, *, delete_token_file: bool = True):
        return {
            "requested": len(account_ids),
            "deleted": len(account_ids),
            "token_files_deleted": len(account_ids),
            "not_found": [],
        }

    def delete_all_accounts(self, *, delete_token_file: bool = True):
        return {"requested": 2, "deleted": 2, "token_files_deleted": 2}


class _FakeContainer:
    def __init__(self) -> None:
        self.store = _FakeStore()


class RoutesApiTests(unittest.TestCase):
    def test_update_config_empty_patch_raises(self) -> None:
        container = _FakeContainer()
        with self.assertRaises(HTTPException) as ctx:
            update_config(UpdateConfigRequest(), container)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_export_accounts_config_generates_oai_style_zip(self) -> None:
        container = _FakeContainer()
        response = export_accounts_config(container)

        self.assertEqual(response.media_type, "application/zip")
        self.assertEqual(response.headers.get("X-Exported-Count"), "2")

        archive = zipfile.ZipFile(io.BytesIO(response.body), mode="r")
        names = archive.namelist()
        self.assertIn("token_same.json", names)
        self.assertIn("token_same_2.json", names)

        text_1 = archive.read("token_same.json").decode("utf-8")
        text_2 = archive.read("token_same_2.json").decode("utf-8")

        expected_1 = json.dumps(
            container.store.tokens["acc-2"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        expected_2 = json.dumps(
            container.store.tokens["acc-1"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertEqual(text_1, expected_1)
        self.assertEqual(text_2, expected_2)

    def test_delete_account_not_found_raises_404(self) -> None:
        container = _FakeContainer()
        with self.assertRaises(HTTPException) as ctx:
            delete_account("acc-404", container)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delete_accounts_batch_validates_non_empty(self) -> None:
        container = _FakeContainer()
        with self.assertRaises(HTTPException) as ctx:
            delete_accounts_batch(DeleteAccountsRequest(account_ids=[]), container)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_delete_accounts_batch_and_all_success(self) -> None:
        container = _FakeContainer()
        batch = delete_accounts_batch(DeleteAccountsRequest(account_ids=["acc-1", "acc-2"]), container)
        self.assertTrue(batch["ok"])
        self.assertEqual(batch["deleted"], 2)

        full = delete_accounts_all(container)
        self.assertTrue(full["ok"])
        self.assertEqual(full["deleted"], 2)


if __name__ == "__main__":
    unittest.main()
