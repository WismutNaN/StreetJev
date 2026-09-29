import base64
import importlib.util
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
from PIL import Image


def test_service_busy_has_no_queue_and_health_stays_available():
    path = Path(__file__).resolve().parents[1] / "services/chipoint/server.py"
    spec = importlib.util.spec_from_file_location("chipoint_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    running, release = threading.Event(), threading.Event()

    class Backend:
        version = "test"

        def infer(self, image):
            running.set()
            assert release.wait(5)
            return {"candidates": []}

    server = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(Backend()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="JPEG")
    payload = {"image": base64.b64encode(buffer.getvalue()).decode()}
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(httpx.post, url + "/v1/geolocate", json=payload, trust_env=False)
            assert running.wait(3)
            assert httpx.get(url + "/health", trust_env=False).json()["busy"]
            assert httpx.post(url + "/v1/geolocate", json=payload, trust_env=False).status_code == 503
            release.set()
            assert first.result(timeout=3).status_code == 200
        assert not httpx.get(url + "/health", trust_env=False).json()["busy"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
