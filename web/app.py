from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from web.container import build_container
from web.routes_api import router as api_router
from web.routes_pages import router as pages_router


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_dir() -> Path:
    raw = os.getenv("DATA_DIR", "data")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = _resolve_data_dir()
    container = build_container(project_root=PROJECT_ROOT, data_dir=data_dir)
    app.state.container = container
    app.state.templates = Jinja2Templates(directory=str(PROJECT_ROOT / "web" / "templates"))
    try:
        yield
    finally:
        container.scheduler_service.stop()


app = FastAPI(
    title="OpenAI Register Control Panel",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(pages_router)
app.include_router(api_router)
