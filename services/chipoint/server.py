"""Локальный HTTP-сервис: один запрос Chipoint, без очереди и без доступа к сети при inference."""

import argparse
import base64
import io
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

log = logging.getLogger(__name__)


def make_handler(backend):
    gate = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, value):
            data = json.dumps(value, allow_nan=False).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            if self.path != "/health":
                return self.reply(404, {"error": "unknown endpoint"})
            self.reply(
                200, {"model": backend.version, "ready": True, "busy": gate.locked(), "mode": "street_retrieval"}
            )

        def do_POST(self):
            if self.path != "/v1/geolocate":
                return self.reply(404, {"error": "unknown endpoint"})
            if not gate.acquire(blocking=False):
                return self.reply(503, {"error": "Chipoint занят; очередь отключена"})
            try:
                self.connection.settimeout(30)
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 16 * 1024 * 1024:
                    return self.reply(413, {"error": "Неверный размер изображения"})
                payload = json.loads(self.rfile.read(size))
                encoded = base64.b64decode(payload["image"], validate=True)
                with Image.open(io.BytesIO(encoded)) as image:
                    if image.width * image.height > 20_000_000:
                        return self.reply(413, {"error": "Слишком большое изображение"})
                    result = backend.infer(image)
                self.reply(200, result)
            except (ValueError, KeyError, OSError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception as exc:
                log.exception("Ошибка inference Chipoint")
                self.reply(500, {"error": str(exc)})
            finally:
                gate.release()

    return Handler


if __name__ == "__main__":
    os.environ["HF_HUB_OFFLINE"] = "1"
    from backend import StreetRetrieval

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--towers", nargs="+", choices=["so400m256", "gopt"], default=["so400m256"])
    args = parser.parse_args()
    engine = StreetRetrieval(args.towers, args.device)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine))
    print(f"Chipoint готов: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
