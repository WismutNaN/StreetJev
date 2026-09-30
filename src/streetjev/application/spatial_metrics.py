"""Оценка локализации отдельно от совпадения административных кодов."""

import math
import statistics
from dataclasses import asdict

from streetjev.domain.localization import EARTH_RADIUS_KM, distance_km, strongest_point

DISTANCE_SCALE_KM = 25
MISSING_DISTANCE_KM = math.pi * EARTH_RADIUS_KM
RADII_KM = (25, 50, 100, 250, 1000)


def has_coordinates(sample):
    # Доступность источника фиксирована до подбора; нельзя улучшить метрику,
    # выбросив неудобный раунд, в котором выбранная страна осталась без точки.
    return any(
        primary.points or payload and payload.get("candidates") for _, _, primary, payload in sample.observations
    )


def point_error(snapshot, truth):
    country = max(snapshot.countries, key=snapshot.countries.get) if snapshot.known_frames else None
    points = [p for p in snapshot.points if p.mass > 0] if snapshot.known_frames else []
    selected = strongest_point(points, country)
    distances = {p.code: distance_km(truth.latitude, truth.longitude, p.latitude, p.longitude) for p in points}
    top5 = sorted(points, key=lambda p: p.mass, reverse=True)[:5]
    error = distances[selected.code] if selected else None
    return {
        "selected_country": country,
        "selected_point": asdict(selected) if selected else None,
        "distance_km": error,
        "log_distance": math.log1p((error if error is not None else MISSING_DISTANCE_KM) / DISTANCE_SCALE_KM),
        "top5_distance_km": min((distances[p.code] for p in top5), default=None),
        # Только диагностика наличия близкого кандидата: истина не участвует в выборе точки.
        "nearest_candidate_km": min(distances.values(), default=None),
        "candidates": len(points),
    }


def summarize_distances(rows):
    errors = sorted(row["distance_km"] for row in rows if row["distance_km"] is not None)
    nearest = [row["nearest_candidate_km"] for row in rows if row["nearest_candidate_km"] is not None]
    top5 = [row["top5_distance_km"] for row in rows if row["top5_distance_km"] is not None]
    index = (len(errors) - 1) * 0.9
    p90 = (
        (errors[math.floor(index)] * (1 - index % 1) + errors[math.ceil(index)] * (index % 1)) if errors else None
    )
    result = {
        "point_rounds": len(rows),
        "point_predictions": len(errors),
        "point_prediction_coverage": len(errors) / len(rows) if rows else None,
        "point_distance_mean_km": statistics.mean(errors) if errors else None,
        "point_distance_median_km": statistics.median(errors) if errors else None,
        "point_distance_p90_km": p90,
        "point_log_distance": statistics.mean(row["log_distance"] for row in rows) if rows else None,
        "point_top5_median_km": statistics.median(top5) if top5 else None,
        "point_nearest_median_km": statistics.median(nearest) if nearest else None,
    }
    for radius in RADII_KM:
        result[f"point_within_{radius}km"] = sum(d <= radius for d in errors) / len(rows) if rows else None
        result[f"candidate_within_{radius}km"] = sum(d <= radius for d in nearest) / len(rows) if rows else None
    return result
