"""Bind delivery evidence to the real Codex session, never the newest log file."""
import json
import os
from pathlib import Path
import stat
import time
import uuid

from errors import Refusal


MAX_READ = 16 * 1024 * 1024


def session_event(event, kind):
    payload = event.get("payload", {})
    return event.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == kind


def user_pointer(event, pointer):
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return False
    if session_event(event, "user_message"):
        return payload.get("message") == pointer
    if event.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
        return False
    content = payload.get("content")
    return (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "input_text" and content[0].get("text") == pointer)


class SessionTracker:
    def __init__(self, cfg, writer):
        self.cfg, self.write = cfg, writer
        self.binding_path = cfg.state / "session.json"
        self.ready_path = cfg.state / "ready.json"
        self.activity_path = cfg.state / "session-activity.json"
        self.root = cfg.codex_home / "sessions"

    def safe_file(self, path):
        path = Path(path)
        if path.is_symlink():
            raise Refusal("Codex session log must not be a symlink")
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            raise Refusal("Codex session log escaped this child's Codex home") from None
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise Refusal("Codex session log is not a regular file")
        return resolved, info

    def records(self, binding, offset=0):
        path, info = self.safe_file(binding["path"])
        if [info.st_dev, info.st_ino] != binding["file_identity"] or info.st_size < offset:
            raise Refusal("Bound Codex session log was replaced or truncated")
        with open(path, "rb") as source:
            source.seek(offset)
            data = source.read(MAX_READ + 1)
        if len(data) > MAX_READ:
            raise Refusal("Codex session evidence exceeds bounded read size")
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        records = []
        for line in data[:end + 1].splitlines():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                raise Refusal("Malformed complete record in bound Codex session") from None
            if isinstance(event, dict):
                records.append(event)
        return records, offset + end + 1

    def bind(self, runtime):
        thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
        if not thread:
            raise Refusal("Real agent session ID is required for readiness")
        try:
            thread = str(uuid.UUID(thread))
        except ValueError:
            raise Refusal("Invalid agent session ID") from None
        if (os.environ.get("CODEX_THREAD_ID") and os.environ.get("CODEX_SESSION_ID")
                and os.environ["CODEX_THREAD_ID"] != os.environ["CODEX_SESSION_ID"]):
            raise Refusal("Agent session environment disagrees")
        candidates = []
        # A worker may have the newest log. Use the agent-provided exact UUID,
        # then verify session_meta and cwd in the selected file.
        for path in self.root.glob(f"**/*{thread}*.jsonl"):
            resolved, info = self.safe_file(path)
            with open(resolved, "rb") as source:
                first = source.readline(1024 * 1024)
            try:
                meta = json.loads(first)
            except (ValueError, UnicodeError):
                continue
            payload = meta.get("payload", {})
            ids = [payload.get(key) for key in ("id", "session_id") if payload.get(key)]
            if (meta.get("type") == "session_meta" and ids and all(value == thread for value in ids)
                    and Path(payload.get("cwd", "")).resolve() == self.cfg.home.resolve()):
                candidates.append({"path": str(resolved), "session_id": thread,
                                   "file_identity": [info.st_dev, info.st_ino], "nonce": runtime["nonce"]})
        if len(candidates) != 1:
            raise Refusal("Could not uniquely bind the actual agent's Codex session")
        binding = candidates[0]
        self.write(self.binding_path, binding)
        return binding

    def binding(self, runtime):
        try:
            binding = json.loads(self.binding_path.read_text())
        except (FileNotFoundError, ValueError):
            raise Refusal("No verified Codex session binding") from None
        if binding.get("nonce") != runtime["nonce"]:
            raise Refusal("Session binding belongs to a stale runtime")
        self.safe_file(binding["path"])
        return binding

    def request_ready(self, runtime, pane):
        binding = self.bind(runtime)
        active, offset = self.activity(binding)
        if not active:
            raise Refusal("Readiness requires the active agent turn's task_started evidence")
        ready = {"nonce": runtime["nonce"], "pane": pane, "time": time.time(),
                 "phase": "awaiting_turn_complete", "turn_id": active,
                 "session_id": binding["session_id"], "offset": offset}
        self.write(self.ready_path, ready)
        return ready

    def activity(self, binding):
        try:
            cursor = json.loads(self.activity_path.read_text())
        except (FileNotFoundError, ValueError):
            cursor = {}
        same = cursor.get("session_id") == binding["session_id"] and cursor.get("file_identity") == binding["file_identity"]
        active = cursor.get("active_turn") if same else None
        events, offset = self.records(binding, cursor.get("offset", 0) if same else 0)
        for event in events:
            if session_event(event, "task_started"):
                active = event["payload"].get("turn_id")
            elif session_event(event, "task_complete") and event["payload"].get("turn_id") == active:
                active = None
        self.write(self.activity_path, {"session_id": binding["session_id"], "file_identity": binding["file_identity"],
                                       "offset": offset, "active_turn": active})
        return active, offset

    def check_ready(self, runtime, pane):
        try:
            ready = json.loads(self.ready_path.read_text())
        except (FileNotFoundError, ValueError):
            raise Refusal("Waiting for verified agent readiness") from None
        if ready.get("nonce") != runtime["nonce"] or ready.get("pane") != pane:
            raise Refusal("Readiness belongs to a stale runtime")
        binding = self.binding(runtime)
        if ready.get("session_id") != binding["session_id"]:
            raise Refusal("Readiness belongs to a different Codex session")
        if ready.get("phase") == "awaiting_turn_complete":
            events, _ = self.records(binding, ready["offset"])
            if not any(session_event(event, "task_complete") and event["payload"].get("turn_id") == ready["turn_id"]
                       for event in events):
                raise Refusal("Waiting for the verified agent turn to complete")
            ready["phase"] = "ready"
            self.write(self.ready_path, ready)
        if ready.get("phase") != "ready":
            raise Refusal("Unrecognized readiness phase")
        if self.activity(binding)[0] is not None:
            raise Refusal("Agent has an active turn; readiness is not currently idle")
        return runtime, pane

    def prepare(self, runtime, pane, pointer):
        binding = self.binding(runtime)
        path, info = self.safe_file(binding["path"])
        if [info.st_dev, info.st_ino] != binding["file_identity"]:
            raise Refusal("Session log identity changed before delivery")
        with open(path, "rb") as source:
            base = max(0, info.st_size - 1024 * 1024)
            source.seek(base)
            tail = source.read()
        end = tail.rfind(b"\n")
        if end < 0:
            raise Refusal("No bounded complete session record before delivery")
        offset = base + end + 1
        return {"runtime": runtime, "pane": pane, "binding": binding, "offset": offset,
                "pointer": pointer, "created_at": time.time(), "attempt_id": str(uuid.uuid4())}

    def acknowledged(self, runtime, attempt):
        binding = self.binding(runtime)
        if binding != attempt["binding"] or runtime != attempt["runtime"]:
            raise Refusal("Delivery acknowledgment belongs to another runtime/session")
        events, _ = self.records(binding, attempt["offset"])
        return any(user_pointer(event, attempt["pointer"]) for event in events)
