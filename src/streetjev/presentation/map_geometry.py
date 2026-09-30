"""Проекция и группировка для отображения; оценки домена не изменяются."""

import math
from dataclasses import dataclass

from streetjev.domain.localization import LocalPoint


def project(lon, lat):
    lat = min(84, max(-84, lat))
    return lon, -math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def unproject(x, y):
    return x, math.degrees(2 * math.atan(math.exp(math.radians(-y))) - math.pi / 2)


@dataclass(frozen=True)
class PointCluster:
    points: tuple[LocalPoint, ...]
    x: float
    y: float
    mass: float


def zoom_bucket(scale):
    return math.floor(math.log2(max(scale, 0.01)) * 2)


def cluster_points(points, bucket):
    size = 32 / 2 ** (bucket / 2)
    groups = {}
    for point in points:
        if point.mass <= 0:
            continue
        x, y = project(point.longitude, point.latitude)
        key = point.country, point.region, math.floor((x + 180) / size), math.floor((y + 180) / size)
        groups.setdefault(key, []).append((point, x, y))
    result = []
    for members in groups.values():
        mass = math.fsum(p.mass for p, _, _ in members)
        result.append(
            PointCluster(
                tuple(p for p, _, _ in members),
                sum(p.mass * x for p, x, _ in members) / mass,
                sum(p.mass * y for p, _, y in members) / mass,
                mass,
            )
        )
    return tuple(sorted(result, key=lambda group: group.mass))


def radius_degrees(latitude, radius_km):
    # Масштаб Mercator в малой окрестности. Радиус — ориентир поиска, не ошибка модели.
    return radius_km / (111.195 * max(0.1, math.cos(math.radians(latitude))))
