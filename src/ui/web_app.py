from __future__ import annotations

import sys
import time
import uuid
from threading import Lock

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.env_config import env_float, env_int, load_project_env, require_env
from src.ui.web_state import (
    STATIC_DIR,
    build_network_context,
    ensure_conversation,
    find_conversation,
    load_conversations,
    query_node_api,
    query_node_progress,
    read_available_nodes,
    save_conversations,
    summarize_conversations,
)


app = FastAPI(title="DIAL Chat")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

lock = Lock()
QUERY_TIMEOUT_S = 150.0
QUERY_STATUSES: dict[str, dict] = {}


class ChatRequest(BaseModel):
    entry_node: str
    prompt: str
    conversation_id: str | None = None


class QueryRequest(BaseModel):
    entry_node: str
    prompt: str


class ContextPreviewRequest(BaseModel):
    prompt: str = ""
    conversation_id: str | None = None
    messages: list[dict] | None = None


def _set_query_status(query_id: str, **updates) -> None:
    with lock:
        status = QUERY_STATUSES.setdefault(
            query_id,
            {
                "query_id": query_id,
                "started_at": time.time(),
                "done": False,
                "ok": None,
                "message": None,
            },
        )
        status.update(updates)


def _finish_chat_query(
    query_id: str,
    conversation_id: str,
    entry_node: str,
    network_prompt: str,
) -> None:
    _set_query_status(
        query_id,
        phase="waiting_for_network",
        message=None,
    )

    try:
        ok, answer, routing_trace = query_node_api(
            entry_node,
            network_prompt,
            QUERY_TIMEOUT_S,
            query_id=query_id,
        )
    except TimeoutError:
        ok, answer, routing_trace = (
            False,
            f"The query timed out after {QUERY_TIMEOUT_S:.0f} seconds.",
            None,
        )
    except Exception as exc:
        ok, answer, routing_trace = False, str(exc), None

    with lock:
        conversations = load_conversations()
        conversation = find_conversation(conversations, conversation_id)

        if conversation is not None and ok:
            conversation.setdefault("messages", []).append(
                {
                    "role": "Assistant",
                    "content": answer,
                    "routing_trace": routing_trace,
                    "created_at": time.time(),
                }
            )
            conversation["updated_at"] = time.time()
            conversations.sort(
                key=lambda item: float(item.get("updated_at", 0)),
                reverse=True,
            )
            save_conversations(conversations)

        response = {
            "ok": ok,
            "answer": answer,
            "routing_trace": routing_trace,
            "error": None if ok else answer,
            "conversation_id": conversation_id,
            "conversation": conversation,
            "conversations": summarize_conversations(load_conversations()),
        }

        status = QUERY_STATUSES.setdefault(query_id, {"query_id": query_id})
        status.update(
            {
                "done": True,
                "ok": ok,
                "phase": "done" if ok else "error",
                "message": "Network response received." if ok else str(answer),
                "response": response,
                "finished_at": time.time(),
            }
        )


def _prepare_chat_query(request: ChatRequest) -> dict | JSONResponse:
    entry_node = request.entry_node.strip()
    prompt = request.prompt.strip()

    if not entry_node or not prompt:
        return JSONResponse(
            {"error": "entry_node and prompt are required"},
            status_code=400,
        )

    with lock:
        conversations = load_conversations()
        conversation = ensure_conversation(
            conversations,
            request.conversation_id,
            prompt,
        )
        messages = conversation.setdefault("messages", [])
        network_prompt, context_info = build_network_context(messages, prompt)
        messages.append(
            {"role": "User", "content": prompt, "created_at": time.time()}
        )
        conversation["updated_at"] = time.time()
        save_conversations(conversations)
        conversation_id = str(conversation["id"])

    query_id = str(uuid.uuid4())
    _set_query_status(
        query_id,
        conversation_id=conversation_id,
        entry_node=entry_node,
        phase="queued",
        message=None,
    )

    return {
        "ok": True,
        "query_id": query_id,
        "conversation_id": conversation_id,
        "entry_node": entry_node,
        "network_prompt": network_prompt,
        "context_info": context_info,
        "conversation": conversation,
        "conversations": summarize_conversations(load_conversations()),
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def state() -> dict:
    with lock:
        return {
            "nodes": read_available_nodes(),
            "conversations": summarize_conversations(load_conversations()),
        }


@app.get("/api/conversations/{conversation_id}", response_model=None)
async def get_conversation(conversation_id: str):
    with lock:
        conversation = find_conversation(load_conversations(), conversation_id)

    if conversation is None:
        return JSONResponse(
            {"error": "conversation not found"},
            status_code=404,
        )

    return conversation


@app.post("/api/context/preview", response_model=None)
async def context_preview(request: ContextPreviewRequest) -> dict:
    prompt = request.prompt.strip()
    messages = request.messages
    if messages is None:
        with lock:
            conversation = (
                find_conversation(load_conversations(), request.conversation_id)
                if request.conversation_id
                else None
            )
            messages = conversation.get("messages", []) if conversation else []
    _, context_info = build_network_context(messages, prompt)

    return {"ok": True, "context_info": context_info}


@app.post("/api/conversations")
async def create_conversation() -> dict:
    with lock:
        conversations = load_conversations()
        conversation = ensure_conversation(conversations, None, "New chat")
        save_conversations(conversations)
        return conversation


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict:
    with lock:
        conversations = load_conversations()
        next_conversations = [
            item
            for item in conversations
            if str(item.get("id")) != conversation_id
        ]
        save_conversations(next_conversations)

    return {
        "ok": True,
        "conversations": summarize_conversations(next_conversations),
    }


@app.post("/api/query")
async def query(request: QueryRequest) -> dict:
    ok, answer, routing_trace = await run_in_threadpool(
        query_node_api,
        request.entry_node.strip(),
        request.prompt.strip(),
        QUERY_TIMEOUT_S,
    )
    return {"ok": ok, "answer": answer, "routing_trace": routing_trace}


@app.post("/api/chat/start", response_model=None)
async def start_chat(request: ChatRequest, background_tasks: BackgroundTasks):
    prepared = _prepare_chat_query(request)
    if isinstance(prepared, JSONResponse):
        return prepared

    background_tasks.add_task(
        _finish_chat_query,
        prepared["query_id"],
        prepared["conversation_id"],
        prepared["entry_node"],
        prepared["network_prompt"],
    )

    return {
        "ok": True,
        "query_id": prepared["query_id"],
        "conversation_id": prepared["conversation_id"],
        "context_info": prepared["context_info"],
        "conversation": prepared["conversation"],
        "conversations": prepared["conversations"],
    }


@app.get("/api/chat/status/{query_id}", response_model=None)
async def chat_status(query_id: str):
    with lock:
        status = dict(QUERY_STATUSES.get(query_id, {}))

    if not status:
        return JSONResponse({"error": "query not found"}, status_code=404)

    elapsed_s = time.time() - float(status.get("started_at", time.time()))
    done = bool(status.get("done"))
    status["elapsed_s"] = elapsed_s
    if not done and status.get("entry_node"):
        progress = await run_in_threadpool(
            query_node_progress,
            status["entry_node"],
            query_id,
        )
        events = progress.get("events") if isinstance(progress, dict) else []
        latest = progress.get("latest") if isinstance(progress, dict) else None
        if events:
            status["progress_events"] = events
        if isinstance(latest, dict) and latest.get("message"):
            status["message"] = latest["message"]
            status["phase"] = latest.get("event", status.get("phase"))

    if done and not status.get("message"):
        status["message"] = "Network response received."
    return status


@app.post("/api/chat", response_model=None)
async def chat(request: ChatRequest):
    start_response = _prepare_chat_query(request)
    if isinstance(start_response, JSONResponse):
        return start_response

    query_id = start_response["query_id"]
    await run_in_threadpool(
        _finish_chat_query,
        query_id,
        start_response["conversation_id"],
        start_response["entry_node"],
        start_response["network_prompt"],
    )

    with lock:
        response = QUERY_STATUSES[query_id]["response"]

    if not response.get("ok"):
        return JSONResponse(response, status_code=400)
    return response


def main() -> None:
    global QUERY_TIMEOUT_S

    try:
        load_project_env()
        QUERY_TIMEOUT_S = env_float("CLIENT_QUERY_TIMEOUT")
        host = require_env("WEB_HOST")
        port = env_int("WEB_PORT")
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
