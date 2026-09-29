"""Локальные установка и запуск Chipoint Street Retrieval. Все файлы — в .runtime/chipoint."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "chipoint"
PYTHON = RUNTIME / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SERVICE = Path(__file__).resolve().parent


def environment():
    env = os.environ.copy()
    env.update(
        {
            "HF_HOME": str(RUNTIME / "huggingface"),
            "TORCH_HOME": str(RUNTIME / "torch"),
            "UV_CACHE_DIR": str(ROOT / ".runtime" / "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": str(ROOT / ".runtime" / "python"),
            "PYTHONUTF8": "1",
            "HF_HUB_DISABLE_XET": "1",
            "HF_HUB_DOWNLOAD_TIMEOUT": "120",
        }
    )
    return env


def run(*args):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, env=environment(), check=True)


def setup(towers, skip_weights=False):
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Не найден uv")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not PYTHON.exists():
        run(uv, "venv", "--python", "3.12", PYTHON.parent.parent)
    run(uv, "pip", "install", "--python", PYTHON, "torch==2.11.0", "torchvision==0.26.0", "--torch-backend=cu128")
    run(
        uv,
        "pip",
        "install",
        "--python",
        PYTHON,
        "open_clip_torch>=3.2,<4",
        "numpy>=2,<3",
        "pillow>=11,<13",
        "huggingface_hub>=1,<2",
    )
    if not skip_weights:
        # Одна галерея ~5 ГБ, энкодер ~1.6 ГБ. Не загружаем photo arm или отключённые towers.
        required = (7 * len(towers) + 2) * 1024**3
        if shutil.disk_usage(RUNTIME).free < required:
            raise RuntimeError(f"Для загрузки нужен запас {required / 1024**3:.0f} ГБ на диске репозитория")
        run(PYTHON, SERVICE / "backend.py", "--download", "--towers", *towers)
    frozen = subprocess.check_output([uv, "pip", "freeze", "--python", str(PYTHON)], env=environment(), text=True)
    (RUNTIME / "installed.txt").write_text(frozen, encoding="utf-8")
    print("Chipoint установлен. Запуск: uv run python services/chipoint/manage.py start", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "start", "status"])
    parser.add_argument("--towers", nargs="+", choices=["so400m256", "gopt"], default=["so400m256"])
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--skip-weights", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535")
    try:
        if args.command == "setup":
            setup(args.towers, args.skip_weights)
        elif args.command == "start":
            if not PYTHON.exists():
                raise RuntimeError("Сначала выполните: uv run python services/chipoint/manage.py setup")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", args.port))
            run(
                PYTHON,
                SERVICE / "server.py",
                "--port",
                args.port,
                "--device",
                args.device,
                "--towers",
                *args.towers,
            )
        else:
            print(f"Окружение: {PYTHON.exists()}; файлы: {RUNTIME}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=2) as response:
                    print(json.dumps(json.load(response), ensure_ascii=False))
            except OSError:
                print("Сервис не запущен")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Ошибка Chipoint: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
