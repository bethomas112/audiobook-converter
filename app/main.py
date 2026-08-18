from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.watcher import start_watcher
from app.web.routes import router

_watcher_state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    observer, stop_event = start_watcher()
    _watcher_state["observer"] = observer
    _watcher_state["stop_event"] = stop_event
    yield
    stop_event.set()
    observer.stop()
    observer.join(timeout=5)


app = FastAPI(title="Audiobook Converter", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
app.include_router(router)
