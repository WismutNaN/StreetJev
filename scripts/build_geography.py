"""Воспроизводимая сборка автономной карты Natural Earth (public domain)."""

import json
import urllib.request
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
REVISION = "ca96624a56bd078437bca8184e78163e5039ad19"
BASE = f"https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{REVISION}/geojson/"


def download(name):
    cache = ROOT / ".research" / name
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        print(f"Загрузка {name}", flush=True)
        urllib.request.urlretrieve(BASE + name, cache)
    return json.loads(cache.read_text(encoding="utf-8"))


def build():
    features, codes, adm_to_code = [], set(), {}
    countries = download("ne_50m_admin_0_countries.geojson")
    grouped = {}
    for f in countries["features"]:
        p = f["properties"]
        code = p["ISO_A2_EH"]
        if code in ("-99", ""):
            code = p["ADM0_A3"]
        adm_to_code[p["ADM0_A3"]] = code
        grouped.setdefault(code, []).append(f)
    for code, group in grouped.items():
        # Natural Earth хранит некоторые зависимые острова отдельными features с общим ISO-кодом.
        p = max(group, key=lambda item: item["properties"]["POP_EST"])["properties"]
        geometry = unary_union([shape(f["geometry"]) for f in group])
        codes.add(code)
        features.append(
            {
                "type": "Feature",
                "properties": {"code": code, "name": p["NAME_EN"], "label": p.get("NAME_RU") or p["NAME_EN"]},
                "geometry": mapping(geometry.simplify(0.035, preserve_topology=True)),
            }
        )
    regions = download("ne_10m_admin_1_states_provinces.geojson")
    region_codes = set()
    for f in regions["features"]:
        p = f["properties"]
        country = adm_to_code.get(p["adm0_a3"])
        if not country:
            continue
        code = p.get("iso_3166_2")
        if not code or code in ("-99",) or code in region_codes:
            code = p["adm1_code"]
        if code in region_codes:
            raise ValueError(f"Повторный код региона {code}")
        region_codes.add(code)
        name = p.get("name_en") or p.get("name") or code
        features.append(
            {
                "type": "Feature",
                "properties": {"code": code, "name": name, "label": p.get("name_ru") or name, "country": country},
                "geometry": mapping(shape(f["geometry"]).simplify(0.025, preserve_topology=True)),
            }
        )
    target = ROOT / "src/streetjev/assets/geography.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"type": "FeatureCollection", "source": "Natural Earth", "revision": REVISION, "features": features}

    # Координаты до 4 знаков: для обзорной карты точнее не требуется.
    def rounded(value):
        if isinstance(value, float):
            return round(value, 4)
        if isinstance(value, (list, tuple)):
            return [rounded(x) for x in value]
        if isinstance(value, dict):
            return {k: rounded(v) for k, v in value.items()}
        return value

    target.write_text(json.dumps(rounded(data), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Готово: {len(codes)} стран и территорий, {len(region_codes)} регионов, {target.stat().st_size:,} байт")


if __name__ == "__main__":
    build()
