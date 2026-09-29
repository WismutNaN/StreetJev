import json
from importlib.resources import files

from streetjev.domain.geography import GeoCatalog, Place


def load_geography() -> tuple[GeoCatalog, dict]:
    data = json.loads(files("streetjev").joinpath("assets/geography.json").read_text(encoding="utf-8"))
    countries, regions = [], []
    for feature in data["features"]:
        p = feature["properties"]
        place = Place(p["code"], p["name"], p["label"], p.get("country"))
        (regions if place.country_code else countries).append(place)
    return GeoCatalog(tuple(countries), tuple(regions)), data
