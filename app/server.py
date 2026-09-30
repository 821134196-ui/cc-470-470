"""HTTP 服务：JSON API + 静态前端 + 故障照片本地存储。仅用标准库。"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import domain as D
from .domain import iso
from .service import BusinessError

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class AppContext:
    def __init__(self, service, cabinet, crowd, scheduler, photo_dir: Path):
        self.service = service
        self.cabinet = cabinet
        self.crowd = crowd
        self.scheduler = scheduler
        self.photo_dir = photo_dir
        self.photo_dir.mkdir(parents=True, exist_ok=True)


def make_handler(ctx: AppContext):
    svc = ctx.service

    class Handler(BaseHTTPRequestHandler):
        server_version = "EscalatorPlatform/1.0"

        def log_message(self, fmt, *args):  # 安静一点
            pass

        # ---------- 响应工具 ----------
        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code: str, message: str, status: int = 400):
            self._json({"ok": False, "error_code": code, "error": message}, status)

        def _static(self, rel: str):
            target = (WEB_DIR / rel).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
                self.send_error(404)
                return
            ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---------- GET ----------
        def do_GET(self):
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            try:
                if path == "/api/state":
                    self._json(self._state())
                elif path == "/api/detail":
                    self._json({"ok": True, **svc.detail(qs["code"][0])})
                elif path == "/api/lockdowns":
                    self._json({"ok": True, "lockdowns": svc.lockdowns()})
                elif path == "/api/faults":
                    self._json({"ok": True, "faults": [dict(r) for r in svc.open_faults()]})
                elif path == "/api/reinspections":
                    rows = svc.list_reinspections(pending_only=qs.get("pending", ["0"])[0] == "1")
                    self._json({"ok": True, "reinspections": [dict(r) for r in rows]})
                elif path == "/api/events":
                    code = qs.get("code", [None])[0]
                    rows = svc.events(code, limit=int(qs.get("limit", ["300"])[0]))
                    self._json({"ok": True, "events": [dict(r) for r in rows]})
                elif path == "/api/detours":
                    self._json({"ok": True, "detours": [dict(r) for r in svc.active_detours()]})
                elif path == "/api/photos":
                    self._json({"ok": True, "photos": [dict(r) for r in
                                                        svc.photos(int(qs["fault_id"][0]))]})
                elif path == "/media/photos":
                    name = Path(qs["name"][0]).name
                    target = ctx.photo_dir / name
                    if not target.is_file():
                        self.send_error(404)
                        return
                    self._serve_bytes(target.read_bytes(),
                                      CONTENT_TYPES.get(target.suffix, "image/jpeg"))
                elif path == "/" or path == "/index.html":
                    self._static("index.html")
                elif path.startswith("/"):
                    self._static(path.lstrip("/"))
                else:
                    self.send_error(404)
            except BusinessError as e:
                self._error(e.code, e.message, 409)
            except KeyError as e:
                self._error("MISSING_PARAM", f"缺少参数：{e}", 400)

        def _serve_bytes(self, data: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _state(self):
            return {
                "ok": True,
                "people": [dict(r) for r in svc.list_people()],
                "escalators": [dict(r) for r in svc.list_escalators()],
                "detours": [dict(r) for r in svc.active_detours()],
                "check_items": [{"key": k, "label": l, "critical": c}
                                for k, l, c in D.CHECK_ITEMS],
                "now": iso(svc.clock.now()),
            }

        # ---------- POST ----------
        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/api/photo":
                    self._upload_photo()
                    return
                payload = self._read_json()
                person = payload.pop("person", None)
                if path == "/api/direction":
                    v = svc.set_direction(payload["code"], payload["direction"], person)
                    self._json({"ok": True, "config_version": v})
                elif path == "/api/inspection/open":
                    iid = svc.open_or_get_inspection(payload["code"], person)
                    self._json({"ok": True, "inspection_id": iid})
                elif path == "/api/inspection/item":
                    svc.record_check_item(int(payload["inspection_id"]), payload["key"],
                                          payload["result"], person, payload.get("remark", ""))
                    self._json({"ok": True})
                elif path == "/api/estop-test":
                    r = svc.submit_emergency_test(payload["code"], person, payload["result"],
                                                  payload.get("response_ms"),
                                                  payload.get("detail", ""))
                    self._json({"ok": True, **r})
                elif path == "/api/start/acquire":
                    self._json({"ok": True, **svc.acquire_start(
                        payload["code"], person, int(payload["version"]))})
                elif path == "/api/start/release":
                    svc.release_start(payload["code"], person)
                    self._json({"ok": True})
                elif path == "/api/start":
                    self._json({"ok": True, **svc.start(
                        payload["code"], person, int(payload["version"]))})
                elif path == "/api/stop":
                    svc.stop(payload["code"], person, payload.get("reason", "手动停梯"))
                    self._json({"ok": True})
                elif path == "/api/seal":
                    fid = svc.seal(payload["code"], person, payload["source"], payload["reason"])
                    self._json({"ok": True, "fault_id": fid})
                elif path == "/api/barricade/place":
                    svc.place_barricade(int(payload["fault_id"]), person)
                    self._json({"ok": True})
                elif path == "/api/repair/dispatch":
                    svc.dispatch(int(payload["fault_id"]), payload["repair"], person)
                    self._json({"ok": True})
                elif path == "/api/repair/enroute":
                    svc.enroute(int(payload["fault_id"]), person)
                    self._json({"ok": True})
                elif path == "/api/repair/arrive":
                    self._json({"ok": True, **svc.arrive(int(payload["fault_id"]), person)})
                elif path == "/api/repair/begin":
                    svc.begin_repair(int(payload["fault_id"]), person, payload.get("note", ""))
                    self._json({"ok": True})
                elif path == "/api/repair/complete":
                    svc.complete_repair(int(payload["fault_id"]), person, payload.get("note", ""))
                    self._json({"ok": True})
                elif path == "/api/reinspection/submit":
                    rid = svc.submit_reinspection(int(payload["fault_id"]), person,
                                                  payload["result"], payload.get("note", ""))
                    self._json({"ok": True, "reinspection_id": rid})
                elif path == "/api/reinspection/approve":
                    svc.approve_reinspection(int(payload["id"]), person,
                                             bool(payload["approve"]), payload.get("reason", ""))
                    self._json({"ok": True})
                elif path == "/api/reopen":
                    self._json({"ok": True, **svc.remove_barricade_and_reopen(
                        int(payload["fault_id"]), person, confirmed=bool(payload.get("confirmed")))})
                elif path == "/api/detour/publish":
                    did = svc.publish_detour(payload["code"], person, payload["message"],
                                             payload.get("floor"))
                    self._json({"ok": True, "detour_id": did})
                elif path == "/api/detour/retract":
                    svc.retract_detour(int(payload["id"]), person)
                    self._json({"ok": True})
                elif path == "/api/mock/trip":
                    ctx.cabinet.arm_trip(payload["code"])
                    self._json({"ok": True, "mock": "下次启动将跳停"})
                elif path == "/api/mock/fault":
                    ctx.cabinet.inject_running_fault(payload["code"],
                                                     payload.get("fault_code", "E-44 主机超速保护"))
                    self._json({"ok": True, "mock": "运行中故障已注入"})
                elif path == "/api/mock/incident":
                    ctx.crowd.arm_incident(payload["code"])
                    self._json({"ok": True, "mock": "客流事件已注入，下一轮询触发封控"})
                elif path == "/api/mock/load":
                    ctx.crowd.set_load(payload["code"], int(payload.get("people", 0)))
                    self._json({"ok": True})
                elif path == "/api/scheduler/tick":
                    self._json({"ok": True, **ctx.scheduler.tick()})
                else:
                    self._error("NOT_FOUND", f"未知接口：{path}", 404)
            except BusinessError as e:
                self._error(e.code, e.message, 409)
            except KeyError as e:
                self._error("MISSING_PARAM", f"缺少参数：{e}", 400)
            except (ValueError, TypeError) as e:
                self._error("BAD_REQUEST", str(e), 400)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise BusinessError("BAD_JSON", "请求体不是合法 JSON")
            return data if isinstance(data, dict) else {}

        # ---------- 照片上传（multipart，手工解析） ----------
        def _upload_photo(self):
            ctype = self.headers.get("Content-Type", "")
            m = re.search(r"boundary=([^;]+)", ctype)
            if "multipart/form-data" not in ctype or not m:
                self._error("BAD_UPLOAD", "需要 multipart/form-data 上传", 400)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            boundary = m.group(1).strip('"').encode()
            fields, files = _parse_multipart(body, boundary)
            fault_id = int(fields.get("fault_id", ""))
            person = fields.get("person")
            upload = files.get("file")
            if not upload:
                self._error("NO_FILE", "未收到照片文件", 400)
                return
            filename, content = upload
            ext = Path(filename).suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                self._error("BAD_FILETYPE", "仅支持 jpg/png/webp/gif 照片", 400)
                return
            safe = f"fault{fault_id}_{uuid.uuid4().hex[:10]}{ext}"
            (ctx.photo_dir / safe).write_bytes(content)
            rel = f"/media/photos?name={safe}"
            pid = svc.add_photo(fault_id, rel, filename, person)
            self._json({"ok": True, "photo_id": pid, "url": rel})

    return Handler


def _parse_multipart(body: bytes, boundary: bytes):
    """极简 multipart 解析：返回 (普通字段 dict, 文件 dict[name]=(filename, bytes))。"""
    fields: dict[str, str] = {}
    files: dict[str, tuple] = {}
    delim = b"--" + boundary
    for part in body.split(delim):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        headers = header_blob.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]+)"', headers)
        file_m = re.search(r'filename="([^"]*)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        content = content.rstrip(b"\r\n")
        if file_m and file_m.group(1):
            files[name] = (file_m.group(1), content)
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def serve(host: str, port: int, ctx: AppContext) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(ctx))
    return httpd
