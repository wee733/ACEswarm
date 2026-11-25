"""Loopback HTTP API for the AivudaOS recorder. Caddy owns the public prefix."""
import argparse
import json
import logging
import signal
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from bag_manager import BagManager
from preset_store import PresetStore
from recorder_engine import create_engine
from recording_options import session_name


class RecorderApp:
    def __init__(self, output_dir, use_sim_time, buffer_mib):
        self.bags = BagManager(output_dir)
        self.presets = PresetStore(self.bags.root / ".presets.json")
        self.engine = create_engine(output_dir, use_sim_time, buffer_mib)
        self.lock = threading.RLock()
        self.active_name = None
        self.shutdown = threading.Event()
        self.epoch = uuid.uuid4().hex

    def status(self):
        with self.lock:
            state = self.engine.status()
            if self.active_name and not state["recording"]:
                self.bags.unprotect(self.active_name)
                self.active_name = None
            return state

    def start(self, payload):
        with self.lock:
            if self.status()["recording"]:
                raise RuntimeError("已有录制正在进行")
            name = session_name(payload.get("name"))
            self.bags.protect(name)
            try:
                state = self.engine.start({**payload, "name": name})
            except Exception:
                self.bags.unprotect(name)
                raise
            self.active_name = name
            return state

    def stop(self):
        with self.lock:
            try:
                return self.engine.stop()
            finally:
                self.status()


def encoded(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class RecorderHandler(BaseHTTPRequestHandler):
    server_version = "ROS2Recorder/0.2"
    # Close-delimited streamed downloads and SSE are valid HTTP/1.0 responses.
    @property
    def app(self):
        return self.server.app

    def reply(self, data, code=200):
        body = encoded(data)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        raw_length = self.headers.get("Content-Length", "0")
        if not raw_length.isdecimal() or len(raw_length) > 7 or int(raw_length) > 1024 * 1024:
            raise ValueError("请求体过大或长度无效")
        if not int(raw_length):
            return {}
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请使用 application/json")
        data = json.loads(self.rfile.read(int(raw_length)))
        if not isinstance(data, dict):
            raise ValueError("请求体须为 JSON 对象")
        return data

    def events(self):
        cursor = 0
        epoch, _, seq = self.headers.get("Last-Event-ID", "").partition("/")
        if epoch == self.app.epoch and seq.isdecimal() and len(seq) <= 20:
            cursor = int(seq)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._streaming = True
        while not self.app.shutdown.is_set():
            state = self.app.status()
            self.wfile.write(b"event: status\ndata: " + encoded(state) + b"\n\n")
            for row in self.app.engine.logs(cursor):
                cursor = row["id"]
                event_id = f"id: {self.app.epoch}/{cursor}\n".encode()
                self.wfile.write(event_id + b"event: log\ndata: " + encoded(row) + b"\n\n")
            self.wfile.flush()
            self.app.shutdown.wait(0.75)

    def download(self, name, query):
        params = parse_qs(query, keep_blank_values=True)
        if set(params) - {"file"} or len(params.get("file", [])) > 1:
            raise ValueError("下载参数无效")
        filename = params.get("file", [None])[0]
        with self.app.bags.download(name, filename) as data:
            self.send_response(200)
            self.send_header("Content-Type", data.content_type)
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(data.filename, safe=""))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            if data.size is not None:
                self.send_header("Content-Length", str(data.size))
            self.end_headers()
            self._streaming = True
            data.write_to(self.wfile)

    def route(self):
        route = urlsplit(self.path)
        parts = [unquote(p, errors="strict") for p in route.path.strip("/").split("/")]
        if self.command in ("POST", "DELETE"):
            # Browsers send Origin on mutations. The proxy preserves the public Host.
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.reply({"error": "请求来源与应用地址不同"}, 403)
                return
        if self.command == "GET":
            if parts == ["topics"]:
                return self.reply({"topics": self.app.engine.topics()})
            if parts == ["recorder", "status"]:
                return self.reply(self.app.status())
            if parts == ["recorder", "events"]:
                return self.events()
            if parts == ["presets"]:
                return self.reply({"presets": self.app.presets.list()})
            if parts[0] == "bags":
                # Reconcile sessions finalized by an asynchronous ROS writer error.
                self.app.status()
                if len(parts) == 1:
                    return self.reply({"bags": self.app.bags.list()})
                if len(parts) == 2:
                    return self.reply(self.app.bags.detail(parts[1]))
                if len(parts) == 3 and parts[2] == "download":
                    return self.download(parts[1], route.query)
        if self.command == "POST":
            body = self.payload()
            if parts == ["recorder", "start"]:
                return self.reply(self.app.start(body))
            if parts == ["recorder", "stop"]:
                return self.reply(self.app.stop())
            if parts == ["presets"]:
                return self.reply(self.app.presets.save(body.get("name"), body.get("topics")))
        if self.command == "DELETE" and len(parts) == 2:
            if parts[0] == "presets":
                self.app.presets.delete(parts[1])
                return self.reply({"deleted": parts[1]})
            if parts[0] == "bags":
                with self.app.lock:
                    self.app.status()
                    self.app.bags.delete(parts[1])
                return self.reply({"deleted": parts[1]})
        self.reply({"error": "接口不存在"}, 404)

    def dispatch(self):
        self._streaming = False
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception as exc:
            if self._streaming:
                logging.warning("Stream ended: %s", exc)
                return
            code = 500
            if isinstance(exc, FileNotFoundError):
                code = 404
            elif isinstance(exc, (ValueError, UnicodeError)):
                code = 400
            elif isinstance(exc, (RuntimeError, FileExistsError)):
                code = 409
            if code == 500:
                logging.exception("Recorder request failed")
            self.reply({"error": str(exc)}, code)

    do_GET = do_POST = do_DELETE = dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--use-sim-time", action="store_true")
    parser.add_argument("--buffer-mib", type=int, default=64)
    args = parser.parse_args()
    # Bind first, so a duplicate service never creates another ROS executor.
    with ThreadingHTTPServer(("127.0.0.1", 17580), RecorderHandler) as server:
        app = RecorderApp(args.output_dir, args.use_sim_time, args.buffer_mib)
        server.app = app
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: app.shutdown.set())
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            serving.start()
            print(f"Recorder ready on 127.0.0.1:17580, output {app.bags.root}", flush=True)
            app.shutdown.wait()
        finally:
            server.shutdown()
            serving.join()
            with app.lock:
                app.engine.close()


if __name__ == "__main__":
    main()
