"""Реальный локальный Chipoint на синтетическом изображении: транспорт и GPU, не точность."""

import json
from pathlib import Path

from PIL import Image

from streetjev.application.session import AnalysisResult, Session
from streetjev.infrastructure.capture import prepare_image
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.demo import DemoExpert
from streetjev.infrastructure.storage import SqliteRoundRepository


def main():
    output = Path("artifacts/chipoint")
    output.mkdir(parents=True, exist_ok=True)
    catalog, geography = load_geography()
    expert = ChipointExpert(catalog, geography, timeout=180)
    image_path = Path("artifacts/model/synthetic-road.png")
    image = Image.open(image_path) if image_path.exists() else Image.new("RGB", (640, 420), "#8ab9db")
    frame = prepare_image(image, "Синтетический тест Chipoint")
    report = []
    repo = SqliteRoundRepository(output / "smoke.sqlite3")
    session = Session(catalog, repo)
    session.accept(AnalysisResult(session.round.id, frame, DemoExpert(catalog).infer(frame), 1))
    try:
        for index in range(2):
            result = expert.infer(frame)
            raw = json.loads(result.raw_json)
            assert len(raw["candidates"]) == 64
            assert abs(sum(result.countries.probabilities.values()) - 1) < 1e-8
            (output / f"raw-{index}.json").write_text(result.raw_json, encoding="utf-8")
            if index == 0:
                assert session.supplement(AnalysisResult(session.round.id, frame, result, 1))
                assert session.round.frames == 1
            report.append(
                {
                    "elapsed_ms": result.elapsed_ms,
                    "model": result.model_version,
                    "mapped_mass": 1 - result.countries.unknown,
                    "candidates": len(raw["candidates"]),
                    "top": sorted(result.countries.probabilities.items(), key=lambda p: p[1], reverse=True)[:5],
                }
            )
            print(json.dumps(report[-1], ensure_ascii=False), flush=True)
        (output / "report.json").write_text(
            json.dumps(
                {"test": "synthetic_integration_not_accuracy", "runs": report}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
    finally:
        repo.close()


if __name__ == "__main__":
    main()
