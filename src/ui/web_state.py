from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
WEB_RUNTIME_DIR = ROOT_DIR / ".runtime" / "web"
BOOTSTRAP_NODES_FILE = WEB_RUNTIME_DIR / "config" / "bootstrap_nodes.txt"
CONVERSATIONS_FILE = WEB_RUNTIME_DIR / "conversations.json"
DEFAULT_CONTEXT_TOKENS = 2000
DEFAULT_CONTEXT_CHARS_PER_TOKEN = 4


def context_chars_per_token() -> int:
    raw = os.environ.get(
        "WEB_CONTEXT_CHARS_PER_TOKEN",
        str(DEFAULT_CONTEXT_CHARS_PER_TOKEN),
    )
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONTEXT_CHARS_PER_TOKEN
    return max(1, value)


def max_context_tokens() -> int:
    raw = os.environ.get("WEB_CONTEXT_TOKENS", str(DEFAULT_CONTEXT_TOKENS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONTEXT_TOKENS
    return max(1, value)


def max_context_chars() -> int:
    return max_context_tokens() * context_chars_per_token()


def make_context_info(
    *,
    included_messages: int,
    available_messages: int,
    current_message_included: bool,
    history_chars: int,
    total_chars: int,
    chat_chars: int,
    max_chars: int,
    truncated: bool,
) -> dict:
    chars_per_token = context_chars_per_token()
    approx_tokens = 0
    if total_chars > 0:
        approx_tokens = max(1, (total_chars + chars_per_token - 1) // chars_per_token)
    chat_approx_tokens = 0
    if chat_chars > 0:
        chat_approx_tokens = max(1, (chat_chars + chars_per_token - 1) // chars_per_token)
    max_tokens = max_context_tokens()
    remaining_tokens = max(0, max_tokens - chat_approx_tokens)
    return {
        "included_messages": included_messages,
        "available_messages": available_messages,
        "current_message_included": current_message_included,
        "history_chars": history_chars,
        "total_chars": total_chars,
        "chat_chars": chat_chars,
        "approx_tokens": approx_tokens,
        "chat_approx_tokens": chat_approx_tokens,
        "max_context_chars": max_chars,
        "max_context_tokens": max_tokens,
        "remaining_context_tokens": remaining_tokens,
        "chars_per_token": chars_per_token,
        "truncated": truncated,
    }


def port_from_api_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    return str(parsed.port) if parsed.port is not None else "?"


def read_available_nodes() -> list[dict]:
    if not BOOTSTRAP_NODES_FILE.exists():
        return []

    api_urls: list[str] = []
    for line in BOOTSTRAP_NODES_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            api_urls.append(stripped)

    nodes = []
    for index, api_url in enumerate(dict.fromkeys(api_urls)):
        port = port_from_api_url(api_url)
        nodes.append(
            {
                "peer_id": "",
                "port": port,
                "address": "",
                "api_url": api_url,
                "label": f"Bootstrap {index} | :{port}",
            }
        )
    return nodes


def load_conversations() -> list[dict]:
    if not CONVERSATIONS_FILE.exists():
        return []
    try:
        data = json.loads(CONVERSATIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    conversations = [item for item in data if isinstance(item, dict)]
    conversations.sort(key=lambda item: float(item.get("updated_at", 0)), reverse=True)
    return conversations


def save_conversations(conversations: list[dict]) -> None:
    CONVERSATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_FILE.write_text(
        json.dumps(conversations, indent=2),
        encoding="utf-8",
    )


def make_title(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= 34:
        return compact or "Untitled chat"
    return compact[:31].rstrip() + "..."


def find_conversation(conversations: list[dict], conversation_id: str) -> dict | None:
    for conversation in conversations:
        if str(conversation.get("id")) == conversation_id:
            return conversation
    return None


def ensure_conversation(
    conversations: list[dict],
    conversation_id: str | None,
    first_prompt: str,
) -> dict:
    if conversation_id:
        existing = find_conversation(conversations, conversation_id)
        if existing is not None:
            return existing

    now = time.time()
    conversation = {
        "id": str(uuid.uuid4()),
        "title": make_title(first_prompt),
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    conversations.insert(0, conversation)
    return conversation


def build_network_context(messages: list[dict], prompt: str) -> tuple[str, dict]:
    limit = max_context_chars()
    prompt_chars = len(prompt)

    if not messages:
        return prompt, make_context_info(
            included_messages=0,
            available_messages=0,
            current_message_included=bool(prompt.strip()),
            history_chars=0,
            total_chars=prompt_chars,
            chat_chars=prompt_chars,
            max_chars=limit,
            truncated=False,
        )

    header = (
        "You are answering one turn in an ongoing UI chat. "
        "Use the recent context only if it is relevant.\n\n"
        "Recent context:\n"
    )
    footer = f"\nCurrent user message:\n{prompt}"
    remaining = limit - len(header) - len(footer)

    selected: list[str] = []
    used = 0
    chat_used = 0
    truncated = False
    for message in reversed(messages):
        role = str(message.get("role", "Assistant"))
        content = str(message.get("content", ""))
        block = f"{role}: {content}\n"
        if used + len(block) > remaining:
            space_left = remaining - used
            prefix = f"{role}: "
            suffix = "\n"
            content_room = space_left - len(prefix) - len(suffix)
            if content_room > 120 and not selected:
                selected.insert(0, f"{prefix}{content[-content_room:]}{suffix}")
                used += space_left
                chat_used += content_room
            truncated = True
            break
        selected.insert(0, block)
        used += len(block)
        chat_used += len(content)

    if not selected:
        return prompt, make_context_info(
            included_messages=0,
            available_messages=len(messages),
            current_message_included=bool(prompt.strip()),
            history_chars=0,
            total_chars=prompt_chars,
            chat_chars=prompt_chars,
            max_chars=limit,
            truncated=True,
        )

    network_prompt = header + "".join(selected) + footer
    return network_prompt, make_context_info(
        included_messages=len(selected),
        available_messages=len(messages),
        current_message_included=bool(prompt.strip()),
        history_chars=used,
        total_chars=len(network_prompt),
        chat_chars=chat_used + prompt_chars,
        max_chars=limit,
        truncated=truncated or len(selected) < len(messages),
    )


def build_network_prompt(messages: list[dict], prompt: str) -> str:
    network_prompt, _ = build_network_context(messages, prompt)
    return network_prompt


def summarize_conversations(conversations: list[dict]) -> list[dict]:
    return [
        {
            "id": conversation.get("id"),
            "title": conversation.get("title") or "Untitled chat",
            "updated_at": conversation.get("updated_at", 0),
        }
        for conversation in conversations
    ]


def query_node_api(
    node_api_url: str,
    prompt: str,
    timeout_s: float = 150.0,
    query_id: str | None = None,
) -> tuple[bool, str, dict | None]:
    if not node_api_url:
        return False, "Selected node does not expose an HTTP API.", None

    url = node_api_url.rstrip("/") + "/api/query"
    payload = {"prompt": prompt}
    if query_id:
        payload["query_id"] = query_id
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False, raw or str(exc), None
    except URLError as exc:
        return False, str(exc.reason), None

    if not isinstance(payload, dict):
        return False, "Node API returned an invalid response.", None

    status = payload.get("status", "ok")
    answer = payload.get("answer")
    routing_trace = payload.get("routing_trace")
    if not isinstance(routing_trace, dict):
        routing_trace = None

    if status != "ok":
        return False, str(answer or status), routing_trace

    return True, str(answer or ""), routing_trace


def query_node_progress(node_api_url: str, query_id: str, timeout_s: float = 2.0) -> dict:
    if not node_api_url or not query_id:
        return {"query_id": query_id, "events": [], "latest": None}

    url = node_api_url.rstrip("/") + f"/api/query/progress/{query_id}"
    request = Request(url, method="GET")

    try:
        with urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return {"query_id": query_id, "events": [], "latest": None}

    if not isinstance(payload, dict):
        return {"query_id": query_id, "events": [], "latest": None}

    return payload
