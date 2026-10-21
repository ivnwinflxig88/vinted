import contextlib
import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from adspower_manager import AdsPower, LocalError, LocalServer, Manager, NAMES


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class FakeAPI(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.rows = []
        self.calls = []
        self.key = "test-only-key"
        self.page_size = 100
        self.fail_at = None
        self.create_count = 0
        self.missing_create_id = False
        self.repeat_page = False
        self.bad_list = False
        self.redirect_to = None

    def client(self, key=None):
        return AdsPower(self.server_port, self.key if key is None else key, interval=0)


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        action = self.path.rsplit("/", 1)[-1]
        self.server.calls.append((action, payload, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != f"Bearer {self.server.key}":
            return self.respond({"code": -1, "msg": "secret echoed: wrong-key"}, 401)
        if self.server.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        if action == "list":
            if self.server.bad_list:
                return self.respond({"code": 0, "data": {}})
            start = 0 if self.server.repeat_page else (payload["page"] - 1) * self.server.page_size
            rows = self.server.rows[start:start + self.server.page_size]
            return self.respond({"code": 0, "data": {"list": rows}})
        if action == "create":
            self.server.create_count += 1
            if self.server.create_count == self.server.fail_at:
                return self.respond({"code": -1, "msg": "profile quota limit; test-only-key"})
            row = {"name": payload["name"], "profile_id": f"id-{len(self.server.rows) + 1}"}
            self.server.rows.append(row)
            data = {} if self.server.missing_create_id else {"profile_id": row["profile_id"]}
            return self.respond({"code": 0, "data": data})
        if action == "start":
            return self.respond({"code": 0, "data": {"ws": "do-not-display-debug-endpoint"}})
        self.respond({"code": -1, "msg": "unexpected operation"}, 404)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.context = running(self.api)
        self.context.__enter__()
        self.manager = Manager(self.api.client(), "it")

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def test_reuses_existing_profile_without_updating_its_settings(self):
        self.api.rows = [{"name": "personal", "profile_id": "keep-0"},
                         {"name": "Vinted-01", "profile_id": "keep-1", "cookie": "private"}]
        before = copy.deepcopy(self.api.rows)
        result = self.manager.create()
        self.assertIsNone(result["error"])
        self.assertEqual(result["rows"][0]["state"], "existing")
        self.assertEqual(self.api.rows[:2], before)
        self.assertEqual(self.api.create_count, 3)
        self.manager.create()
        self.assertEqual(self.api.create_count, 3)
        for action, payload, auth in self.api.calls:
            self.assertEqual(auth, "Bearer test-only-key")
            self.assertIn(action, ("list", "create"))
            if action == "create":
                self.assertEqual(set(payload), {"name", "group_id", "user_proxy_config", "tabs", "fingerprint_config"})
                self.assertEqual(payload["user_proxy_config"], {"proxy_soft": "no_proxy"})
                self.assertEqual(payload["tabs"], ["https://www.vinted.it/"])

    def test_checks_all_pages_even_when_api_caps_page_size(self):
        self.api.page_size = 1
        self.api.rows = [{"name": "other", "profile_id": "a"}, {"name": "Vinted-01", "profile_id": "b"}]
        self.assertEqual(self.manager.plan()[0]["profile_id"], "b")
        self.assertEqual([call[1]["page"] for call in self.api.calls], [1, 2, 3])

    def test_partial_quota_failure_preserves_results_and_rerun_resumes(self):
        self.api.fail_at = 3
        result = self.manager.create()
        self.assertEqual([row["state"] for row in result["rows"]], ["created", "created", "missing", "missing"])
        self.assertIn("额度", result["error"])
        self.assertNotIn(self.api.key, result["error"])
        self.api.fail_at = None
        result = self.manager.create()
        self.assertIsNone(result["error"])
        self.assertEqual([row["name"] for row in self.api.rows], list(NAMES))
        self.assertEqual(len({row["profile_id"] for row in self.api.rows}), 4)

    def test_success_without_id_stops_and_rescan_finds_profile(self):
        self.api.missing_create_id = True
        result = self.manager.create()
        self.assertIn("可能已经创建", result["error"])
        self.assertEqual(self.api.create_count, 1)
        self.api.missing_create_id = False
        self.manager.create()
        self.assertEqual(self.api.create_count, 4)

    def test_duplicate_names_stop_before_any_mutation(self):
        self.api.rows = [{"name": "Vinted-01", "profile_id": "a"}, {"name": "Vinted-01", "profile_id": "b"}]
        with self.assertRaisesRegex(LocalError, "多个同名"):
            self.manager.create()
        self.assertEqual(self.api.create_count, 0)

    def test_malformed_or_repeated_pages_stop_before_mutation(self):
        self.api.bad_list = True
        with self.assertRaisesRegex(LocalError, "列表格式"):
            self.manager.create()
        self.api.bad_list = False
        self.api.repeat_page = True
        self.api.rows = [{"name": "Vinted-01", "profile_id": "a"}]
        with self.assertRaisesRegex(LocalError, "分页重复"):
            self.manager.create()
        self.assertEqual(self.api.create_count, 0)

    def test_starts_visible_selected_profile_without_exposing_debug_data(self):
        self.api.rows = [{"name": "Vinted-02", "user_id": "profile-two"}]
        result = self.manager.start("Vinted-02")
        self.assertIn("手动登录", result["message"])
        self.assertNotIn("ws", result)
        self.assertEqual(self.api.calls[-1][1], {"profile_id": "profile-two", "headless": "0", "launch_args": ["https://www.vinted.it/"]})
        with self.assertRaises(LocalError):
            self.manager.start("personal")

    def test_auth_failure_redacts_remote_response(self):
        with self.assertRaisesRegex(LocalError, "验证失败") as caught:
            self.api.client("wrong-key").profiles()
        self.assertNotIn("wrong-key", str(caught.exception))
        self.assertNotIn("echoed", str(caught.exception))

    def test_does_not_follow_redirect_or_forward_key(self):
        with running(FakeAPI()) as target:
            self.api.redirect_to = f"http://127.0.0.1:{target.server_port}/capture"
            with self.assertRaisesRegex(LocalError, "HTTP 302"):
                self.manager.plan()
            self.assertEqual(target.calls, [])

    def test_ignores_environment_proxy(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
            self.assertEqual(len(Manager(self.api.client(), "fr").plan()), 4)


class WebTests(unittest.TestCase):
    def test_local_page_auth_connect_create_and_start_over_http(self):
        with running(FakeAPI()) as api, running(LocalServer()) as ui:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def post(path, body, origin=None, token=None, host=None):
                headers = {"Content-Type": "application/json", "Origin": ui.origin if origin is None else origin,
                           "X-Local-Token": ui.token if token is None else token}
                if host:
                    headers["Host"] = host
                req = urllib.request.Request(ui.origin + path, json.dumps(body).encode(), headers, method="POST")
                try:
                    response = opener.open(req, timeout=15)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, json.load(response)

            for path in ("/", "/style.css", "/app.js"):
                with opener.open(ui.origin + path) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertTrue(response.read())
            self.assertEqual(post("/api/connect", {}, token="")[0], 403)
            self.assertEqual(post("/api/connect", {}, origin="https://attacker.invalid")[0], 403)
            self.assertEqual(post("/api/connect", {}, host="attacker.invalid")[0], 403)
            self.assertEqual(api.calls, [])
            self.assertEqual(post("/api/create", {})[0], 400)
            status, result = post("/api/connect", {"port": api.server_port, "key": api.key, "site": "fr"})
            self.assertEqual(status, 200)
            self.assertEqual(len(result["rows"]), 4)
            ui.manager.client.interval = 0
            status, result = post("/api/create", {})
            self.assertEqual(status, 200)
            self.assertIsNone(result["error"])
            status, result = post("/api/start", {"name": "Vinted-01"})
            self.assertEqual(status, 200)
            self.assertIn("手动登录", result["message"])
            self.assertNotIn(api.key, json.dumps(result))
            self.assertEqual(post("/api/connect", {"port": ui.server_port, "key": ""})[0], 400)


if __name__ == "__main__":
    unittest.main()
