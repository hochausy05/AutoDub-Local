import unittest
import tempfile
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.database import Database, SCHEMA_VERSION


class LocalApiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name) / "data"
        self.app = create_app("smoke-test-secret", data_root=self.data_root)
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))
        self.client.__enter__()
        self.headers = {
            "host": "127.0.0.1:8000",
            "origin": "http://127.0.0.1:5173",
            "sec-fetch-site": "same-origin",
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def bootstrap(self):
        response = self.client.post(
            "/api/v1/session/bootstrap",
            headers={**self.headers, "content-type": "application/json", "x-local-bootstrap": "smoke-test-secret"},
            json={},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def create(self, name="Project test", key="test-idempotency-key-0001", csrf=None):
        return self.client.post(
            "/api/v1/projects",
            headers={**self.headers, "x-csrf-token": csrf or self.bootstrap(), "idempotency-key": key},
            json={"name": name},
        )

    def test_health_requires_session_and_reports_only_liveness(self):
        anonymous = self.client.get("/api/v1/health", headers=self.headers)
        self.assertEqual(anonymous.status_code, 401)

        self.bootstrap()
        response = self.client.get("/api/v1/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "autodub-local-backend"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_host_origin_and_fetch_metadata_are_exact(self):
        self.assertEqual(self.client.get("/api/v1/health", headers={**self.headers, "host": "localhost:8000"}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/health", headers={**self.headers, "origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/health", headers={**self.headers, "sec-fetch-site": "cross-site"}).status_code, 403)

    def test_bootstrap_is_one_time_and_logout_requires_csrf(self):
        csrf = self.bootstrap()
        replay = self.client.post(
            "/api/v1/session/bootstrap",
            headers={**self.headers, "content-type": "application/json", "x-local-bootstrap": "smoke-test-secret"},
            json={},
        )
        self.assertEqual(replay.status_code, 410)

        denied = self.client.post("/api/v1/session/logout", headers=self.headers, json={})
        self.assertEqual(denied.status_code, 403)
        accepted = self.client.post(
            "/api/v1/session/logout",
            headers={**self.headers, "x-csrf-token": csrf},
            json={},
        )
        self.assertEqual(accepted.status_code, 204)
        self.assertEqual(self.client.get("/api/v1/health", headers=self.headers).status_code, 401)

    def test_openapi_is_not_exposed(self):
        self.bootstrap()
        self.assertEqual(self.client.get("/docs", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/openapi.json", headers=self.headers).status_code, 404)

    def test_media_selection_and_listing_keep_existing_session_csrf_guards(self):
        self.assertEqual(self.client.post(f"/api/v1/projects/{'00000000-0000-4000-8000-000000000001'}/selections", headers={**self.headers, "content-type": "application/json"}, json={}).status_code, 401)
        csrf = self.bootstrap()
        project_id = self.create(csrf=csrf).json()["id"]
        route = f"/api/v1/projects/{project_id}/selections"
        self.assertEqual(self.client.post(route, headers={**self.headers, "content-type": "application/json"}, json={}).status_code, 403)
        self.assertEqual(self.client.post(route, headers={**self.headers, "content-type": "application/json", "x-csrf-token": csrf}, json={"path": "C:\\private\\file.mp4"}).status_code, 422)
        listed = self.client.get(f"/api/v1/projects/{project_id}/media", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json(), {"items": []})
        self.assertNotIn("path", listed.text.lower())

    def test_media_import_post_uses_json_body_not_required_query_parameter(self):
        csrf = self.bootstrap()
        project_id = self.create(csrf=csrf).json()["id"]
        response = self.client.post(
            f"/api/v1/projects/{project_id}/media/imports",
            headers={**self.headers, "content-type": "application/json", "x-csrf-token": csrf,
                     "idempotency-key": "import-contract-regression-0001"},
            json={"selection_id": "a" * 64, "mode": "COPY"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "selection_invalid")
        missing_mode = self.client.post(
            f"/api/v1/projects/{project_id}/media/imports",
            headers={**self.headers, "content-type": "application/json", "x-csrf-token": csrf,
                     "idempotency-key": "import-missing-mode-idempotency-01"},
            json={"selection_id": "a" * 64},
        )
        self.assertEqual(missing_mode.status_code, 422)
        self.assertEqual(missing_mode.json()["error"]["code"], "validation_error")
        self.assertEqual(missing_mode.json()["error"]["details"][0]["field"], "mode")

    def test_first_startup_creates_database_and_configures_sqlite(self):
        self.assertTrue(self.app.state.database.path.is_file())
        with closing(self.app.state.database.connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO idempotency_records(scope,idempotency_key,request_digest,project_id,response_json,created_at) VALUES('test','bad-fk','digest','missing-project','{}','now')"
                )

    def test_data_root_rejects_source_tree_and_unc_paths(self):
        source_root = Path(__file__).resolve().parents[2]
        with self.assertRaisesRegex(ValueError, "outside the application source tree"):
            Database(source_root)
        with self.assertRaisesRegex(ValueError, "UNC paths"):
            Database(r"\\server\share\AutoDub")

    def test_second_startup_and_migration_are_idempotent_and_preserve_data(self):
        csrf = self.bootstrap()
        created = self.create("Persistent", csrf=csrf)
        project_id = created.json()["id"]
        self.client.__exit__(None, None, None)
        reopened = TestClient(create_app("next-secret", data_root=self.data_root), client=("127.0.0.1", 50001))
        with reopened:
            with closing(reopened.app.state.database.connect()) as connection:
                self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            headers = {**self.headers, "x-local-bootstrap": "next-secret", "content-type": "application/json"}
            session = reopened.post("/api/v1/session/bootstrap", headers=headers, json={}).json()
            got = reopened.get(f"/api/v1/projects/{project_id}", headers=self.headers)
            self.assertEqual(got.status_code, 200)
            self.assertEqual(got.json()["name"], "Persistent")
            self.assertTrue(session["csrf_token"])

    def test_project_create_read_list_and_idempotency(self):
        csrf = self.bootstrap()
        created = self.create("  Alpha  ", csrf=csrf)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["lock_version"], 1)
        replay = self.create("Alpha", csrf=csrf)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json(), created.json())
        self.assertEqual(replay.headers["idempotency-replayed"], "true")
        conflict = self.create("Different", csrf=csrf)
        self.assertEqual(conflict.status_code, 409)
        project_id = created.json()["id"]
        item = self.client.get(f"/api/v1/projects/{project_id}", headers=self.headers)
        listing = self.client.get("/api/v1/projects", headers=self.headers)
        self.assertEqual(item.status_code, 200)
        self.assertEqual(item.json()["name"], "Alpha")
        self.assertEqual(len(listing.json()["items"]), 1)

    def test_project_mutations_require_session_csrf_and_valid_input(self):
        unauthenticated = self.client.post(
            "/api/v1/projects",
            headers={**self.headers, "idempotency-key": "test-idempotency-key-0001"},
            json={"name": "Denied"},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        csrf = self.bootstrap()
        missing_csrf = self.client.post(
            "/api/v1/projects",
            headers={**self.headers, "idempotency-key": "test-idempotency-key-0002"},
            json={"name": "Denied"},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        blank = self.create("   ", key="test-idempotency-key-0003", csrf=csrf)
        self.assertEqual(blank.status_code, 422)
        unknown_field = self.client.post(
            "/api/v1/projects",
            headers={**self.headers, "x-csrf-token": csrf, "idempotency-key": "test-idempotency-key-0004"},
            json={"name": "Okay", "database_path": "C:\\private"},
        )
        self.assertEqual(unknown_field.status_code, 422)
        self.assertEqual(self.client.get("/api/v1/projects", headers=self.headers).status_code, 200)
        bad_id = self.client.get("/api/v1/projects/not-a-uuid", headers=self.headers)
        self.assertEqual(bad_id.status_code, 422)

    def test_concurrent_project_creates_preserve_all_records(self):
        csrf = self.bootstrap()

        def create_one(index):
            return self.client.post(
                "/api/v1/projects",
                headers={**self.headers, "x-csrf-token": csrf, "idempotency-key": f"concurrent-project-key-{index:04d}"},
                json={"name": f"Project {index}"},
            ).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(create_one, range(12)))
        self.assertEqual(statuses, [201] * 12)
        self.assertEqual(len(self.client.get("/api/v1/projects?limit=100", headers=self.headers).json()["items"]), 12)


if __name__ == "__main__":
    unittest.main()
