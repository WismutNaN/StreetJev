"""Координатные совпадения и совместимая сетка зон порядка 10 км."""

import math
from dataclasses import dataclass, replace

ROWS = 2000
LATITUDE_STEP = 180 / ROWS


@dataclass(frozen=True)
class LocalPoint:
    id: str
    latitude: float
    longitude: float
    country: str
    region: str | None
    mass: float

    def __post_init__(self):
        if (
            not isinstance(self.id, str)
            or not self.id
            or not self.country
            or not all(math.isfinite(v) for v in (self.latitude, self.longitude, self.mass))
            or not -90 <= self.latitude <= 90
            or not -180 <= self.longitude <= 180
            or self.mass < 0
        ):
            raise ValueError("Некорректное локальное совпадение")
        object.__setattr__(self, "latitude", float(self.latitude))
        object.__setattr__(self, "longitude", float(self.longitude))

    @property
    def code(self):
        return f"{self.country}:{self.region or '-'}:{self.id}:{self.latitude}:{self.longitude}"


def combine_points(points):
    combined = {}
    for point in points:
        previous = combined.get(point.code)
        combined[point.code] = replace(point, mass=point.mass + (previous.mass if previous else 0))
    return tuple(combined.values())


def columns(row):
    latitude = -90 + (row + 0.5) * LATITUDE_STEP
    return max(1, round(4000 * math.cos(math.radians(latitude))))


@dataclass(frozen=True)
class LocalCell:
    country: str
    region: str | None
    row: int
    column: int
    mass: float

    def __post_init__(self):
        if (
            not self.country
            or not isinstance(self.row, int)
            or isinstance(self.row, bool)
            or not isinstance(self.column, int)
            or isinstance(self.column, bool)
            or not 0 <= self.row < ROWS
            or not 0 <= self.column < columns(self.row)
            or not math.isfinite(self.mass)
            or self.mass < 0
        ):
            raise ValueError("Некорректная локальная зона")

    @property
    def code(self):
        return f"{self.country}:{self.region or '-'}:{self.row}:{self.column}"

    @property
    def bounds(self):
        west = -180 + self.column * 360 / columns(self.row)
        south = -90 + self.row * LATITUDE_STEP
        return west, south, west + 360 / columns(self.row), south + LATITUDE_STEP

    @property
    def center(self):
        west, south, east, north = self.bounds
        return (south + north) / 2, (west + east) / 2


def locate_cell(latitude, longitude, country, region, mass):
    if (
        not math.isfinite(latitude)
        or not -90 <= latitude <= 90
        or not math.isfinite(longitude)
        or not -180 <= longitude <= 180
    ):
        raise ValueError("Некорректные координаты зоны")
    row = min(ROWS - 1, math.floor((latitude + 90) / LATITUDE_STEP))
    column = math.floor(((longitude + 180) % 360) / 360 * columns(row))
    return LocalCell(country, region, row, column, mass)


def combine_cells(cells):
    combined = {}
    for cell in cells:
        previous = combined.get(cell.code)
        combined[cell.code] = replace(cell, mass=cell.mass + (previous.mass if previous else 0))
    return tuple(combined.values())


def local_scores(cells, total_weight, countries, regions):
    """Поддержка от координат не превышает поддержку родительского региона."""
    if total_weight <= 0:
        return ()
    totals, country_totals = {}, {}
    for cell in cells:
        key = cell.country, cell.region
        totals[key] = totals.get(key, 0) + cell.mass / total_weight
        country_totals[cell.country] = country_totals.get(cell.country, 0) + cell.mass / total_weight
    result = []
    for cell in cells:
        parent = regions.get(cell.region, 0) if cell.region else countries.get(cell.country, 0)
        total = totals[cell.country, cell.region]
        mass = (
            cell.mass
            / total_weight
            * min(1, parent / total, countries.get(cell.country, 0) / country_totals[cell.country])
            if total
            else 0
        )
        if mass > 0:
            result.append(replace(cell, mass=mass))
    return tuple(result)
