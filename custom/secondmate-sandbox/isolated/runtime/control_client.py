"""Child-scoped parent control client. No primary filesystem or credentials."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import urllib.error
import urllib.parse
import urllib.request
import uuid

from errors import Refusal


CONFIG_PATHS = {"config/inherited-runtime.json", "config/crew-dispatch.json", "config/crew-harness",
                "config/backend", "config/backlog-backend", "config/startup-memory-budget"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None  # never forward a child credential to a redirected origin


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class ControlClient:
    def __init__(self, cfg):
        self.cfg = cfg
        parsed = urllib.parse.urlsplit(cfg.parent_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise Refusal("Invalid parent control origin")
        self.origin = cfg.parent_url.rstrip("/")

    def request(self, method, path, payload=None):
        if not path.startswith("/v1/") or ".." in path or "?" in path or "#" in path:
            raise Refusal("Invalid child control API path")
        token = self.cfg.control_token_file.read_text().strip()
        if not token or any(character.isspace() for character in token):
            raise Refusal("Invalid child control credential")
        request = urllib.request.Request(self.origin + path, method=method,
                                         data=canonical(payload) if payload is not None else None,
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise Refusal("Parent control response exceeded size limit")
            return json.loads(body)
        except urllib.error.HTTPError as error:
            raise Refusal("Parent control request rejected (HTTP " + str(error.code) + ")") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise Refusal("Parent control request failed; credential and private details withheld") from None

    @property
    def child_path(self):
        return "/v1/children/" + self.cfg.child_id

    def report(self, text, kind="progress", request_id=None, correlation=None, event_id=None, artifact=None):
        event = {"event_id": event_id or str(uuid.uuid4()), "kind": kind, "text": text}
        if request_id is not None:
            event.update(request_id=request_id, correlation=correlation)
        if artifact is not None:
            event["artifact"] = artifact
        result = self.request("POST", self.child_path + "/reports", event)
        if not isinstance(result, dict) or result.get("event_id") != event["event_id"] or result.get("stored") is not True:
            raise Refusal("Parent report acknowledgement does not match the submitted event")
        return result


def allowed_brain_path(path):
    if not isinstance(path, str) or "\\" in path:
        return False
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(part in (".", "..") for part in parts):
        return False
    if path in CONFIG_PATHS:
        return True
    return (len(parts) >= 4 and parts[:2] == (".agents", "skills")
            and all(part and all(char.isalnum() or char in "-_." for char in part) for part in parts[2:]))


def sync_brain(cfg, client, writer):
    snapshot = client.request("GET", client.child_path + "/brain")
    if snapshot.get("schema") != "brain-snapshot.v1" or not isinstance(snapshot.get("files"), list):
        raise Refusal("Invalid parent brain snapshot")
    files = snapshot["files"]
    if any(not isinstance(item, dict) or set(item) != {"path", "sha256", "content"} for item in files):
        raise Refusal("Invalid brain file entry")
    if files != sorted(files, key=lambda value: value.get("path", "")) or digest(files) != snapshot.get("revision"):
        raise Refusal("Brain snapshot revision/order mismatch")
    record_path = cfg.state / "inherited-brain.json"
    previous = json.loads(record_path.read_text()) if record_path.exists() else {"files": {}}
    planned, seen = [], set()
    for item in files:
        path, content = item.get("path"), item.get("content")
        if not allowed_brain_path(path) or path in seen or not isinstance(content, str):
            raise Refusal("Brain snapshot contains an unauthorized/duplicate path")
        seen.add(path)
        data = content.encode("utf-8")
        if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise Refusal("Brain snapshot file checksum/size mismatch")
        destination = cfg.home / path
        current = destination
        while current != cfg.home:
            if current.is_symlink():
                raise Refusal("Brain destination contains a symlink")
            current = current.parent
        try:
            destination.resolve().relative_to(cfg.home.resolve())
        except ValueError:
            raise Refusal("Brain destination escaped child home") from None
        if destination.exists():
            actual = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual not in (item["sha256"], previous.get("files", {}).get(path)):
                raise Refusal("Brain sync refuses to overwrite locally changed material")
        planned.append((destination, data, item["sha256"]))
    # Validate the whole snapshot before replacing anything; no destination is
    # deleted. A crash can resume because desired/previous hashes are accepted.
    for destination, data, _ in planned:
        if destination.exists() and destination.read_bytes() == data:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".brain-new")
        if temporary.exists() or temporary.is_symlink():
            raise Refusal("Brain temporary path requires operator inspection")
        with open(temporary, "xb") as out:
            out.write(data)
            out.flush()
            import os
            os.fsync(out.fileno())
        temporary.chmod(0o444)
        temporary.replace(destination)
    writer(record_path, {"revision": snapshot["revision"], "source_commit": snapshot.get("source_commit"),
                         "files": {item["path"]: item["sha256"] for item in files}})
    return {"revision": snapshot["revision"], "files": len(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync-brain"])
    parser.parse_args()
    from bridge import Settings, atomic_json
    cfg = Settings.load()
    print(json.dumps(sync_brain(cfg, ControlClient(cfg), atomic_json)))


if __name__ == "__main__":
    try:
        main()
    except (Refusal, OSError, ValueError):
        raise SystemExit("Child brain sync refused; inspect configuration and local state")
