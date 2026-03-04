import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from services.refresh_service import RefreshService
from services.store_service import StoreService


class RefreshServiceTests(unittest.TestCase):
    def _create_store_and_account(self) -> tuple[StoreService, str]:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        store = StoreService(project_root=root, data_dir=root / "data")
        token_data = {
            "id_token": "",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "account_id": "acc-1",
            "last_refresh": "2026-01-01T00:00:00Z",
            "email": "a@example.com",
            "type": "codex",
            "expired": "2026-01-02T00:00:00Z",
        }
        store.save_account(token_data)
        return store, "acc-1"

    def tearDown(self) -> None:
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_refresh_account_success(self) -> None:
        store, account_id = self._create_store_and_account()
        service = RefreshService(store)

        mock_resp = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "id_token": "",
        }

        with patch.object(RefreshService, "_post_form", return_value=mock_resp):
            updated = service.refresh_account(account_id)

        self.assertIsNotNone(updated)
        token = store.load_account_token(account_id)
        self.assertIsNotNone(token)
        assert token is not None
        self.assertEqual(token["access_token"], "new-access")
        self.assertEqual(token["refresh_token"], "new-refresh")

    def test_refresh_account_failure_marks_invalid(self) -> None:
        store, account_id = self._create_store_and_account()
        service = RefreshService(store)

        with patch.object(RefreshService, "_post_form", side_effect=RuntimeError("boom")):
            updated = service.refresh_account(account_id)

        self.assertIsNone(updated)
        account = store.get_account(account_id)
        self.assertIsNotNone(account)
        assert account is not None
        self.assertEqual(account["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
