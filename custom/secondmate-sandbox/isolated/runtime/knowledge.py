"""Create immutable child knowledge proposals. Approval/application are parent-only."""
import argparse
import json
from pathlib import Path
import uuid

from control_client import ControlClient, canonical, digest
from errors import Refusal


def propose(cfg, client, source, writer, notifier=None):
    if source.is_symlink():
        raise Refusal("Knowledge proposal input must not be a symlink")
    try:
        source.resolve().relative_to(cfg.home.resolve())
    except ValueError:
        raise Refusal("Knowledge input must remain inside the child home") from None
    if source.stat().st_size > 256 * 1024:
        raise Refusal("Knowledge proposal exceeds size limit")
    value = json.loads(source.read_text(encoding="utf-8"))
    allowed = {"schema", "proposal_id", "version", "manifest", "content", "sha256"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise Refusal("Knowledge proposal contains unsupported authority/target fields")
    value.setdefault("schema", "knowledge-proposal.v1")
    value.setdefault("version", 1)
    value.setdefault("proposal_id", str(uuid.uuid5(uuid.NAMESPACE_URL,
                     cfg.child_id + ":knowledge:" + digest({key: item for key, item in value.items() if key != "sha256"}))))
    if (value["schema"] != "knowledge-proposal.v1" or type(value["version"]) is not int or value["version"] <= 0
            or not isinstance(value.get("manifest"), dict) or not isinstance(value.get("content"), str)):
        raise Refusal("Invalid knowledge proposal")
    try:
        if str(uuid.UUID(value["proposal_id"])) != value["proposal_id"]:
            raise ValueError()
    except (ValueError, TypeError):
        raise Refusal("Knowledge proposal ID must be a canonical UUID") from None
    claimed = value.pop("sha256", None)
    checksum = digest(value)
    if claimed is not None and claimed != checksum:
        raise Refusal("Knowledge proposal checksum mismatch")
    value["sha256"] = checksum
    folder = cfg.home / "data" / "knowledge-proposals" / value["proposal_id"]
    current = folder
    while current != cfg.home:
        if current.is_symlink():
            raise Refusal("Knowledge storage must not contain symlinks")
        current = current.parent
    stored = folder / ("v" + str(value["version"]) + ".json")
    for candidate in (stored, folder / ("v" + str(value["version"]) + ".receipt.json"),
                      folder / ("v" + str(value["version"]) + ".notification.json")):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise Refusal("Unsafe knowledge record path")
    if stored.exists():
        if canonical(json.loads(stored.read_text())) != canonical(value):
            raise Refusal("Immutable knowledge proposal version already exists with different content")
    else:
        writer(stored, value)
    # The immutable local record preserves identity before networking. The same
    # draft generates the same ID; retrying the stored record is also safe.
    receipt = client.request("POST", client.child_path + "/proposals", value)
    if (receipt.get("proposal_id") != value["proposal_id"] or receipt.get("version") != value["version"]
            or receipt.get("sha256") != checksum or receipt.get("approval_required") is not True):
        raise Refusal("Parent proposal receipt does not match the immutable version")
    writer(folder / ("v" + str(value["version"]) + ".receipt.json"), receipt)
    notification_path = folder / ("v" + str(value["version"]) + ".notification.json")
    notification = json.loads(notification_path.read_text()) if notification_path.exists() else None
    if notification and notification.get("status") != "sent":
        raise Refusal("Knowledge notification is uncertain; inspect captain chat before explicit retry")
    if notification is None:
        if notifier is None:
            raise Refusal("Parent accepted proposal; captain notification requires the configured bot")
        claims = value["manifest"].get("claims", [])
        summary = "\n".join("- " + str(claim.get("text", "")) for claim in claims if isinstance(claim, dict))[:2500]
        text = (f"Captain, {cfg.child_id} proposes private knowledge {value['proposal_id']} v{value['version']}.\n"
                f"SHA-256: {checksum}\n{summary}\nAwaiting Mac's explicit approval of this exact version. Nothing was imported into firstmate.")
        writer(notification_path, {"status": "sending", "proposal_sha256": checksum})
        try:
            result = notifier.send(cfg.captain, text)
        except Exception:
            writer(notification_path, {"status": "uncertain", "proposal_sha256": checksum})
            raise Refusal("Knowledge proposal stored; captain notification uncertain and not automatically retried") from None
        writer(notification_path, {"status": "sent", "proposal_sha256": checksum,
                                   "message_id": result.get("message_id") if isinstance(result, dict) else None})
    return {"proposal_id": value["proposal_id"], "version": value["version"], "sha256": checksum,
            "approval_required": True, "local_file": str(stored)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["propose"])
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    from bridge import Settings, Telegram, atomic_json
    cfg = Settings.load()
    notifier = Telegram(cfg)
    notifier.verify()
    print(json.dumps(propose(cfg, ControlClient(cfg), args.file, atomic_json, notifier)))


if __name__ == "__main__":
    try:
        main()
    except (Refusal, OSError, ValueError):
        raise SystemExit("Knowledge proposal refused or unconfirmed; preserve and retry the immutable local file")
