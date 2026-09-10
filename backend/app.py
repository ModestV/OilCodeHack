from __future__ import annotations

import csv
import io
import json
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics as a
from .config import ROOT, STORAGE
from .formulas import formula_results

IMPORT_LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    STORAGE.mkdir(parents=True, exist_ok=True)
    # An interrupted import must not leave the UI polling forever after a restart.
    for file in STORAGE.glob("*/manifest.json"):
        try:
            m = json.loads(file.read_text())
            if m.get("status") == "importing":
                m.update(
                    status="error",
                    error="Импорт был прерван. Загрузите файлы повторно.",
                )
                write_manifest(file.parent, m)
        except (OSError, ValueError):
            pass
    yield


app = FastAPI(title="Нефтекод · Мониторинг", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)


def write_manifest(directory: Path, value: dict):
    temporary = directory / "manifest.tmp"
    temporary.write_text(json.dumps(a.clean(value), ensure_ascii=False, indent=2))
    temporary.replace(directory / "manifest.json")


def dataset(dataset_id: str, ready=True) -> Path:
    if not dataset_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in dataset_id
    ):
        raise HTTPException(404, "Набор не найден")
    directory = STORAGE / dataset_id
    if not (directory / "manifest.json").exists():
        raise HTTPException(404, "Набор не найден")
    if ready and a.manifest(directory).get("status") != "ready":
        raise HTTPException(409, "Набор ещё не готов к анализу")
    return directory


@app.exception_handler(ValueError)
async def value_error(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/reference/avt/{page}")
def reference_scheme(page: int):
    if page not in (1, 2, 3):
        raise HTTPException(404, "Страница схемы не найдена")
    return FileResponse(ROOT / "docs" / "reference" / f"avt-{page}.png", media_type="image/png")


@app.get("/api/datasets")
def datasets():
    items = []
    for path in STORAGE.glob("*/manifest.json"):
        try:
            items.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    items.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return {
        "datasets": items,
        "default_id": next((m["id"] for m in items if m.get("status") == "ready"), None),
    }


def perform_import(paths: list[Path], directory: Path, name: str):
    try:
        from .ingest import import_dataset

        with IMPORT_LOCK:
            import_dataset(paths, directory, name)
    except Exception as exc:
        previous = a.manifest(directory)
        previous.update(status="error", error=str(exc))
        write_manifest(directory, previous)


@app.post("/api/datasets", status_code=202)
async def upload(
    background: BackgroundTasks,
    files: Annotated[list[UploadFile], File()],
    name: Annotated[str, Form()] = "Загруженные данные",
):
    if not files or len(files) > 8:
        raise HTTPException(422, "Загрузите от 1 до 8 CSV/Excel файлов")
    filenames = [Path(f.filename or "").name for f in files]
    if len(set(filenames)) != len(filenames):
        raise HTTPException(422, "Имена файлов не должны повторяться")
    if any(
        Path(n).suffix.lower() not in (".csv", ".xlsx") or n.startswith("~$") for n in filenames
    ):
        raise HTTPException(422, "Нужны CSV или XLSX; временные файлы и архивы не принимаются")
    identifier = uuid.uuid4().hex[:12]
    directory = STORAGE / identifier
    raw = directory / "raw"
    raw.mkdir(parents=True)
    paths = []
    total = 0
    try:
        for upload_file, filename in zip(files, filenames):
            target = raw / filename
            with target.open("wb") as stream:
                while chunk := await upload_file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 800 * 1024 * 1024:
                        raise HTTPException(413, "Максимальный суммарный размер — 800 МБ")
                    stream.write(chunk)
            paths.append(target)
            await upload_file.close()
    except Exception:
        shutil.rmtree(directory)
        raise
    m = {
        "id": identifier,
        "name": name.strip()[:120] or "Загруженные данные",
        "status": "importing",
        "created_at": datetime.now().isoformat(),
        "sources": [],
        "metrics": [],
        "issues": [],
    }
    write_manifest(directory, m)
    background.add_task(perform_import, paths, directory, m["name"])
    return m


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str):
    return a.manifest(dataset(dataset_id, False))


@app.get("/api/datasets/{dataset_id}/metrics")
def metrics(dataset_id: str):
    return {"metrics": a.metric_catalog(dataset(dataset_id))}


@app.get("/api/datasets/{dataset_id}/snapshot")
def snapshot(dataset_id: str, at: str):
    return a.snapshot(dataset(dataset_id), at)


@app.get("/api/datasets/{dataset_id}/summary")
def summary(
    dataset_id: str,
    start: Annotated[str, Query(alias="from")],
    end: Annotated[str, Query(alias="to")],
    exclude_suspect: bool = False,
):
    return a.summary(dataset(dataset_id), start, end, exclude_suspect)


def selected_metrics(directory: Path, metrics: str) -> list[str]:
    ids = list(dict.fromkeys(filter(None, metrics.split(","))))
    allowed = {m["id"] for m in a.metric_catalog(directory)}
    if not ids or len(ids) > 20 or any(mid not in allowed for mid in ids):
        raise ValueError("Выберите от 1 до 20 показателей из каталога")
    return ids


@app.get("/api/datasets/{dataset_id}/series")
def series(
    dataset_id: str,
    metrics: str,
    start: Annotated[str, Query(alias="from")],
    end: Annotated[str, Query(alias="to")],
    limit: Annotated[int, Query(ge=50, le=4000)] = 600,
    exclude_suspect: bool = False,
):
    directory = dataset(dataset_id)
    return a.series(
        directory,
        selected_metrics(directory, metrics),
        start,
        end,
        limit,
        exclude_suspect,
    )


@app.get("/api/datasets/{dataset_id}/distribution")
def distribution(
    dataset_id: str,
    metric: str,
    start: Annotated[str, Query(alias="from")],
    end: Annotated[str, Query(alias="to")],
    exclude_suspect: bool = False,
):
    directory = dataset(dataset_id)
    selected_metrics(directory, metric)
    return a.distribution(directory, metric, start, end, exclude_suspect)


@app.get("/api/datasets/{dataset_id}/formulas")
def formulas(dataset_id: str, at: str):
    return formula_results(dataset(dataset_id), at)


@app.get("/api/datasets/{dataset_id}/quality")
def quality(dataset_id: str):
    return a.quality(dataset(dataset_id))


@app.get("/api/datasets/{dataset_id}/distillation")
def distillation(dataset_id: str, at: str):
    return a.distillation(dataset(dataset_id), at)


class Freshness(BaseModel):
    kip: float = Field(default=10, gt=0, le=1440)
    pak: float = Field(default=30, gt=0, le=10080)
    lims: float = Field(default=2880, gt=0, le=525600)


class Settings(BaseModel):
    freshness_minutes: Freshness


@app.get("/api/datasets/{dataset_id}/settings")
def settings(dataset_id: str):
    return a.settings(dataset(dataset_id))


@app.put("/api/datasets/{dataset_id}/settings")
def update_settings(dataset_id: str, body: Settings):
    directory = dataset(dataset_id)
    value = body.model_dump()
    temp = directory / "settings.tmp"
    temp.write_text(json.dumps(value))
    temp.replace(directory / "settings.json")
    return value


@app.get("/api/datasets/{dataset_id}/export")
def export(
    dataset_id: str,
    metrics: str,
    start: Annotated[str, Query(alias="from")],
    end: Annotated[str, Query(alias="to")],
    exclude_suspect: bool = False,
):
    directory = dataset(dataset_id)
    ids = selected_metrics(directory, metrics)
    low, high = a.interval(start, end)

    def stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        yield "\ufeff"
        writer.writerow(
            [
                "metric_id",
                "timestamp",
                "value",
                "source",
                "unit",
                "flags",
                "source_file",
                "source_row",
            ]
        )
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        with a.connection(directory) as db:
            cursor = db.execute(
                "SELECT * FROM obs WHERE metric_id IN (SELECT unnest(?)) AND timestamp>=? AND timestamp<?"
                + (f" AND {a.valid_sql(True)}" if exclude_suspect else "")
                + " ORDER BY timestamp,metric_id",
                [ids, low, high],
            )
            while rows := cursor.fetchmany(2000):
                for row in rows:
                    # Prevent formula injection if a user-controlled filename reaches Excel.
                    writer.writerow(
                        [
                            (
                                "'" + v
                                if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                                else v
                            )
                            for v in row
                        ]
                    )
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="oilcode-export.csv"'},
    )


dist = ROOT / "frontend" / "dist"
if dist.exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Метод API не найден")
        return FileResponse(dist / "index.html")
