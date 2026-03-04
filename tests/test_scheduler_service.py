import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.scheduler_service import SchedulerService
from services.store_service import StoreService


class DummyRegisterService:
    def __init__(self) -> None:
        self.counter = 0

    def register_account(self, proxy: str | None = None):
        self.counter += 1
        n = self.counter
        return {
            "id_token": "",
            "access_token": f"access-{n}",
            "refresh_token": f"refresh-{n}",
            "account_id": f"acc-{n}",
            "last_refresh": "2026-01-01T00:00:00Z",
            "email": f"user{n}@example.com",
            "type": "codex",
            "expired": "2026-01-02T00:00:00Z",
        }


class DummyRefreshService:
    pass


class SchedulerServiceTests(unittest.TestCase):
    def test_refill_pool_to_target(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StoreService(project_root=root, data_dir=root / "data")
            store.update_runtime_config({"pool_target": 3, "register_concurrency": 2})

            scheduler = SchedulerService(store, DummyRegisterService(), DummyRefreshService())
            result = scheduler.refill_pool(source="test")

            self.assertTrue(result["ok"])
            self.assertEqual(store.count_active_accounts(), 3)
            self.assertEqual(result["registered"], 3)


if __name__ == "__main__":
    unittest.main()
