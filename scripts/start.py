"""One-command local start without Docker (Windows, macOS, Linux).

    python scripts/start.py --data /path/to/Нефтекод_2.0

Creates .venv, installs pinned dependencies, builds the UI when Node is
available, imports the organisers' data once and serves UI + API on one port.
Re-running skips every step that is already done.
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


def ensure_venv() -> None:
    requirements = ROOT / "requirements.lock"
    if not requirements.exists():
        requirements = ROOT / "requirements.txt"
    marker = VENV / ".oilcode-installed"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if marker.exists() and marker.read_text().strip() == digest and PY.exists():
        return
    step("Окружение Python и зависимости")
    if not PY.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    run([str(PY), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(requirements)])
    marker.write_text(digest)


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
    args = parser.parse_args()

    ensure_python()
    ensure_venv()
    ensure_frontend()
    storage = Path(os.environ.get("OILCODE_STORAGE", ROOT / "storage"))
    ensure_dataset(args.data, args.id, storage)
    env = {**os.environ, "OILCODE_STORAGE": str(storage)}
    if args.llm_url and args.llm_model:
        env.update(OILCODE_LLM_BASE_URL=args.llm_url, OILCODE_LLM_MODEL=args.llm_model)
    serve(args.host, args.port, not args.no_browser, env)


if __name__ == "__main__":
    main()
