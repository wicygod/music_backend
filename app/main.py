from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.middleware.security import LightweightSecurityMiddleware
from app.routers import admin, artists, auth, bugreport, feed, history, import_jobs, playlists, search, stream, tracks


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Million Dollars Music Metadata API",
    version="0.1.0",
    description="Local metadata-only catalog backend for the Tauri music desktop app.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LightweightSecurityMiddleware)

app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(bugreport.router)
app.include_router(feed.router)
app.include_router(history.router)
app.include_router(search.router)
app.include_router(stream.router)
app.include_router(tracks.router)
app.include_router(artists.router)
app.include_router(playlists.router)
app.include_router(import_jobs.router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
