"""Tool registry: one declarative Tool per business intent.

The model fills only what the user alone knows. The account is decided by the host and is never
a model input. A write is keyed once per user intent, so a retry replays the stored result instead
of writing twice. The key is the host's request_id; a client that sends none (MCP) can opt into a
content window, where the same input within the window counts as the same intent.
Every answer is an envelope with a next action. The same registry exports the catalogue for
MCP tools/list, OpenAI and Anthropic tool formats, and a full contract for operators.
"""
from dataclasses import dataclass, field
import hashlib
import json
import re
import threading
import time
from typing import Any, Callable
import uuid

from .mapper import MapperError

EFFECTS = ("read", "generate", "write")
NEXT_ACTION = {
    "INVALID_INPUT": "입력 값을 고쳐 다시 호출합니다. 원 시스템에는 아무것도 반영되지 않았습니다.",
    "UNKNOWN_TOOL": "도구 목록을 다시 확인합니다.",
    "TOOL_DISABLED": "지금 실행할 수 없는 도구입니다. 사용자에게 준비 중이라고 안내합니다.",
    "NOT_FOUND": "대상이 없거나 이 계정에서 볼 수 없습니다. 있는지 없는지 추측하지 말고 ID를 다시 확인합니다.",
    "CONFLICT": "읽은 뒤에 내용이 바뀌었습니다. 다시 읽고 사용자에게 확인한 뒤 진행합니다.",
    "IDEMPOTENCY_CONFLICT": "같은 request_id로 다른 내용이 들어왔습니다. 같은 요청이면 원래 입력으로, 새 요청이면 새 request_id로 호출합니다.",
    "OPERATION_IN_PROGRESS": "같은 요청이 아직 처리 중입니다. 잠시 뒤 같은 입력으로 다시 호출하면 결과를 받습니다.",
    "OUTCOME_UNKNOWN": "이미 반영됐을 수 있어 자동으로 다시 보내지 않습니다. 원 시스템에서 결과를 확인한 뒤 사용자에게 알립니다.",
    "BACKEND_UNAVAILABLE": "원 시스템에 연결하지 못했습니다. 결과가 없는 것과 실패는 다르므로 '결과가 없다'고 말하지 않습니다.",
}


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    effect: str  # read | generate | write
    run: Callable[[dict, str], Any]  # (validated arguments, account_id) -> data
    questions: dict = field(default_factory=dict)  # {input or "확인": what only the user knows, and when to ask}
    side_effects: str = "없음"  # for operators: exported in the full contract, not shown to the model
    hold_reason: str = ""  # listed to the assistant but not runnable yet


def _pattern(pattern):
    """JSON Schema patterns are ECMA-262: '$' matches only at the very end, never before a trailing newline."""
    return pattern[:-1] + r"\Z" if pattern.endswith("$") and not pattern.endswith("\\$") else pattern


def _violation(schema, value, path="input"):
    """The first way `value` breaks `schema`, or None. Covers what tool inputs use; not a full JSON Schema."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{path}: object expected"
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                return f"{path}.{name}: required"
        for name, item in value.items():
            if name not in properties:
                return f"{path}.{name}: not an input of this tool"
            found = _violation(properties[name], item, f"{path}.{name}")
            if found:
                return found
        return None
    expected = {"string": str, "integer": int, "boolean": bool}.get(kind)
    if expected is None or not isinstance(value, expected) or (kind == "integer" and isinstance(value, bool)):
        return f"{path}: {kind} expected"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: one of {schema['enum']}"
    if kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 10**6):
            return f"{path}: length out of range"
        if "pattern" in schema and not re.search(_pattern(schema["pattern"]), value):
            return f"{path}: format mismatch"
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        return f"{path}: out of range"
    return None


def _description(tool):
    """What the model reads: enough to choose the tool, fill inputs and talk to the user."""
    parts = [f"[실행 불가] {tool.hold_reason}"] if tool.hold_reason else []
    parts.append(tool.description)
    if tool.effect == "write":
        parts.append("저장되는 작업입니다. 사용자가 확인한 뒤에만 호출합니다.")
    if tool.questions:
        parts.append("Question: 대화에 값이 없으면 호출하기 전에 사용자에게 묻습니다.")
        parts += [f"- {key}: {text}" for key, text in tool.questions.items()]
    return "\n".join(parts)


class Tools:
    def __init__(self, *, audit=None, content_window=None, clock=time.monotonic):
        """content_window: seconds during which a write without request_id is replayed for the same input.
        None (the default) refuses such a write."""
        self._tools = {}
        # key -> (fingerprint, envelope, stored_at). stored_at None never expires. Durable storage in production.
        self._replays = {}
        self._pending = {}  # key -> fingerprint of the call that is running now
        self._lock = threading.Lock()
        self.audit = audit or (lambda entry: None)
        self.content_window, self.clock = content_window, clock

    def register(self, tool):
        schema = tool.input_schema
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        if tool.effect not in EFFECTS:
            raise ValueError(f"{tool.name}: effect must be one of {EFFECTS}")
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError(f"{tool.name}: input_schema must be a closed object")
        properties = schema.get("properties", {})
        if {"account_id", "request_id"} & set(properties):
            raise ValueError(f"{tool.name}: account_id and request_id are host values, not model inputs")
        for key in tool.questions:
            if key != "확인" and key not in properties:
                raise ValueError(f"{tool.name}: question target '{key}' is not an input")
        self._tools[tool.name] = tool
        return tool

    def catalog(self, fmt="mcp"):
        tools = list(self._tools.values())
        if fmt == "mcp":
            return {"tools": [{"name": t.name, "title": t.title, "description": _description(t),
                               "inputSchema": t.input_schema,
                               "annotations": {"title": t.title, "readOnlyHint": t.effect == "read",
                                               "destructiveHint": False, "idempotentHint": t.effect == "read",
                                               "openWorldHint": False}} for t in tools]}
        if fmt == "openai":
            return [{"type": "function", "function": {"name": t.name, "description": _description(t),
                                                      "parameters": t.input_schema}} for t in tools]
        if fmt == "anthropic":
            return [{"name": t.name, "description": _description(t), "input_schema": t.input_schema} for t in tools]
        if fmt == "full":
            return [{"name": t.name, "title": t.title, "status": "held" if t.hold_reason else "open",
                     "hold_reason": t.hold_reason or None, "effect": t.effect, "side_effects": t.side_effects,
                     "questions": t.questions, "description": t.description, "input_schema": t.input_schema}
                    for t in tools]
        raise ValueError("fmt must be mcp, openai, anthropic or full")

    def call(self, name, arguments, *, account_id, request_id=None):
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("the host must pass the verified account_id")
        started, trace_id = time.monotonic(), uuid.uuid4().hex
        tool = self._tools.get(name)
        meta = {"tool": name, "effect": tool.effect if tool else None, "trace_id": trace_id}

        def failure(code, message):
            return {"ok": False, "meta": meta, "error": {"code": code, "message": message, "next_action": NEXT_ACTION[code]}}

        def answer(envelope):
            code = "OK" if envelope["ok"] else envelope["error"]["code"]
            # Tool, account, status and time. Never an argument or a result.
            self.audit({"tool": name, "account_id": account_id, "code": code, "trace_id": trace_id,
                        "elapsed_ms": round((time.monotonic() - started) * 1000)})
            return envelope

        if tool is None:
            return answer(failure("UNKNOWN_TOOL", f"'{name}' 도구가 없습니다."))
        if tool.hold_reason:
            return answer(failure("TOOL_DISABLED", tool.hold_reason))
        found = _violation(tool.input_schema, arguments)
        if found:
            return answer(failure("INVALID_INPUT", found))

        key = window = fingerprint = None
        if tool.effect == "write":
            fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if isinstance(request_id, str) and request_id:
                key = (account_id, name, "id", request_id)
            elif self.content_window:
                key, window = (account_id, name, "content", fingerprint), self.content_window
            else:
                return answer(failure("INVALID_INPUT", "쓰기 도구는 사용자 의도 한 번마다 발급한 request_id가 필요합니다."))
            # Checking the stored result and claiming the key are one step, so concurrent retries cannot both run.
            with self._lock:
                stored = self._replays.get(key)
                if stored and stored[2] is not None and window is not None and self.clock() - stored[2] > window:
                    del self._replays[key]  # outside the window the same input is a new intent
                    stored = None
                if stored:
                    # The stored result comes before any version check, so a retry of a success is not a conflict.
                    if stored[0] != fingerprint:
                        return answer(failure("IDEMPOTENCY_CONFLICT", "같은 request_id에 다른 입력입니다."))
                    return answer({**stored[1], "meta": {**stored[1]["meta"], "replayed": True, "trace_id": trace_id}})
                if key in self._pending:
                    if self._pending[key] != fingerprint:
                        return answer(failure("IDEMPOTENCY_CONFLICT", "같은 request_id에 다른 입력입니다."))
                    return answer(failure("OPERATION_IN_PROGRESS", "같은 요청이 처리 중입니다."))
                self._pending[key] = fingerprint

        store, stored_at = False, None
        try:
            envelope = {"ok": True, "data": tool.run(dict(arguments), account_id), "meta": meta}
            store, stored_at = True, self.clock()
        except (ToolError, MapperError) as error:
            # A refusal with nothing applied. Not stored, so the same intent may be sent again.
            envelope = failure(error.code, error.message)
        except Exception:
            if tool.effect != "write":
                envelope = failure("BACKEND_UNAVAILABLE", "조회하지 못했습니다.")
            else:
                # Without proof that nothing was applied, a failed write is unknown, not failed. The same key
                # answers unknown again, and that answer never expires, so the write is not sent a second time.
                envelope, store = failure("OUTCOME_UNKNOWN", "결과를 확인하지 못했습니다."), True
        finally:
            if key is not None:
                with self._lock:
                    self._pending.pop(key, None)
                    if store:
                        self._replays[key] = (fingerprint, envelope, stored_at)
        return answer(envelope)
