from __future__ import annotations

import time
from threading import Lock
from typing import TYPE_CHECKING

import uvicorn
import trio
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.logging_utils import log

if TYPE_CHECKING:
    from src.node import Node


class NodeQueryRequest(BaseModel):
    prompt: str
    query_id: str | None = None
    required_capabilities: dict[str, float] | None = None


class NodeAPIService:
    def __init__(self, node: Node, host: str, port: int) -> None:
        self.node = node
        self.host = host
        self.port = port
        self._progress_lock = Lock()
        self._progress_by_query_id: dict[str, list[dict]] = {}
        self.app = FastAPI(title="TSADAI Node API")
        self.app.add_api_route(
            "/api/query",
            self.query,
            methods=["POST"],
            response_model=None,
        )
        self.app.add_api_route(
            "/api/query/progress/{query_id}",
            self.query_progress,
            methods=["GET"],
            response_model=None,
        )
        self.app.add_api_route(
            "/api/profile",
            self.profile,
            methods=["GET"],
            response_model=None,
        )

    async def run(self) -> None:
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        log("HTTP", f"Node API listening on http://{self.host}:{self.port}")
        log("HTTP", f"Query endpoint: POST http://{self.host}:{self.port}/api/query")
        log(
            "HTTP",
            f"Progress endpoint: GET http://{self.host}:{self.port}/api/query/progress/<query_id>",
        )
        log("HTTP", f"Profile endpoint: GET http://{self.host}:{self.port}/api/profile")
        try:
            await trio.to_thread.run_sync(server.run, abandon_on_cancel=True)
        finally:
            server.should_exit = True

    def _append_progress_event(self, query_id: str, event: dict) -> None:
        event = {
            "ts": time.time(),
            **event,
        }
        with self._progress_lock:
            events = self._progress_by_query_id.setdefault(query_id, [])
            events.append(event)
            del events[:-50]

    def _reset_progress(self, query_id: str) -> None:
        with self._progress_lock:
            self._progress_by_query_id[query_id] = []

    def _progress_events(self, query_id: str) -> list[dict]:
        with self._progress_lock:
            return list(self._progress_by_query_id.get(query_id, []))

    async def query(self, request: NodeQueryRequest):
        if not request.prompt.strip():
            return JSONResponse(
                {"error": "prompt must be a non-empty string"},
                status_code=400,
            )

        try:
            progress_callback = None
            if request.query_id:
                self._reset_progress(request.query_id)
                progress_callback = lambda event: self._append_progress_event(
                    request.query_id,
                    event,
                )

            reply = await run_in_threadpool(
                self.node.answer_query_from_api,
                request.prompt,
                request.query_id,
                request.required_capabilities,
                progress_callback,
            )
        except Exception as exc:
            log("HTTP", f"Query failed: {exc}")
            return JSONResponse(
                {"error": str(exc)},
                status_code=500,
            )

        if request.query_id and isinstance(reply, dict):
            events = self._progress_events(request.query_id)
            reply["progress_events"] = events
            if isinstance(reply.get("routing_trace"), dict):
                reply["routing_trace"]["progress_events"] = events

        return reply

    async def query_progress(self, query_id: str):
        events = self._progress_events(query_id)

        return {
            "query_id": query_id,
            "events": events,
            "latest": events[-1] if events else None,
        }

    async def profile(self):
        try:
            return self.node.benchmark_profile_from_api()
        except Exception as exc:
            log("HTTP", f"Profile failed: {exc}")
            return JSONResponse(
                {"error": str(exc)},
                status_code=500,
            )
