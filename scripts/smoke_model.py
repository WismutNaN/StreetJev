"""Интеграционная проверка GPU на синтетическом изображении; не benchmark точности."""

import argparse
import json
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from streetjev.application.session import AnalysisResult, Session
from streetjev.infrastructure.capture import prepare_image
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.clues import load_clue_facts
from streetjev.infrastructure.imajev import ImajevExpert
from streetjev.infrastructure.storage import SqliteRoundRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--output", type=Path, default=Path("artifacts/model"))
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=3, trust_env=False) as client:
        deadline = time.monotonic() + 180
        while True:
            try:
                response = client.get(args.url.rstrip("/") + "/v1/models")
                response.raise_for_status()
                print("Сервис готов:", response.json(), flush=True)
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Сервис не запустился за 180 секунд") from None
                time.sleep(2)
    image = Image.new("RGB", (640, 420), "#90b5d7")
    draw = ImageDraw.Draw(image)
    draw.polygon([(0, 260), (190, 100), (280, 240), (440, 80), (640, 260)], fill="#789283")
    draw.rectangle((0, 260, 640, 420), fill="#688c45")
    draw.polygon([(300, 260), (340, 260), (570, 420), (75, 420)], fill="#64666a")
    draw.line((320, 280, 320, 420), fill="white", width=4)
    draw.rectangle((380, 235, 388, 355), fill="#cccccc")
    draw.rectangle((340, 170, 620, 245), fill="#164eaa", outline="white", width=3)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 22)
    draw.text((355, 180), "A12  INNSBRUCK", font=font, fill="white")
    draw.text((355, 210), "SALZBURG  150 km", font=font, fill="white")
    image.save(output / "synthetic-road.png")
    catalog, geography = load_geography()
    expert = ImajevExpert(catalog, args.url, timeout=180, beam=3, facts=load_clue_facts(geography))
    frame = prepare_image(image, "Синтетический тест интеграции")
    evidence = expert.infer(frame)
    assert evidence.model_version == "imajev-4b"
    assert len(evidence.countries.probabilities) == len(catalog.countries)
    assert abs(sum(evidence.countries.probabilities.values()) - 1) < 1e-8
    assert set(evidence.clues) == {"hemisphere", "driving_side"}
    repo = SqliteRoundRepository(output / "smoke.sqlite3")
    try:
        session = Session(catalog, repo)
        assert session.accept(AnalysisResult(session.round.id, frame, evidence, 1))
        assert session.round.frames == 1
        top = sorted(evidence.countries.probabilities.items(), key=lambda pair: pair[1], reverse=True)[:5]
        report = {
            "test": "synthetic_integration_not_accuracy",
            "model": evidence.model_version,
            "elapsed_ms": evidence.elapsed_ms,
            "countries": len(evidence.countries.probabilities),
            "region_branches": list(evidence.regions),
            "unknown": evidence.countries.unknown,
            "top": top,
            "clues": {
                key: {"probabilities": dict(value.probabilities), "unknown": value.unknown}
                for key, value in evidence.clues.items()
            },
        }
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "raw-response.json").write_text(evidence.raw_json, encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        repo.close()


if __name__ == "__main__":
    main()
