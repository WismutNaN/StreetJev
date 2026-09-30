"""Локальные факты из guess-map и геометрии Natural Earth."""

import json
from importlib.resources import files

from shapely.geometry import shape

from streetjev.domain.clues import PlaceClues


def load_clue_facts(geography):
    data = json.loads(files("streetjev").joinpath("assets/driving_side.json").read_text(encoding="utf-8"))
    sides = {code: side for side in ("left", "right") for code in data[side]}
    facts = {}
    for feature in geography["features"]:
        properties = feature["properties"]
        country = properties.get("country") or properties["code"]
        bounds = shape(feature["geometry"]).bounds
        # Пересекающие экватор территории остаются совместимыми с обоими ответами.
        hemisphere = "north" if bounds[1] > 0 else "south" if bounds[3] < 0 else None
        facts[properties["code"]] = PlaceClues(sides.get(country), hemisphere)
    return facts
