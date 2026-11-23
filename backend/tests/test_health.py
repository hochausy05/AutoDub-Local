import unittest

from fastapi.testclient import TestClient

from app.main import create_app


class LocalApiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app("smoke-test-secret"), client=("127.0.0.1", 50000))
        self.headers = {
            "host": "127.0.0.1:8000",
            "origin": "http://127.0.0.1:5173",
            "sec-fetch-site": "same-origin",
        }

    def bootstrap(self):
        response = self.client.post(
            "/api/v1/session/bootstrap",
            headers={**self.headers, "content-type": "application/json", "x-local-bootstrap": "smoke-test-secret"},
            json={},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

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

    def test_unimplemented_apis_and_openapi_are_not_exposed(self):
        self.bootstrap()
        self.assertEqual(self.client.get("/docs", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/openapi.json", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/projects", headers=self.headers).status_code, 404)


if __name__ == "__main__":
    unittest.main()
