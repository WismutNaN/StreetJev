"""Ленивый кэш geoBoundaries ADM2; исходные координаты отделены от экранного упрощения."""

import gzip
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import Request, urlopen

from shapely import STRtree, from_wkb, make_valid
from shapely.errors import GEOSException
from shapely.geometry import Point, box, shape

from streetjev.domain.subregions import Subregion

log = logging.getLogger(__name__)
API = "https://www.geoboundaries.org/api/current/gbOpen/{iso}/ADM2/"
MAX_DOWNLOAD = 64 * 1024 * 1024


def cooperative_json(content):
    # У больших GeoJSON C-декодер иначе удерживает GIL до конца всего файла.
    # Python callbacks дают потоку UI время между координатами.
    return json.loads(content, parse_float=lambda value: float(value), parse_int=lambda value: int(value))


def write_json(stream, data):
    # iterencode работает небольшими порциями, не удерживая GIL на сериализации всей страны.
    chunks, size = [], 0
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(data):
        chunks.append(chunk)
        size += len(chunk)
        if size >= 65536:
            stream.write("".join(chunks))
            chunks, size = [], 0
    stream.write("".join(chunks))


def country_iso3():
    return json.loads(files("streetjev").joinpath("assets/country_iso3.json").read_text(encoding="utf-8"))


def download_json(url):
    with urlopen(
        Request(url, headers={"User-Agent": "StreetJev/0.1 (desktop geographic hypotheses)"}), timeout=20
    ) as r:
        content = r.read(MAX_DOWNLOAD + 1)
    if len(content) > MAX_DOWNLOAD:
        raise ValueError("Слишком большой набор границ")
    return cooperative_json(content)


def rings(geometry):
    parts = [geometry] if geometry.geom_type == "Polygon" else getattr(geometry, "geoms", ())
    return tuple(
        tuple(tuple(r.coords) for r in [p.exterior, *p.interiors])
        for p in parts
        if p.geom_type == "Polygon" and not p.is_empty
    )


class SubregionDataset:
    def __init__(self, country, metadata, data, geography):
        self.country, self.metadata = country, metadata
        parents = [
            (f["properties"]["code"], make_valid(shape(f["geometry"])))
            for f in geography["features"]
            if f["properties"].get("country") == country
        ]
        parent_tree = STRtree([p[1] for p in parents])
        self.places, self.geometries, self.drawings, self.anchors = [], [], {}, {}
        seen = set()
        for feature in data["features"]:
            props = feature["properties"]
            if props.get("shapeType") != "ADM2":
                raise ValueError("Источник не содержит ADM2")
            if props.get("shapeGroup", metadata["boundaryISO"]) != metadata["boundaryISO"]:
                raise ValueError("ADM2 относится к другой стране")
            geometry = make_valid(shape(feature["geometry"]))
            if geometry.is_empty or geometry.area <= 0:
                continue
            candidates = [
                (float(geometry.intersection(parents[int(i)][1]).area), parents[int(i)][0])
                for i in parent_tree.query(geometry, predicate="intersects")
            ]
            area, parent = max(candidates, default=(0, None))
            if area < geometry.area * 0.5:
                parent = None
            code = f"gb:{country}:ADM2:{props['shapeID']}"
            if code in seen:
                raise ValueError("Повторный идентификатор ADM2")
            seen.add(code)
            self.places.append(Subregion(code, str(props.get("shapeName") or code), country, parent))
            self.geometries.append(geometry)
            anchor = geometry.representative_point()
            self.anchors[code] = (anchor.x, anchor.y)
            self.drawings[code] = tuple(
                rings(geometry.simplify(tolerance, preserve_topology=True)) for tolerance in (0.02, 0.004, 0.0005)
            )
        self.finish()

    def finish(self):
        self.tree = STRtree(self.geometries)
        self.by_code = {p.code: p for p in self.places}
        self.locations = OrderedDict()

    def locate(self, latitude, longitude):
        key = latitude, longitude
        if key in self.locations:
            self.locations.move_to_end(key)
            return self.locations[key]
        found = self.tree.query(Point(longitude, latitude), predicate="intersects")
        # На общей границе детерминированно учитываем точку один раз.
        result = min((self.places[int(i)].code for i in found), default=None)
        self.locations[key] = result
        if len(self.locations) > 16384:
            self.locations.popitem(last=False)
        return result

    def visible(self, bounds):
        return [int(i) for i in self.tree.query(box(*bounds), predicate="intersects")]


class SubregionRepository:
    def __init__(self, root, geography, fetch=download_json):
        self.root, self.geography, self.fetch = Path(root), geography, fetch
        self.iso3 = country_iso3()

    def load(self, country):
        iso = self.iso3.get(country)
        if not iso:
            raise ValueError(f"Для {country} нет кода geoBoundaries")
        path = self.root / f"{iso}-ADM2.json.gz"
        prepared = self.read_prepared(country, path)
        if prepared and prepared[1] > time.time() - 30 * 86400:
            return prepared[0]
        saved = None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                saved = cooperative_json(stream.read())
            if saved.get("version") != 1 or saved["metadata"]["boundaryISO"] != iso:
                saved = None
            elif saved["saved_at"] > time.time() - 30 * 86400:
                dataset = SubregionDataset(country, saved["metadata"], saved["data"], self.geography)
                self.write_prepared(dataset, path, saved["saved_at"])
                return dataset
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            saved = None
        try:
            metadata = self.fetch(API.format(iso=iso))
            if metadata.get("boundaryISO") != iso or metadata.get("boundaryType") != "ADM2":
                raise ValueError(f"ADM2 для {country} недоступен")
            data = self.fetch(metadata["simplifiedGeometryGeoJSON"])
            dataset = SubregionDataset(country, metadata, data, self.geography)
        except Exception:
            if prepared:
                log.info("Используется подготовленный кэш ADM2 %s", country)
                return prepared[0]
            if saved:
                log.info("Используется сохранённая версия ADM2 %s", country)
                return SubregionDataset(country, saved["metadata"], saved["data"], self.geography)
            raise
        temporary = None
        saved_at = time.time()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                with gzip.open(stream, "wt", encoding="utf-8", compresslevel=1) as compressed:
                    write_json(
                        compressed, {"version": 1, "saved_at": saved_at, "metadata": metadata, "data": data}
                    )
            os.replace(temporary, path)
        except OSError:
            log.warning("Не удалось записать кэш ADM2 %s", country)
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        self.write_prepared(dataset, path, saved_at)
        return dataset

    def signature(self, country, path):
        parents = [f for f in self.geography["features"] if f["properties"].get("country") == country]
        digest = hashlib.sha256(json.dumps(parents, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        stat = path.stat()
        return [1, country, digest, stat.st_mtime_ns, stat.st_size]

    def read_prepared(self, country, path):
        try:
            signature = self.signature(country, path)
            with gzip.open(path.with_suffix(".prepared.gz"), "rt", encoding="utf-8") as stream:
                header = json.loads(stream.readline())
                if header["signature"] != signature:
                    return None
                dataset = SubregionDataset.__new__(SubregionDataset)
                dataset.country, dataset.metadata = country, header["metadata"]
                dataset.places, dataset.geometries, dataset.drawings, dataset.anchors = [], [], {}, {}
                for line in stream:
                    code, name, region, wkb, drawings, anchor = cooperative_json(line)
                    dataset.places.append(Subregion(code, name, country, region))
                    dataset.geometries.append(from_wkb(wkb))
                    dataset.drawings[code], dataset.anchors[code] = drawings, anchor
                if len(dataset.places) != header["count"]:
                    return None
                dataset.finish()
                return dataset, header["saved_at"]
        except (OSError, ValueError, KeyError, TypeError, EOFError, GEOSException):
            return None

    def write_prepared(self, dataset, path, saved_at):
        temporary = None
        try:
            signature = self.signature(dataset.country, path)
            with NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                with gzip.open(file, "wt", encoding="utf-8", compresslevel=1) as stream:
                    stream.write(
                        json.dumps(
                            {
                                "signature": signature,
                                "metadata": dataset.metadata,
                                "saved_at": saved_at,
                                "count": len(dataset.places),
                            }
                        )
                        + "\n"
                    )
                    for place, geometry in zip(dataset.places, dataset.geometries, strict=True):
                        stream.write(
                            json.dumps(
                                [
                                    place.code,
                                    place.name,
                                    place.region,
                                    geometry.wkb_hex,
                                    dataset.drawings[place.code],
                                    dataset.anchors[place.code],
                                ],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
            os.replace(temporary, path.with_suffix(".prepared.gz"))
        except OSError:
            log.info("Не удалось записать подготовленный кэш ADM2 %s", dataset.country)
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
