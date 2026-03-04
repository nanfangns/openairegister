import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.store_service import StoreService


class StoreServiceTests(unittest.TestCase):
    def test_save_and_read_account(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StoreService(project_root=root, data_dir=root / "data")

            token_data = {
                "id_token": "id",
                "access_token": "access",
                "refresh_token": "refresh",
                "account_id": "acc-1",
                "last_refresh": "2026-01-01T00:00:00Z",
                "email": "a@example.com",
                "type": "codex",
                "expired": "2026-01-02T00:00:00Z",
            }

            entry = store.save_account(token_data)
            self.assertEqual(entry["account_id"], "acc-1")

            accounts = store.list_accounts(status="active")
            self.assertEqual(len(accounts), 1)

            loaded_token = store.load_account_token("acc-1")
            self.assertIsNotNone(loaded_token)
            assert loaded_token is not None
            self.assertEqual(loaded_token["access_token"], "access")

    def test_log_retention(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StoreService(project_root=root, data_dir=root / "data", max_log_lines=1000)

            for i in range(1105):
                store.append_event(level="info", event="log.test", message=f"line-{i}")

            lines = (root / "data" / "logs" / "events.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1000)
            first = json.loads(lines[0])
            self.assertTrue(str(first["message"]).startswith("line-"))

    def test_runtime_config_defaults_present(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StoreService(project_root=root, data_dir=root / "data")
            cfg = store.load_runtime_config()

            self.assertEqual(cfg["pool_target"], 20)
            self.assertEqual(cfg["register_concurrency"], 5)
            self.assertIsNone(cfg["proxy"])
            self.assertNotIn("provider_api_key", cfg)
            self.assertNotIn("provider_base_url", cfg)
            self.assertNotIn("default_model", cfg)

    def test_runtime_config_update_and_sanitize(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StoreService(project_root=root, data_dir=root / "data")

            cfg = store.update_runtime_config(
                {
                    "pool_target": 0,
                    "register_concurrency": -1,
                    "proxy": "  http://127.0.0.1:7890  ",
                    "provider_api_key": "should_be_ignored",
                }
            )

            self.assertEqual(cfg["pool_target"], 1)
            self.assertEqual(cfg["register_concurrency"], 1)
            self.assertEqual(cfg["proxy"], "http://127.0.0.1:7890")
            self.assertNotIn("provider_api_key", cfg)

    def test_runtime_config_load_filters_legacy_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StoreService(project_root=root, data_dir=root / "data")

            legacy = {
                "pool_target": 30,
                "register_concurrency": 8,
                "provider_base_url": "https://api.openai.com/v1",
                "provider_api_key": "sk-legacy",
                "default_model": "gpt-4.1-mini",
            }
            config_path = root / "data" / "config" / "runtime_config.json"
            config_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")

            cfg = store.load_runtime_config()
            self.assertEqual(cfg["pool_target"], 30)
            self.assertEqual(cfg["register_concurrency"], 8)
            self.assertNotIn("provider_base_url", cfg)
            self.assertNotIn("provider_api_key", cfg)
            self.assertNotIn("default_model", cfg)

            persisted = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("provider_base_url", persisted)
            self.assertNotIn("provider_api_key", persisted)
            self.assertNotIn("default_model", persisted)

    def test_delete_accounts_batch_and_all(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = StoreService(project_root=root, data_dir=root / "data")

            for idx in range(1, 4):
                token_data = {
                    "access_token": f"access-{idx}",
                    "refresh_token": f"refresh-{idx}",
                    "account_id": f"acc-{idx}",
                    "email": f"user{idx}@example.com",
                }
                store.save_account(token_data)

            result_batch = store.delete_accounts(["acc-1", "acc-2", "missing"], delete_token_file=True)
            self.assertEqual(result_batch["requested"], 3)
            self.assertEqual(result_batch["deleted"], 2)
            self.assertEqual(len(result_batch["not_found"]), 1)
            self.assertEqual(store.count_active_accounts(), 1)

            result_all = store.delete_all_accounts(delete_token_file=True)
            self.assertEqual(result_all["deleted"], 1)
            self.assertEqual(store.count_active_accounts(), 0)


if __name__ == "__main__":
    unittest.main()
