"""标准库 HTTP 服务：JSON API + 静态前端。"""

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta

from . import errors as E
from .scheduler import Scheduler

WEB_DIR = Path(__file__).parent / "web"


class Handler(BaseHTTPRequestHandler):
    server_version = "EscPlatform/1.0"

    # 注入到 server 实例: server.svc / server.scheduler
    @property
    def svc(self):
        return self.server.svc

    def log_message(self, fmt, *args):
        print("[http] " + fmt % args)

    # ------------------------------------------------------------ helpers
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype or raw[:1] in (b"{", b"["):
            try:
                return json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                raise E.PlatformError("请求体不是合法 JSON")
        # 简单表单（照片直传等）
        return {"_raw": raw, "_content_type": ctype,
                **{k: v[0] for k, v in parse_qs(raw.decode()).items()}}

    def _handle_error(self, exc: Exception):
        if isinstance(exc, E.PlatformError):
            self._json(exc.to_dict(), exc.http_status)
        else:
            self._json({"error": "internal", "message": str(exc)}, 500)

    # ------------------------------------------------------------ routing
    def do_GET(self):
        try:
            url = urlparse(self.path)
            path = url.path
            qs = parse_qs(url.query)
            if path == "/api/staff":
                return self._json(self.svc.list_staff())
            if path == "/api/escalators":
                return self._json(self.svc.list_escalators())
            if path.startswith("/api/escalators/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    return self._json(self.svc.escalator_detail(parts[2]))
            if path == "/api/events":
                return self._json(self.svc.events(
                    qs.get("esc_id", [None])[0],
                    qs.get("type", [None])[0],
                    int(qs.get("limit", ["200"])[0])))
            if path == "/api/rechecks/pending":
                return self._json(self.svc.pending_rechecks())
            if path.startswith("/uploads/"):
                return self._serve_upload(path.split("/", 2)[2])
            return self._serve_static(path)
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self):
        try:
            url = urlparse(self.path)
            path = url.path
            body = self._body()
            s = self.svc

            if path.startswith("/api/escalators/"):
                parts = path.strip("/").split("/")
                if len(parts) < 4:
                    raise E.NotFound("未知接口")
                eid, action = parts[2], parts[3]
                staff_id = body.get("staff_id")

                if action == "direction":
                    v = s.set_direction(staff_id, eid, body["direction"])
                    return self._json({"ok": True, "version": v})
                if action == "schedule":
                    s.set_schedule(staff_id, eid, body["on_time"],
                                   body["off_time"], bool(body.get("enabled", True)))
                    return self._json({"ok": True})
                if action == "precheck":
                    s.submit_precheck(staff_id, eid, body["items"])
                    return self._json({"ok": True, "all_passed": True})
                if action == "start" and len(parts) == 5:
                    if parts[4] == "prepare":
                        info = s.prepare_start(staff_id, eid)
                        return self._json({"ok": True, **info})
                    if parts[4] == "cancel":
                        s.release_start_lock(staff_id, eid)
                        return self._json({"ok": True})
                if action == "start":
                    s.start(staff_id, eid, body["direction"], int(body["version"]))
                    return self._json({"ok": True, "status": "running"})
                if action == "stop":
                    s.stop(staff_id, eid, body.get("reason", "manual"))
                    return self._json({"ok": True})
                if action == "lockdown":
                    lid = s.lock_down(eid, body["reason"], body.get("detail", ""),
                                      staff_id)
                    return self._json({"ok": True, "lockdown_id": lid})
                if action == "barrier":
                    s.set_barrier(staff_id, eid, bool(body.get("in_place", True)))
                    return self._json({"ok": True})
                if action == "repair":
                    photo = None
                    if body.get("photo_data"):
                        photo = s.save_photo(body.get("filename", "photo.jpg"),
                                             bytes.fromhex(body["photo_data"]))
                    oid = s.report_repair(staff_id, eid,
                                          body.get("description", "故障报修"), photo)
                    return self._json({"ok": True, "order_id": oid})
                if action == "arrive":
                    s.arrive(staff_id, eid)
                    return self._json({"ok": True})
                if action == "fixed":
                    s.mark_fixed(staff_id, eid, body.get("note", ""))
                    return self._json({"ok": True})
                if action == "rework":
                    s.rework(staff_id, eid, body.get("note", ""))
                    return self._json({"ok": True})
                if action == "recheck":
                    rid = s.submit_recheck(staff_id, eid, body.get("note", ""))
                    return self._json({"ok": True, "recheck_id": rid})
                if action == "reopen":
                    s.reopen(staff_id, eid)
                    return self._json({"ok": True, "status": "stopped"})
                if action == "trip":
                    r = s.handle_trip(eid, source="manual",
                                      code=body.get("code"))
                    return self._json({"ok": True, "result": r})
                if action == "detour":
                    did = s.publish_detour(staff_id, eid, body["message"])
                    return self._json({"ok": True, "detour_id": did})
                if action == "inject":
                    kind = body.get("kind", "crowd")
                    until = s.clock() + timedelta(minutes=int(body.get("minutes", 30)))
                    if kind == "crowd":
                        s.crowd.inject(eid, body.get("level", "critical"), until)
                    elif kind == "trip":
                        s.cabinet.schedule_trip(eid, s.clock())
                    elif kind == "fault":
                        s.cabinet.inject_fault(eid, body.get("code", "E12_OVERLOAD"))
                    else:
                        raise E.PlatformError("未知注入类型")
                    return self._json({"ok": True})
            if path.startswith("/api/rechecks/") and path.endswith("/decide"):
                rid = int(path.strip("/").split("/")[2])
                s.decide_recheck(body["staff_id"], rid,
                                 bool(body.get("approve", True)),
                                 body.get("note", ""))
                return self._json({"ok": True})
            if path.startswith("/api/detours/") and path.endswith("/cancel"):
                did = int(path.strip("/").split("/")[2])
                s.cancel_detour(body["staff_id"], did)
                return self._json({"ok": True})
            raise E.NotFound(f"未知接口: {path}")
        except Exception as exc:
            self._handle_error(exc)

    # ------------------------------------------------------------ static
    def _serve_static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            target = WEB_DIR / "index.html"
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         ctype + ("; charset=utf-8" if ctype.startswith("text")
                                  or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_upload(self, name):
        target = (self.svc.photo_dir / Path(name).name).resolve()
        if not str(target).startswith(str(self.svc.photo_dir.resolve())) \
                or not target.is_file():
            raise E.NotFound("照片不存在")
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(svc, host="127.0.0.1", port=8000, with_scheduler=True):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.svc = svc
    sched = None
    if with_scheduler:
        sched = Scheduler(svc, interval_sec=10)
        sched.start()
    httpd.scheduler = sched
    print(f"平台已启动: http://{host}:{port}  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中…")
    finally:
        if sched:
            sched.stop()
        httpd.server_close()
