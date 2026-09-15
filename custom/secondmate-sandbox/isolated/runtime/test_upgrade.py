import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from bridge import Queue, Refusal, Settings, Target, Telegram, atomic_json, main
from control_client import canonical, digest, sync_brain
from knowledge import propose
from session_delivery import SessionTracker, user_pointer
from test_bridge import message


def event(kind, turn):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn}}


def user_event(pointer, role="user"):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text", "text": pointer}],
            "internal_chat_message_metadata_passthrough": {"turn_id": str(uuid.uuid4())}}}


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "child"
        self.home.mkdir()
        self.cfg = Settings(self.home, 42, frozenset([-100]), codex_home=Path(self.temp.name) / ".codex")
        self.queue = Queue(self.cfg)
        self.runtime = {"nonce": "runtime-1", "pid": 321, "start": "100", "home": str(self.home), "target": self.cfg.target}
        self.session_id = str(uuid.uuid4())
        self.turn = str(uuid.uuid4())
        self.log = self.cfg.codex_home / "sessions" / ("rollout-2026-09-15-" + self.session_id + ".jsonl")
        self.log.parent.mkdir(parents=True)
        self.append({"type": "session_meta", "payload": {"id": self.session_id, "session_id": self.session_id, "cwd": str(self.home)}})
        self.append(event("task_started", self.turn))
        self.tracker = SessionTracker(self.cfg, atomic_json)

    def tearDown(self):
        self.queue.db.close()
        # Brain snapshots deliberately use read-only files.
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.temp.cleanup()

    def append(self, value, newline=True):
        with open(self.log, "ab") as out:
            out.write(json.dumps(value).encode() + (b"\n" if newline else b""))

    def ready_request(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.session_id, "CODEX_SESSION_ID": self.session_id}):
            return self.tracker.request_ready(self.runtime, "%1")

    def ready(self):
        self.ready_request()
        self.append(event("task_complete", self.turn))
        self.tracker.check_ready(self.runtime, "%1")

    def api(self):
        api = Mock()
        api.verify.return_value = 99
        api.call.return_value = {"status": "member"}
        return api

    def admin(self, uid, command, sender=42, chat=-200, **kwargs):
        value = message(sender=sender, chat=chat, kind="supergroup", **kwargs)
        value["text"] = command
        self.queue.ingest({"update_id": uid, "message": value}, self.api())

    def request(self):
        return {"schema": "parent-request.v1", "request_id": str(uuid.uuid4()), "correlation": "a" * 16,
                "child_id": self.cfg.child_id, "body": "Inspect the authorized project scope.",
                "authority": {"role": "parent", "approval": False}, "scope": {"project": "example"}}

    def fake_target(self, ack=True):
        target = Mock()
        target.prepare_delivery.return_value = {"attempt_id": "attempt", "runtime": self.runtime, "pane": "%1"}
        target.await_ack.return_value = ack
        return target

    def test_ready_waits_for_matching_task_complete_not_other_turn(self):
        self.ready_request()
        with self.assertRaisesRegex(Refusal, "turn to complete"):
            self.tracker.check_ready(self.runtime, "%1")
        self.append(event("task_complete", str(uuid.uuid4())))
        with self.assertRaises(Refusal):
            self.tracker.check_ready(self.runtime, "%1")
        self.append(event("task_complete", self.turn))
        self.assertEqual(self.tracker.check_ready(self.runtime, "%1"), (self.runtime, "%1"))

    def test_binding_uses_exact_environment_session_not_newer_worker(self):
        worker = self.log.parent / ("rollout-9999-" + str(uuid.uuid4()) + ".jsonl")
        worker.write_text(json.dumps({"type": "session_meta", "payload": {"id": str(uuid.uuid4()), "cwd": str(self.home / "worker")}}) + "\n")
        self.ready_request()
        self.assertEqual(json.loads(self.tracker.binding_path.read_text())["path"], str(self.log.resolve()))

    def test_interrupted_turn_never_unlocks_readiness_without_completion(self):
        self.ready_request()
        self.append(event("turn_aborted", self.turn))
        self.append(event("task_complete", str(uuid.uuid4())))
        with self.assertRaises(Refusal):
            self.tracker.check_ready(self.runtime, "%1")

    def test_new_direct_active_turn_invalidates_previously_idle_readiness(self):
        self.ready()
        self.append(event("task_started", str(uuid.uuid4())))
        with self.assertRaisesRegex(Refusal, "active turn"):
            self.tracker.check_ready(self.runtime, "%1")

    def test_session_environment_disagreement_or_wrong_cwd_refused(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.session_id, "CODEX_SESSION_ID": str(uuid.uuid4())}):
            with self.assertRaises(Refusal):
                self.tracker.bind(self.runtime)
        self.log.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.session_id, "cwd": str(self.home / "another")}}) + "\n")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.session_id, "CODEX_SESSION_ID": self.session_id}):
            with self.assertRaises(Refusal):
                self.tracker.bind(self.runtime)

    def test_ack_requires_new_exact_user_record_in_bound_session(self):
        self.ready()
        pointer = "# SECONDMATE_INBOX 100 /fixed/inbox/100.json"
        self.append(user_event(pointer))
        attempt = self.tracker.prepare(self.runtime, "%1", pointer)
        self.assertFalse(self.tracker.acknowledged(self.runtime, attempt))
        self.append(user_event(pointer, role="assistant"))
        self.append(user_event(pointer + " extra"))
        self.assertFalse(self.tracker.acknowledged(self.runtime, attempt))
        self.append(user_event(pointer), newline=False)
        self.assertFalse(self.tracker.acknowledged(self.runtime, attempt))
        with open(self.log, "ab") as out:
            out.write(b"\n")
        self.assertTrue(self.tracker.acknowledged(self.runtime, attempt))

    def test_session_truncation_and_stale_runtime_never_ack(self):
        self.ready()
        attempt = self.tracker.prepare(self.runtime, "%1", "pointer")
        self.log.write_text("")
        with self.assertRaises(Refusal):
            self.tracker.acknowledged(self.runtime, attempt)
        with self.assertRaises(Refusal):
            self.tracker.binding({**self.runtime, "nonce": "other"})

    def test_old_event_msg_user_shape_supported_but_quotes_are_not(self):
        self.assertTrue(user_pointer({"type": "event_msg", "payload": {"type": "user_message", "message": "pointer"}}, "pointer"))
        self.assertFalse(user_pointer({"type": "event_msg", "payload": {"type": "agent_message", "message": "pointer"}}, "pointer"))

    def test_no_ack_marks_attention_and_never_repastes(self):
        self.queue.ingest({"update_id": 100, "message": message()})
        target = self.fake_target(ack=False)
        self.queue.deliver_one(target)
        self.assertEqual(self.queue.row(100)["status"], "unacknowledged")
        self.assertFalse(self.queue.deliver_one(target))
        target.inject.assert_called_once()
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM parent_reports").fetchone()[0], 1)
        target.accepted.return_value = True
        self.queue.reconcile_acceptance(target)
        self.assertEqual(self.queue.row(100)["status"], "delivered")
        target.inject.assert_called_once()

    def test_dynamic_captain_enrollment_team_intake_and_revocation(self):
        self.admin(100, "/group_on@MaychaFinance_Bot")
        self.assertIn(-200, self.queue.active_groups())
        self.queue.ingest({"update_id": 101, "message": message(sender=23, chat=-200, kind="group")})
        self.assertEqual(json.loads(self.queue.row(101)["envelope"])["routing"]["role"], "team")
        self.admin(102, "/group_off")
        self.assertNotIn(-200, self.queue.active_groups())
        self.assertFalse(self.queue.deliver_one(self.fake_target()))
        self.assertEqual(self.queue.row(101)["status"], "blocked_group")
        api = Telegram(self.cfg, self.queue.active_groups)
        api.call = Mock()
        with self.assertRaises(Refusal):
            api.send(-200, "reply")
        api.call.assert_not_called()

    def test_group_explicit_target_is_rejected_in_group_and_works_in_private_dm(self):
        self.admin(100, "/group_off -200", chat=-100)
        self.assertIn(-100, self.queue.active_groups())
        private = message(chat=42, kind="private")
        private["text"] = "/group_on -200"
        self.queue.ingest({"update_id": 101, "message": private}, self.api())
        self.assertIn(-200, self.queue.active_groups())

    def test_reconcile_cannot_overwrite_concurrent_completed_or_new_attempt(self):
        self.queue.ingest({"update_id": 100, "message": message()})
        target = self.fake_target(ack=False)
        self.queue.deliver_one(target)
        def completed(_):
            self.queue.set_status(100, "completed")
            return True
        target.accepted.side_effect = completed
        self.queue.reconcile_acceptance(target)
        self.assertEqual(self.queue.row(100)["status"], "completed")
        old_attempt = self.queue.row(100)["attempt"]
        with self.queue.db:
            self.queue.db.execute("UPDATE updates SET status='delivering',attempt='{}' WHERE update_id=100")
        self.assertFalse(self.queue.transition_attempt(100, old_attempt, "delivered"))
        self.assertEqual(self.queue.row(100)["status"], "delivering")

    def test_delivery_finish_paths_cannot_overwrite_concurrent_completed(self):
        for uid, result in ((100, False), (101, True), (102, "error")):
            self.queue.ingest({"update_id": uid, "message": message()})
            target = self.fake_target()
            def completed(*_):
                self.queue.set_status(uid, "completed")
                if result == "error":
                    raise OSError("simulated failure after another connection completed")
                return result
            if result == "error":
                target.inject.side_effect = completed
                with self.assertRaises(Refusal):
                    self.queue.deliver_one(target)
            else:
                target.await_ack.side_effect = completed
                self.queue.deliver_one(target)
            self.assertEqual(self.queue.row(uid)["status"], "completed")
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM parent_reports").fetchone()[0], 0)

    def test_unknown_bot_anonymous_and_wrong_suffix_cannot_enroll(self):
        self.admin(100, "/group_on", sender=23)
        self.admin(101, "/group_on", bot=True)
        self.admin(102, "/group_on@OtherBot")
        anonymous = message(chat=-200, kind="group")
        anonymous.update(text="/group_on", sender_chat={"id": -200})
        self.queue.ingest({"update_id": 103, "message": anonymous}, self.api())
        self.assertNotIn(-200, self.queue.active_groups())
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM group_audit WHERE outcome='rejected'").fetchone()[0], 4)

    def test_excluded_company_group_cannot_be_enabled_even_by_captain(self):
        self.queue.cfg = replace(self.cfg, excluded_groups=frozenset([-200]))
        self.admin(100, "/group_on")
        self.assertNotIn(-200, self.queue.active_groups())
        self.queue.ingest({"update_id": 101, "message": message(chat=-200, kind="group")})
        self.assertEqual(self.queue.db.execute("SELECT status FROM updates WHERE update_id=101").fetchone()[0], "ignored")

    def test_group_revocation_survives_restart_and_env_seed(self):
        self.admin(100, "/group_off", chat=-100)
        self.queue.db.close()
        self.queue = Queue(self.cfg)
        self.assertNotIn(-100, self.queue.active_groups())
        self.admin(100, "/group_on", chat=-100)  # conflicting same update is ignored
        self.assertNotIn(-100, self.queue.active_groups())

    def test_membership_failure_never_enrolls(self):
        value = message(chat=-200, kind="group")
        value["text"] = "/group_on"
        api = self.api()
        api.call.side_effect = Refusal("offline")
        self.queue.ingest({"update_id": 100, "message": value}, api)
        self.assertNotIn(-200, self.queue.active_groups())
        self.assertEqual(self.queue.cursor(), 101)

    def test_parent_request_is_idempotent_negative_and_never_captain_approval(self):
        request = self.request()
        first = self.queue.enqueue_parent(request)
        self.assertLess(first["update_id"], 0)
        self.assertEqual(self.queue.enqueue_parent(request)["status"], "duplicate")
        with self.assertRaises(Refusal):
            self.queue.enqueue_parent({**request, "body": "different"})
        with self.assertRaises(Refusal):
            self.queue.enqueue_parent({**request, "authority": {"role": "captain", "approval": True}})
        with self.assertRaises(Refusal):
            self.queue.enqueue_parent({**request, "child_id": "other-child"})
        self.assertEqual(self.queue.cursor(), 0)

    def test_parent_notify_uses_correlated_host_event_not_telegram(self):
        request = self.request()
        row = self.queue.enqueue_parent(request)
        response = self.home / "response.txt"
        response.write_text("Verified result.")
        with patch("bridge.Settings.load", return_value=self.cfg), patch("bridge.ControlClient") as control, \
                patch("bridge.Telegram", side_effect=AssertionError("must not send Telegram")), \
                patch("sys.argv", ["bridge.py", "notify", "--update", str(row["update_id"]), "--file", str(response)]), \
                contextlib.redirect_stdout(io.StringIO()):
            main()
            args = control.return_value.report.call_args.args
            self.assertEqual(args[2:4], (request["request_id"], request["correlation"]))
        self.assertEqual(self.queue.row(row["update_id"])["notified"], 1)

    def test_parent_request_limits_match_host_utf8_and_escaped_stdin(self):
        request = {**self.request(), "body": "\U0001f600" * 25000, "scope": {"x": "a" * 7992}}
        self.assertEqual(len(canonical(request["scope"])), 8000)
        encoded = json.dumps(request).encode()
        self.assertGreater(len(encoded), 300000)
        source = self.home / "parent-request.json"
        source.write_bytes(encoded)
        with patch("bridge.Settings.load", return_value=self.cfg), \
                patch("sys.argv", ["bridge.py", "enqueue-parent", "--request-file", str(source)]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        self.assertEqual(json.loads(output.getvalue())["status"], "queued")
        for change in ({"body": request["body"] + "a"}, {"scope": {"x": "a" * 7993}}, {"body": " \n"}, {"body": "x\x00y"}):
            with self.assertRaises(Refusal):
                self.queue.enqueue_parent({**request, **change})

    def test_quiesce_generation_gate_prevents_racing_delivery(self):
        target = self.fake_target()
        target.check_identity.return_value = (self.runtime, "%1")
        target.tmux.return_value = "%1"
        result = self.queue.control_check(target, True, self.runtime["nonce"])
        self.assertTrue(result["safe_to_stop"])
        self.queue.ingest({"update_id": 100, "message": message()})
        self.assertFalse(self.queue.deliver_one(target))
        self.assertEqual(self.queue.control_check(target, True, self.runtime["nonce"])["lease_id"], result["lease_id"])
        with self.assertRaises(Refusal):
            self.queue.control_resume("wrong")
        self.queue.control_resume(result["lease_id"])
        self.assertTrue(self.queue.deliver_one(target))

    def test_quiesce_refuses_active_worker_or_uncertain_request(self):
        target = self.fake_target()
        target.check_identity.return_value = (self.runtime, "%1")
        target.tmux.return_value = "%1\n%2"
        with self.assertRaises(Refusal):
            self.queue.control_check(target, True, self.runtime["nonce"])
        target.tmux.return_value = "%1"
        self.queue.ingest({"update_id": 100, "message": message()})
        self.queue.set_status(100, "uncertain")
        with self.assertRaises(Refusal):
            self.queue.control_check(target, True, self.runtime["nonce"])

    def test_brain_allowlist_hashes_and_no_private_memory_import(self):
        item = {"path": "config/crew-harness", "content": "codex\n", "sha256": hashlib.sha256(b"codex\n").hexdigest()}
        snapshot = {"schema": "brain-snapshot.v1", "source_commit": "abc", "files": [item], "revision": digest([item])}
        client = Mock(child_path="/v1/children/team-sandbox")
        client.request.return_value = snapshot
        self.assertEqual(sync_brain(self.cfg, client, atomic_json)["files"], 1)
        self.assertEqual((self.home / item["path"]).read_text(), "codex\n")
        bad = {**item, "path": "data/captain.md"}
        client.request.return_value = {**snapshot, "files": [bad], "revision": digest([bad])}
        with self.assertRaises(Refusal):
            sync_brain(self.cfg, client, atomic_json)
        self.assertFalse((self.home / "data/captain.md").exists())

    def test_brain_preserves_local_edits_and_rejects_checksum_mismatch(self):
        file = self.home / "config/crew-harness"
        file.parent.mkdir()
        file.write_text("local choice")
        item = {"path": "config/crew-harness", "content": "codex", "sha256": hashlib.sha256(b"codex").hexdigest()}
        client = Mock(child_path="/v1/children/team-sandbox")
        client.request.return_value = {"schema": "brain-snapshot.v1", "files": [item], "revision": digest([item])}
        with self.assertRaises(Refusal):
            sync_brain(self.cfg, client, atomic_json)
        self.assertEqual(file.read_text(), "local choice")
        item["sha256"] = "0" * 64
        client.request.return_value["revision"] = digest([item])
        with self.assertRaises(Refusal):
            sync_brain(self.cfg, client, atomic_json)

    def proposal_fixture(self):
        source = self.home / "proposal.json"
        source.write_text(json.dumps({"manifest": {"claims": [{"id": "claim-1", "text": "Verified operational fact", "evidence_ids": ["e-1"]}],
                         "evidence": [{"id": "e-1", "description": "Local observation", "source": "child-local record"}],
                         "scope": {"domain": "team"}}, "content": "Proposed knowledge only"}))
        client = Mock(child_path="/v1/children/team-sandbox")
        client.request.side_effect = lambda _method, _path, value: {"proposal_id": value["proposal_id"], "version": value["version"],
                                               "sha256": value["sha256"], "event_id": str(uuid.uuid4()), "approval_required": True}
        notifier = Mock()
        notifier.send.return_value = {"message_id": 7}
        return source, client, notifier

    def test_knowledge_is_immutable_idempotent_and_notifies_captain_once(self):
        source, client, notifier = self.proposal_fixture()
        first = propose(self.cfg, client, source, atomic_json, notifier)
        second = propose(self.cfg, client, source, atomic_json, notifier)
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        notifier.send.assert_called_once()
        self.assertEqual(notifier.send.call_args.args[0], self.cfg.captain)
        stored = Path(first["local_file"])
        value = json.loads(stored.read_text())
        value["content"] = "Changed after approval request"
        value.pop("sha256")
        source.write_text(json.dumps(value))
        with self.assertRaises(Refusal):
            propose(self.cfg, client, source, atomic_json, notifier)
        self.assertFalse((self.home / "data/captain.md").exists())

    def test_knowledge_notification_failure_is_uncertain_without_retry(self):
        source, client, notifier = self.proposal_fixture()
        notifier.send.side_effect = Refusal("network uncertain")
        with self.assertRaises(Refusal):
            propose(self.cfg, client, source, atomic_json, notifier)
        with self.assertRaises(Refusal):
            propose(self.cfg, client, source, atomic_json, notifier)
        notifier.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
