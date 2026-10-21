"""Loopback-only HTTP API and SSE adapter; no model calls happen on startup."""

from __future__ import annotations

import asyncio
import json
import time
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import Settings, load_settings
from .sessions import SessionManager
from .folder_picker import pick_directory


class NewSession(BaseModel):
    workdir: str = Field(min_length=1, max_length=4096)
    permission_mode: str = "workspace"
    mock: bool = False


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class Approval(BaseModel):
    allowed: bool


class PickDirectory(BaseModel):
    initial_path: str = Field(default="", max_length=4096)


class SessionSettings(BaseModel):
    permission_mode: str | None = None
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    confirm_full: bool = False


def create_app(settings: Settings | None = None, *, manager: SessionManager | None = None,
               port: int = 8765, static_dir: Path | None = None,
               directory_picker=pick_directory) -> FastAPI:
    settings = settings or load_settings()
    manager = manager or SessionManager(settings)
    static_dir = static_dir or Path(__file__).resolve().parents[2] / "web" / "dist"
    picker_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        manager.close()

    app = FastAPI(title="minicode Web", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.manager = manager

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Prevent a third-party website or DNS rebinding from driving local tools.
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if request.url.hostname not in local_hosts:
            return JSONResponse({"detail": "仅允许通过本机地址访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                valid_origin = parsed.scheme == "http" and parsed.hostname in local_hosts and parsed.port in {port, 5173}
            except ValueError:
                valid_origin = False
            if not valid_origin:
                return JSONResponse({"detail": "不允许此网页来源访问本地 Agent"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    def session(identity):
        try:
            return manager.get(identity)
        except KeyError:
            raise HTTPException(404, "会话不存在") from None

    @app.get("/api/config")
    def config():
        return {"workdir": str(settings.workdir), "provider": settings.provider, "model": settings.model,
                "has_api_key": bool(settings.api_key), "permission_mode": "workspace",
                "permission_modes": [{"value": "read-only", "label": "只读"},
                                     {"value": "workspace", "label": "工作区内修改"},
                                     {"value": "full", "label": "完全权限"}]}

    @app.get("/api/sessions")
    def sessions():
        return manager.list()

    @app.post("/api/workspaces/pick")
    def choose_directory(data: PickDirectory):
        if not picker_lock.acquire(blocking=False):
            raise HTTPException(409, "文件夹选择窗口已经打开，请先完成或取消当前选择")
        try:
            selected = directory_picker(data.initial_path or str(settings.workdir))
            if selected is None:
                return {"path": None}
            path = Path(selected).expanduser().resolve()
            if not path.is_dir():
                raise HTTPException(422, "所选文件夹不存在，请重新选择")
            return {"path": str(path)}
        except (RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc
        finally:
            picker_lock.release()

    @app.post("/api/sessions", status_code=201)
    def new_session(data: NewSession):
        try:
            return manager.create(data.workdir, data.permission_mode, mock=data.mock).snapshot()
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/sessions/{identity}")
    def snapshot(identity: str):
        return session(identity).snapshot()

    @app.post("/api/sessions/{identity}/settings")
    def update_settings(identity: str, data: SessionSettings):
        try:
            return session(identity).update_settings(**data.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/sessions/{identity}/messages", status_code=202)
    def submit(identity: str, data: Message):
        if not data.text.strip():
            raise HTTPException(422, "请输入任务内容")
        try:
            session(identity).submit(data.text.strip())
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"accepted": True}

    @app.post("/api/sessions/{identity}/stop")
    def stop(identity: str):
        session(identity).stop()
        return {"requested": True}

    @app.post("/api/sessions/{identity}/approvals/{request_id}")
    def approve(identity: str, request_id: str, data: Approval):
        try:
            session(identity).decide(request_id, data.allowed)
        except KeyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"resolved": True}

    @app.get("/api/sessions/{identity}/events")
    async def events(identity: str, request: Request):
        current = session(identity)

        async def stream():
            initial = current.snapshot()
            cursor = initial["seq"]
            yield f"id: {cursor}\nevent: snapshot\ndata: {json.dumps(initial, ensure_ascii=False)}\n\n"
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                for event in current.events_after(cursor):
                    cursor = event["seq"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                if time.monotonic() - heartbeat > 15:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        target = (static_dir / path).resolve()
        if not target.is_relative_to(static_dir.resolve()):
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(target)
        index = static_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "前端尚未构建。请在 web 目录执行 npm.cmd install 和 npm.cmd run build。"}, status_code=503)

    return app
