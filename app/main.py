"""FastAPI entrypoint. This process serves the web UI and also runs the
inbox watcher as a background thread (see app/watcher.py) - but it does
NOT run the Huey consumer that actually executes jobs. That's a second,
separate OS process (see entrypoint.sh), because the conversion work it
does is long-running and blocking; keeping it out of this process is what
lets the web UI stay responsive while a book is converting. The two
processes coordinate purely through the shared SQLite database in
CONFIG_DIR (see app/db.py for why that needs WAL mode) - there's no
in-memory or IPC channel between them.
"""
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
