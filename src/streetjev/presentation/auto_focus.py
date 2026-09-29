"""Выбор охвата карты по группе ведущих гипотез; без зависимости от Qt."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Bounds:
    left: float
    top: float
    right: float
    bottom: float


def focus_bounds(scores: dict[str, float], bounds: dict[str, Bounds]) -> Bounds | None:
    ranked = sorted(
        ((code, score) for code, score in scores.items() if code in bounds), key=lambda x: x[1], reverse=True
    )
    if not ranked or ranked[0][1] <= 0:
        return None
    # Не отбрасываем далёкого соперника с сопоставимой оценкой ради удобного кадра.
    threshold = ranked[0][1] * 0.3
    leaders = [(code, score) for code, score in ranked if score >= threshold]
    if len(leaders) > 12 or sum(score for _, score in leaders) < 0.55 * sum(score for _, score in ranked):
        return None
    selected = [bounds[code] for code, _ in leaders]
    left, right = min(b.left for b in selected), max(b.right for b in selected)
    top, bottom = min(b.top for b in selected), max(b.bottom for b in selected)
    if right - left > 110 or bottom - top > 95:
        return None
    # Не прыгаем к маленькому полигону: минимум несколько стран / крупный регион.
    width, height = max(30, (right - left) * 1.25), max(22, (bottom - top) * 1.25)
    cx, cy = (right + left) / 2, (bottom + top) / 2
    return Bounds(cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)
