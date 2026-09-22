"""One-command local start without Docker (Windows, macOS, Linux).

    python scripts/start.py --data /path/to/Нефтекод_2.0

Creates .venv, installs pinned dependencies, builds the UI when Node is
available, imports the organisers' data once and serves UI + API on one port.
Re-running skips every step that is already done.

Closed network (no internet on the target machine)::

    python scripts/start.py --prepare-offline offline   # on a machine with internet, same OS/Python
    python scripts/start.py --wheelhouse offline/wheels --data /path/to/data   # on the target

The first command downloads the pinned wheels and builds ``frontend/dist``;
copy the whole repository folder (with ``offline/`` and ``frontend/dist``).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
WINDOWS = os.name == "nt"
PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
TABLES = {".csv", ".xlsx"}
ARCHIVES = {".rar", ".zip", ".7z"}


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(command: list[str], **kwargs) -> None:
    print("   $", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def ensure_python() -> None:
    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        sys.exit(f"Нужен Python 3.11–3.13, запущен {sys.version.split()[0]}.")


def requirements_file() -> Path:
    requirements = ROOT / "requirements.lock"
    return requirements if requirements.exists() else ROOT / "requirements.txt"


def ensure_venv(wheelhouse: Path | None = None) -> None:
    requirements = requirements_file()
    marker = VENV / ".oilcode-installed"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if marker.exists() and marker.read_text().strip() == digest and PY.exists():
        return
    step("Окружение Python и зависимости" + (" (без интернета)" if wheelhouse else ""))
    if not PY.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    offline = ["--no-index", "--find-links", str(wheelhouse)] if wheelhouse else []
    run([str(PY), "-m", "pip", "install", "--disable-pip-version-check", "-q", *offline, "-r", str(requirements)])
    marker.write_text(digest)


def prepare_offline(target: Path) -> None:
    """Everything the closed-network start needs besides the data: pinned wheels and the built UI."""

    step(f"Офлайн-комплект в {target}")
    wheels = target / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "-q",
         "-r", str(requirements_file()), "-d", str(wheels)])
    if not ensure_frontend():
        sys.exit("Для офлайн-комплекта нужен собранный интерфейс: установите Node.js 22.12+ и повторите.")
    print(f"\nГотово. Перенесите папку репозитория целиком (с {target} и frontend/dist) и запустите:\n"
          f"  python scripts/start.py --wheelhouse {wheels} --data /путь/к/данным")


def ensure_frontend() -> bool:
    dist = ROOT / "frontend" / "dist" / "index.html"
    if dist.exists():
        return True
    npm = shutil.which("npm")
    if not npm:
        print("\n[!] Node.js/npm не найден и собранного интерфейса нет: будет доступен только API (/docs).\n"
              "    Установите Node.js 22.12+ или используйте Docker: docker compose up --build")
        return False
    step("Сборка интерфейса (один раз)")
    run([npm, "--prefix", str(ROOT / "frontend"), "ci", "--no-audit", "--no-fund"])
    run([npm, "--prefix", str(ROOT / "frontend"), "run", "build"])
    return dist.exists()


def unpack(data: Path) -> Path:
    """Return a folder with CSV/XLSX; unpack data.rar/zip with the system tar (bsdtar) if needed."""

    archives = [p for p in data.rglob("*") if p.suffix.lower() in ARCHIVES]
    if not archives:
        return data
    target = Path(tempfile.mkdtemp(prefix="oilcode-data-"))
    for table in data.rglob("*"):
        if table.suffix.lower() in TABLES and not table.name.startswith("~$"):
            shutil.copy2(table, target / table.name)
    tar = shutil.which("bsdtar") or shutil.which("tar")
    for archive in archives:
        step(f"Распаковка {archive.name}")
        try:
            run([tar, "-xf", str(archive), "-C", str(target)])
        except (subprocess.CalledProcessError, TypeError):
            sys.exit(f"Не удалось распаковать {archive}. Распакуйте архив вручную и передайте папку в --data.")
    return target


def ensure_dataset(data: Path | None, dataset_id: str, storage: Path) -> None:
    if (storage / dataset_id / "manifest.json").exists():
        return
    if data is None:
        print("\n[i] Данные не импортированы. Передайте --data /путь/к/комплекту или загрузите файлы в интерфейсе.")
        return
    if not data.is_dir():
        sys.exit(f"Папка с данными не найдена: {data}")
    step(f"Импорт данных в набор '{dataset_id}' (один раз, несколько минут)")
    run([str(PY), str(ROOT / "scripts" / "import_demo.py"), str(unpack(data)), "--id", dataset_id],
        env={**os.environ, "OILCODE_STORAGE": str(storage)})


def serve(host: str, port: int, open_browser: bool, env: dict[str, str]) -> None:
    url = f"http://{'127.0.0.1' if host in {'0.0.0.0', '::'} else host}:{port}"
    step(f"Запуск: {url}  (API: {url}/docs, остановка — Ctrl+C)")
    process = subprocess.Popen([str(PY), "-m", "uvicorn", "backend.app:app", "--host", host, "--port", str(port)],
                               cwd=ROOT, env=env)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"{url}/api/health", timeout=1)
                break
            except OSError:
                if process.poll() is not None:
                    sys.exit(process.returncode)
                time.sleep(0.5)
        if open_browser:
            webbrowser.open(url)
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Запуск Нефтекод одной командой")
    parser.add_argument("--data", type=Path, help="папка комплекта организаторов (CSV/XLSX или data.rar)")
    parser.add_argument("--id", default="hackathon", help="ID набора данных")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--llm-url", help="OpenAI-совместимый endpoint, напр. http://localhost:11434/v1")
    parser.add_argument("--llm-model", help="модель, напр. qwen2.5:3b-instruct или gpt-oss:20b")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--wheelhouse", type=Path, help="папка с колёсами из --prepare-offline: установка без интернета")
    parser.add_argument("--prepare-offline", type=Path, metavar="DIR",
                        help="скачать зависимости и собрать интерфейс для запуска в закрытой сети, затем выйти")
    args = parser.parse_args()

    ensure_python()
    if args.prepare_offline:
        prepare_offline(args.prepare_offline)
        return
    if args.wheelhouse and not args.wheelhouse.is_dir():
        sys.exit(f"Папка с колёсами не найдена: {args.wheelhouse}")
    ensure_venv(args.wheelhouse)
    ensure_frontend()
    storage = Path(os.environ.get("OILCODE_STORAGE", ROOT / "storage"))
    ensure_dataset(args.data, args.id, storage)
    env = {**os.environ, "OILCODE_STORAGE": str(storage)}
    if args.llm_url and args.llm_model:
        env.update(OILCODE_LLM_BASE_URL=args.llm_url, OILCODE_LLM_MODEL=args.llm_model)
    serve(args.host, args.port, not args.no_browser, env)


if __name__ == "__main__":
    main()
