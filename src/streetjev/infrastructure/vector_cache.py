"""Дисковый кэш разобранных линий и подписей, отдельный от HTTP-кэша."""

import gzip
import hashlib
import json
import os
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile


def expires_at(headers, now=None):
    now = time.time() if now is None else now
    directives = str(headers.get("cache-control", "")).lower().split(",")
    if any(value.strip().split("=", 1)[0] in ("no-store", "no-cache") for value in directives):
        return now
    try:
        dated = min(now, parsedate_to_datetime(headers["date"]).timestamp())
    except (KeyError, ValueError, TypeError):
        dated = now
    for value in directives:
        if value.strip().startswith("max-age="):
            try:
                age = max(0, int(headers.get("age", 0)), now - dated)
                return now + max(0, int(value.split("=", 1)[1].strip(' "')) - age)
            except ValueError:
                break
    try:
        return parsedate_to_datetime(headers["expires"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return dated + 7 * 86400


class VectorCache:
    def __init__(self, root):
        self.root = Path(root) / "geometry-v2"
        self.last_prune = 0.0

    def path(self, url):
        return self.root / (hashlib.sha256(url.encode()).hexdigest() + ".json.gz")

    def read(self, url):
        try:
            with gzip.open(self.path(url), "rt", encoding="utf-8") as stream:
                saved = json.load(stream)
            if saved["expires"] > time.time() and set(saved["details"]) == {"roads", "labels"}:
                return saved["details"], saved["expires"]
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            pass
        return None

    def write(self, url, details, expires):
        if expires <= time.time():
            return
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                data = json.dumps(
                    {"expires": expires, "details": details}, ensure_ascii=False, separators=(",", ":")
                )
                stream.write(gzip.compress(data.encode(), compresslevel=1))
            os.replace(temporary, self.path(url))
            if time.monotonic() - self.last_prune > 60:
                self.last_prune = time.monotonic()
                self.prune()
        except OSError:
            pass  # Ошибка необязательного кэша не скрывает загруженную карту.
        finally:
            try:
                if temporary:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def prune(self):
        entries = sorted((p.stat().st_mtime, p.stat().st_size, p) for p in self.root.glob("*.json.gz"))
        total = sum(size for _, size, _ in entries)
        root = self.root.resolve()
        for _, size, path in entries:
            if total <= 256 * 1024 * 1024:
                break
            if path.resolve().parent == root:
                path.unlink(missing_ok=True)
                total -= size
