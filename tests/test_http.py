"""HTTP 端到端冒烟测试：真实起服务 + urllib 调用 JSON API。"""

import json
import sys
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from escalator_platform.demo_data import build_service
from escalator_platform.server import Handler

ALL_PASS = {"steps": "pass", "comb": "pass", "handrail": "pass",
            "lighting": "pass", "estop": "pass"}


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        tmp = tempfile.mkdtemp()
        cls.svc = build_service(f"{tmp}/p.db", f"{tmp}/photos", reset=True)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.svc = cls.svc
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_served(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/", timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("扶梯", r.read().decode())

    def test_full_fault_flow_over_http(self):
        s, _ = self.call("/api/escalators/E34-A/lockdown",
                         {"staff_id": "D01", "reason": "fault", "detail": "异响"})
        self.assertEqual(s, 200)
        code, body = self.call("/api/escalators/E34-A/reopen",
                               {"staff_id": "D01"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "barrier_missing")

        self.call("/api/escalators/E34-A/barrier",
                  {"staff_id": "D01", "in_place": True})
        self.call("/api/escalators/E34-A/repair",
                  {"staff_id": "I01", "description": "故障"})
        # 客服不能报修
        code, body = self.call("/api/escalators/E34-A/repair",
                               {"staff_id": "C01", "description": "x"})
        self.assertEqual(body["error"], "duty_separation")
        self.call("/api/escalators/E34-A/arrive", {"staff_id": "M01"})
        self.call("/api/escalators/E34-A/fixed", {"staff_id": "M01"})
        _, rc = self.call("/api/escalators/E34-A/recheck",
                          {"staff_id": "I01"})
        rid = rc["recheck_id"]
        code, body = self.call(f"/api/rechecks/{rid}/decide",
                               {"staff_id": "I01", "approve": True})
        self.assertEqual(body["error"], "duty_separation")
        self.call(f"/api/rechecks/{rid}/decide",
                  {"staff_id": "I02", "approve": True})
        self.call("/api/escalators/E34-A/barrier",
                  {"staff_id": "D01", "in_place": False})
        code, body = self.call("/api/escalators/E34-A/reopen",
                               {"staff_id": "D01"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "stopped")

    def test_start_mutex_and_version_over_http(self):
        self.call("/api/escalators/E12-A/precheck",
                  {"staff_id": "I01", "items": ALL_PASS})
        self.assertEqual(self.call("/api/escalators/E12-A/start/prepare",
                                   {"staff_id": "D01"})[0], 200)
        code, body = self.call("/api/escalators/E12-A/start/prepare",
                               {"staff_id": "D02"})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "start_conflict")
        # 错误方向
        code, body = self.call("/api/escalators/E12-A/start",
                               {"staff_id": "D01", "direction": "down",
                                "version": 1})
        self.assertEqual(body["error"], "direction_mismatch")
        # D01 按当前版本启动成功
        self.assertEqual(self.call("/api/escalators/E12-A/start",
                                   {"staff_id": "D01", "direction": "up",
                                    "version": 1})[0], 200)

    def test_events_endpoint(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/events?limit=5",
                timeout=5) as r:
            data = json.loads(r.read())
        self.assertIsInstance(data, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
