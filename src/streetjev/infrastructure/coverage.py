"""Небольшая поправка по слою Street View из guess-map, без отдельного сервиса."""

import asyncio
import io
import math
import time

import httpx
from PIL import Image


class CoverageDensity:
    zoom = 12

    def __init__(self, transport=None):
        self.transport = transport
        self.cache = {}

    @classmethod
    def tile(cls, lat, lon):
        count = 2**cls.zoom
        lat = max(-85.05112878, min(85.05112878, lat))
        return (
            int((lon + 180) / 360 * count) % count,
            max(0, min(count - 1, int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * count))),
        )

    @staticmethod
    def density(content):
        with Image.open(io.BytesIO(content)) as tile:
            if tile.size not in ((1, 1), (256, 256)) or "transparency" not in tile.info and "A" not in tile.mode:
                raise ValueError("Неожиданный формат слоя покрытия")
            rgba = tile.convert("RGBA")
            pixels = rgba.tobytes()
            # Синие/голубые линии слоя svv. Белая ошибка или непрозрачная подложка не являются покрытием.
            covered = sum(
                a >= 16 and b > r + 30 and b >= g
                for r, g, b, a in zip(pixels[0::4], pixels[1::4], pixels[2::4], pixels[3::4])
            )
            if covered == 0 and any(alpha >= 16 for alpha in pixels[3::4]):
                raise ValueError("Вместо прозрачного слоя получена неизвестная подложка")
            return covered / (rgba.width * rgba.height)

    async def _sample(self, tiles):
        async with httpx.AsyncClient(timeout=1.5, trust_env=False, transport=self.transport) as client:

            async def fetch(key):
                x, y = key
                density = None
                try:
                    async with asyncio.timeout(2):
                        response = await client.get(
                            f"https://mts{x % 4}.google.com/vt",
                            params={
                                "hl": "en-US",
                                "lyrs": "svv",
                                "style": "40,18",
                                "x": x,
                                "y": y,
                                "z": self.zoom,
                            },
                        )
                        response.raise_for_status()
                        if len(response.content) > 1_000_000:
                            raise ValueError("Слишком большой тайл")
                        density = self.density(response.content)
                except (httpx.HTTPError, OSError, ValueError, TimeoutError):
                    pass
                self.cache[key] = (time.monotonic() + (86400 if density is not None else 60), density)

            await asyncio.gather(*(fetch(key) for key in tiles))

    def sample(self, candidates):
        keys = list(dict.fromkeys(self.tile(c["lat"], c["lon"]) for c in candidates))[:8]
        now = time.monotonic()
        if len(self.cache) > 512:
            self.cache.clear()
        missing = [key for key in keys if self.cache.get(key, (0, None))[0] <= now]
        if missing:
            asyncio.run(self._sample(missing))
        return {key: self.cache[key][1] for key in keys}

    @staticmethod
    def factor(density, penalty=0.15):
        # Это эвристика плотности линий, не доля возможных игровых локаций.
        return 1.0 if density is None else 1 - penalty + penalty * min(1.0, math.sqrt(density / 0.05))
