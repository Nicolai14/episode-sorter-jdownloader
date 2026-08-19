"""Episode Sorter: FastAPI app, static dashboard and background scheduler."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .api.routes import router
from .core.scheduler import scheduler
from .db import engine, ensure_schema, session_scope
from .core import library
from .models import Base

logging.basicConfig(
    level=os.environ.get("ES_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("episode-sorter")

WEB_DIR = Path(os.environ.get("ES_WEB_DIR", "/app/web"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    added = ensure_schema(Base)
    if added:
        logger.info("schema updated: %s", ", ".join(added))
    config.bootstrap()
    try:
        with session_scope() as session:
            stats = library.reindex(session)
        logger.info("library indexed: %s", stats)
    except Exception as exc:  # noqa: BLE001 - a missing mount must not stop the app
        logger.warning("library index failed: %s", exc)
    scheduler.start()
    logger.info("episode sorter started, dry run = %s", config.get("dry_run"))
    yield
    scheduler.stop()


app = FastAPI(title="Episode Sorter", version="1.0.0", lifespan=lifespan)
app.include_router(router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "dry_run": bool(config.get("dry_run", True))})


if WEB_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")
