"""Установка и запуск imajev: все изменяемые файлы остаются внутри StreetJev/.runtime."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "imajev"
SOURCE = RUNTIME / "source"
VENV = RUNTIME / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REVISION = "7a0e6a11eab8f38e6c005c7137d7ae47f7339613"
MANIFEST = Path(__file__).with_name("manifest.json")


def environment():
    env = os.environ.copy()
    env.update(
        {
            "HF_HOME": str(RUNTIME / "huggingface"),
            "UV_CACHE_DIR": str(ROOT / ".runtime" / "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": str(ROOT / ".runtime" / "python"),
            "TORCH_HOME": str(RUNTIME / "torch"),
            "PYTHONPATH": os.pathsep.join([str(SOURCE / "src"), str(SOURCE / "scripts")]),
            "PYTHONUTF8": "1",
            # Обычный HTTP надёжнее при сетях, обрывающих большие Xet range-запросы.
            "HF_HUB_DISABLE_XET": "1",
            "HF_HUB_DOWNLOAD_TIMEOUT": "60",
        }
    )
    return env


def run(*args, cwd=ROOT):
    subprocess.run([str(arg) for arg in args], cwd=cwd, env=environment(), check=True)


def install_source():
    marker = SOURCE / ".streetjev-revision"
    if marker.exists():
        if marker.read_text().strip() != REVISION:
            raise RuntimeError(
                "Версия сервиса отличается от manifest. Используйте отдельный каталог для обновления."
            )
        return
    RUNTIME.mkdir(parents=True, exist_ok=True)
    archive = RUNTIME / "source.zip"
    print("Загрузка исходников imajev…", flush=True)
    urllib.request.urlretrieve(f"https://codeload.github.com/mohit67890/imajev/zip/{REVISION}", archive)
    # Извлекаем только файлы закреплённого коммита внутри .runtime/imajev/source.
    with zipfile.ZipFile(archive) as bundle:
        prefix = f"imajev-{REVISION}/"
        for member in bundle.infolist():
            if not member.filename.startswith(prefix) or member.is_dir():
                continue
            relative = Path(member.filename[len(prefix) :])
            target = (SOURCE / relative).resolve()
            if not target.is_relative_to(SOURCE.resolve()):
                raise RuntimeError("Небезопасный путь в архиве сервиса")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(member))
    marker.write_text(REVISION, encoding="utf-8")
    archive.unlink()


def setup(skip_weights=False):
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Не найден uv. Установите uv и повторите команду.")
    install_source()
    if not PYTHON.exists():
        run(uv, "venv", "--python", "3.12", VENV)
    run(uv, "pip", "install", "--python", PYTHON, "torch", "torchvision", "--torch-backend=cu128")
    run(uv, "pip", "install", "--python", PYTHON, "-e", str(SOURCE) + "[serve,torch]")
    run(
        PYTHON,
        "-c",
        "import torch; print('CUDA:', torch.cuda.is_available()); "
        "print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'GPU не обнаружен')",
    )
    if not skip_weights:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        run(PYTHON, "scripts/download_model.py", "--model", "4b", cwd=SOURCE)
        code = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download('mohit67890/imajev-4b', revision={manifest['adapter_revision']!r}, "
            f"local_dir={str(SOURCE / 'adapters/imajev-4b')!r}, ignore_patterns=['mlx/*'])"
        )
        run(PYTHON, "-c", code, cwd=SOURCE)
    installed = subprocess.check_output(
        [uv, "pip", "freeze", "--python", str(PYTHON)], env=environment(), text=True
    )
    (RUNTIME / "installed.txt").write_text(installed, encoding="utf-8")
    print("Установка завершена. Запуск: uv run python services/imajev/manage.py start", flush=True)


def server_command(port=18765):
    return [
        str(PYTHON),
        "scripts/playground/server.py",
        "--backend",
        "torch",
        "--model-bundle",
        "artifacts/model-qwen4b.json",
        "--adapter",
        "adapters/imajev-4b",
        "--calibration",
        "adapters/imajev-4b/calibration.json",
        "--model-name",
        "imajev-4b",
        "--rotations",
        "1",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def start(port=18765):
    if not PYTHON.exists() or not (SOURCE / "artifacts/model-qwen4b.json").exists():
        raise RuntimeError("Сначала выполните: uv run python services/imajev/manage.py setup")
    # Проверяем до загрузки GPU: занятый порт не должен стоить десятков секунд и VRAM.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"Порт {port} занят. Укажите другой --port и тот же адрес в приложении.") from exc
    return subprocess.call(server_command(port), cwd=SOURCE, env=environment())


def status(port=18765):
    print(f"Исходники: {SOURCE}")
    print(f"Окружение установлено: {PYTHON.exists()}")
    print(f"Базовая модель загружена: {(SOURCE / 'artifacts/model-qwen4b.json').exists()}")
    print(f"Адаптер загружен: {(SOURCE / 'adapters/imajev-4b/adapter_config.json').exists()}")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as response:
            print(response.read().decode())
    except OSError:
        print("Сервер пока не отвечает на /v1/models.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "start", "status"))
    parser.add_argument("--skip-weights", action="store_true", help="установить зависимости без весов")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535")
    try:
        if args.command == "setup":
            setup(args.skip_weights)
        elif args.command == "start":
            return start(args.port)
        else:
            status(args.port)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Ошибка сервиса: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
