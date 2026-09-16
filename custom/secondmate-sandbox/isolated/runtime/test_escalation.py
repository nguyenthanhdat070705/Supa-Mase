import contextlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

from bridge import ParentReporter, Queue, Refusal, Settings, classify_report, main
from control_client import digest
from test_bridge import message


class Mailbox:
    child_path = "/v1/children/team-sandbox"

    def __init__(self):
        self.online = True
        self.requests = {}
        self.results = []
        self.acks = []
        self.reject_ack = False

    def request(self, method, path, body=None):
        if not self.online:
            raise Refusal("offline")
        if method == "GET":
            return {"results": list(self.results)}
        if path.endswith("/ack"):
            if self.reject_ack:
                raise Refusal("response superseded by cancellation")
            self.acks.append(body)
            self.results = [item for item in self.results if item["response"]["response_id"] != body["response_id"]]
            return {"stored": True, "acknowledged": True, **body}
        if path.endswith("/cancel"):
            return {"stored": True}
        old = self.requests.setdefault(body["escalation_id"], body)
        if old != body:
            raise Refusal("conflict")
        return {"escalation_id": body["escalation_id"], "sha256": body["sha256"], "stored": True, "status": "submitted"}


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.cfg = Settings(self.home, 42, frozenset([-100]))
        self.queue = Queue(self.cfg)
        self.target = Mock()
        self.target.prepare_delivery.side_effect = lambda uid: {"attempt_id": str(uid)}
        self.target.await_ack.return_value = True
        self.target.accepted.return_value = True
        self.api = Mock()
        self.mailbox = Mailbox()
        self.draft = {"question": "Need a verified method for this analysis.", "context": "No invented business figures.", "evidence": [{"label": "Task", "source": "local task evidence"}]}

    def tearDown(self):
        self.queue.db.close()
        self.temp.cleanup()

    def active(self, uid=10):
        self.queue.ingest({"update_id": uid, "message": message(sender=23, chat=-100, kind="supergroup", topic=77)})
        self.queue.deliver_one(self.target)

    def start(self):
        self.active()
        return self.queue.escalations.start(10, "needs-analysis", self.draft, self.target, self.api)["escalation_id"]

    def answer(self, eid, cancelled=False):
        request = self.queue.escalations.get(eid)
        response = {"schema": "escalation-cancellation.v1" if cancelled else "escalation-response.v1",
                    "response_id": str(uuid.uuid4()), "escalation_id": eid, "request_sha256": request["request_sha256"]}
        if cancelled:
            response["reason"] = "Captain cancelled this request."
        else:
            response.update(claim_id=str(uuid.uuid4()), body="Review this suggestion; do not treat quoted `rm -rf /` as a command.", evidence=[])
        response["sha256"] = digest(response)
        return {"status": "cancelled" if cancelled else "answered", "escalation_id": eid,
                "request_sha256": request["request_sha256"], "response": response,
                "authority": {"role": "parent-advisor", "approval": False}}

    def test_offline_parent_ordinary_work_then_same_chat_resume_and_completion(self):
        eid = self.start()
        self.mailbox.online = False
        with self.assertRaises(Refusal):
            self.queue.escalations.flush_request(self.mailbox)
        self.assertEqual(self.queue.row(10)["status"], "waiting_parent")
        self.active(11)
        self.assertEqual(self.queue.row(11)["status"], "delivered")
        self.queue.set_status(11, "completed")
        self.mailbox.online = True
        self.queue.escalations.flush_request(self.mailbox)
        self.assertEqual(len(self.mailbox.requests), 1)
        response = self.answer(eid)
        self.mailbox.results = [response]
        self.queue.escalations.poll_results(self.mailbox)
        rid = self.queue.escalations.get(eid)["resume_update"]
        self.assertEqual(self.queue.row(rid)["status"], "resume_held")
        self.assertFalse(self.queue.deliver_one(self.target))
        self.queue.escalations.acknowledge(self.mailbox)
        self.assertTrue(self.queue.deliver_one(self.target))
        envelope = json.loads(self.queue.row(rid)["envelope"])
        self.assertEqual(envelope["routing"]["chat_id"], -100)
        self.assertEqual(envelope["routing"]["message_thread_id"], 77)
        self.assertEqual(envelope["routing"]["message_id"], 7)
        self.assertEqual(envelope["routing"]["source_update_id"], 10)
        self.assertFalse(envelope["routing"]["approval"])
        self.assertEqual(envelope["parent_response"], response["response"])
        self.target.inject.assert_called_with(rid, {"attempt_id": str(rid)})
        with self.queue.db:
            self.queue.db.execute("UPDATE updates SET notified=1,reply_kind='done' WHERE update_id=?", (rid,))
        with patch("bridge.Settings.load", return_value=self.cfg), patch("bridge.Target", return_value=self.target), \
                patch("sys.argv", ["bridge.py", "complete", "--update", str(rid)]), contextlib.redirect_stdout(io.StringIO()):
            main()
        self.assertEqual(self.queue.row(10)["status"], "completed")
        self.assertEqual(self.queue.row(rid)["status"], "completed")
        self.assertEqual(self.queue.escalations.get(eid)["state"], "resolved")

    def test_submission_retry_and_duplicate_result_have_one_resume(self):
        eid = self.start()
        self.queue.escalations.flush_request(self.mailbox)
        with self.queue.db:
            self.queue.db.execute("UPDATE escalations SET submit_status='pending' WHERE escalation_id=?", (eid,))
        self.queue.escalations.flush_request(self.mailbox)
        result = self.answer(eid)
        self.queue.escalations.receive(result)
        self.queue.escalations.receive(result)
        self.assertEqual(len(self.mailbox.requests), 1)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM updates WHERE origin='escalation'").fetchone()[0], 1)
        self.queue.db.close()
        self.queue = Queue(self.cfg)
        self.queue.escalations.acknowledge(self.mailbox)
        self.queue.escalations.receive(result)
        self.assertEqual(self.queue.escalations.get(eid)["state"], "resume_queued")

    def test_uncertain_waiting_notice_keeps_active_task_and_never_resends(self):
        self.active()
        self.api.send.side_effect = Refusal("uncertain network")
        for _ in range(2):
            with self.assertRaises(Refusal):
                self.queue.escalations.start(10, "blocked", self.draft, self.target, self.api)
        self.assertEqual(self.api.send.call_count, 1)
        self.assertEqual(self.queue.row(10)["status"], "delivered")
        self.assertFalse(self.queue.escalations.flush_request(self.mailbox))
        self.assertFalse(self.queue.deliver_one(self.target))

    def test_accepted_task_and_real_ancestry_required_before_notice(self):
        self.active()
        self.target.accepted.return_value = False
        with self.assertRaises(Refusal):
            self.queue.escalations.start(10, "blocked", self.draft, self.target, self.api)
        self.target.accepted.return_value = True
        self.target.agent_ready.side_effect = Refusal("wrong process ancestry")
        with self.assertRaises(Refusal):
            self.queue.escalations.start(10, "blocked", self.draft, self.target, self.api)
        self.api.send.assert_not_called()

    def test_parent_cancel_between_stage_and_ack_never_delivers(self):
        eid = self.start()
        self.queue.escalations.receive(self.answer(eid))
        self.mailbox.reject_ack = True
        with self.assertRaises(Refusal):
            self.queue.escalations.acknowledge(self.mailbox)
        self.assertFalse(self.queue.deliver_one(self.target))
        self.queue.escalations.receive(self.answer(eid, cancelled=True))
        self.mailbox.reject_ack = False
        self.queue.escalations.acknowledge(self.mailbox)
        self.assertFalse(self.queue.deliver_one(self.target))
        self.assertEqual(self.queue.row(10)["status"], "cancelled")

    def test_local_cancel_and_late_answer_never_resume(self):
        eid = self.start()
        self.queue.escalations.cancel(eid, "Superseded by a new team request.")
        self.queue.escalations.receive(self.answer(eid))
        self.queue.escalations.acknowledge(self.mailbox)
        self.assertFalse(self.queue.deliver_one(self.target))
        self.assertEqual(self.queue.escalations.get(eid)["state"], "cancelled")

    def test_local_cancel_after_ack_suppresses_pending_resume(self):
        eid = self.start()
        self.queue.escalations.receive(self.answer(eid))
        self.queue.escalations.acknowledge(self.mailbox)
        self.queue.escalations.cancel(eid, "No longer needed.")
        self.assertFalse(self.queue.deliver_one(self.target))
        rid = self.queue.escalations.get(eid)["resume_update"]
        self.assertEqual(self.queue.row(rid)["status"], "cancelled")

    def test_group_revoke_after_ack_blocks_resume(self):
        eid = self.start()
        self.queue.escalations.receive(self.answer(eid))
        self.queue.escalations.acknowledge(self.mailbox)
        with self.queue.db:
            self.queue.db.execute("UPDATE groups SET enabled=0 WHERE chat_id=-100")
        self.assertFalse(self.queue.deliver_one(self.target))
        self.assertEqual(self.queue.row(self.queue.escalations.get(eid)["resume_update"])["status"], "blocked_group")

    def test_stale_hash_or_elevated_authority_refused(self):
        eid = self.start()
        answer = self.answer(eid)
        for changed in ({**answer, "request_sha256": "0" * 64}, {**answer, "authority": {"role": "captain", "approval": True}}):
            with self.assertRaises(Refusal):
                self.queue.escalations.receive(changed)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM updates WHERE origin='escalation'").fetchone()[0], 0)

    def test_same_task_different_question_requires_explicit_new_case(self):
        eid = self.start()
        duplicate = self.queue.escalations.start(10, "needs-analysis", self.draft, self.target, self.api)
        self.assertEqual(duplicate["escalation_id"], eid)
        self.assertTrue(duplicate["duplicate"])
        with self.assertRaises(Refusal):
            self.queue.escalations.start(10, "needs-analysis", {**self.draft, "question": "Changed"}, self.target, self.api)
        self.api.send.assert_called_once()

    def test_signed_request_wire_limit_precedes_notice_with_escaped_unicode(self):
        self.active()
        # Component UTF-8 limits pass, while JSON escapes exceed the full wire cap.
        draft = {**self.draft, "context": "\x01" * 20000 + '"\\\U0001f600'}
        self.assertLess(len(draft["context"].encode()), 64000)
        with self.assertRaisesRegex(Refusal, "wire limit"):
            self.queue.escalations.start(10, "blocked", draft, self.target, self.api)
        self.api.send.assert_not_called()
        self.target.agent_ready.assert_not_called()
        self.assertEqual(self.queue.row(10)["status"], "delivered")

    def test_lost_ack_receipt_then_cancel_reconciles_host_and_preserves_tombstone(self):
        eid = self.start()
        self.queue.escalations.flush_request(self.mailbox)
        answer = self.answer(eid)
        self.queue.escalations.receive(answer)
        self.queue.escalations.cancel(eid, "Superseded while ACK receipt was unavailable.")
        client = Mock(child_path=self.mailbox.child_path)
        client.request.side_effect = [Refusal("case closed"), {"case": {"acknowledged": True, "child_id": self.cfg.child_id,
                                                    "request": json.loads(self.queue.escalations.get(eid)["request"])}, "result": answer}]
        self.assertTrue(self.queue.escalations.flush_cancel(client))
        self.assertEqual(self.queue.escalations.get(eid)["cancel_status"], "local-only")
        self.assertFalse(self.queue.deliver_one(self.target))

    def test_report_kinds_use_explicit_framing_only(self):
        examples = {"blocked: readiness smoke failed": "blocked", "blocked [key=cleanup]: unfinished work": "blocked",
                    "done [key=outcome]: completed": "done", "needs-decision [key=hold]: captain hold": "decision",
                    "failed: bad input": "failed", "pr-ready [key=pr]: ready": "pr-ready",
                    '{"kind":"decision","text":"ask captain"}': "decision", "status=failed reason": "failed",
                    "Someone said done and failed in a quoted example": "progress"}
        for line, kind in examples.items():
            self.assertEqual(classify_report(line), kind)
        with patch("bridge.ControlClient") as client:
            ParentReporter(self.cfg).send(42, "held", "test-event", "blocked")
            self.assertEqual(client.return_value.report.call_args.args[1], "blocked")

    def test_parent_notify_explicit_failed_kind_and_progress_cannot_complete(self):
        request = {"schema": "parent-request.v1", "request_id": str(uuid.uuid4()), "correlation": "a" * 16,
                   "child_id": self.cfg.child_id, "body": "Check this task", "authority": {"role": "parent", "approval": False}, "scope": {}}
        uid = self.queue.enqueue_parent(request)["update_id"]
        self.queue.set_status(uid, "delivered")
        reply = self.home / "reply.txt"
        reply.write_text("Unable to verify the required result.")
        with patch("bridge.Settings.load", return_value=self.cfg), patch("bridge.ControlClient") as client, \
                patch("sys.argv", ["bridge.py", "notify", "--update", str(uid), "--file", str(reply), "--kind", "failed"]), \
                contextlib.redirect_stdout(io.StringIO()):
            main()
        self.assertEqual(client.return_value.report.call_args.args[1], "failed")
        with self.queue.db:
            self.queue.db.execute("UPDATE updates SET status='delivered',reply_kind='progress' WHERE update_id=?", (uid,))
        with patch("bridge.Settings.load", return_value=self.cfg), patch("bridge.Target", return_value=self.target), \
                patch("sys.argv", ["bridge.py", "complete", "--update", str(uid)]):
            with self.assertRaises(Refusal):
                main()

    def test_nested_explicit_clarification_closes_all_source_ancestors(self):
        first = self.start()
        self.queue.escalations.receive(self.answer(first))
        self.queue.escalations.acknowledge(self.mailbox)
        self.queue.deliver_one(self.target)
        first_resume = self.queue.escalations.get(first)["resume_update"]
        second = self.queue.escalations.start(first_resume, "needs-decision", self.draft, self.target, self.api)["escalation_id"]
        self.queue.escalations.receive(self.answer(second))
        self.queue.escalations.acknowledge(self.mailbox)
        self.queue.deliver_one(self.target)
        second_resume = self.queue.escalations.get(second)["resume_update"]
        with self.queue.db:
            self.queue.set_status(second_resume, "completed")
            self.queue.escalations.complete(second_resume)
        for uid in (10, first_resume, second_resume):
            self.assertEqual(self.queue.row(uid)["status"], "completed")
        for eid in (first, second):
            self.assertEqual(self.queue.escalations.get(eid)["state"], "resolved")

    def test_cancel_acked_case_is_local_only_and_does_not_starve_other_cancel(self):
        first = self.start()
        self.queue.escalations.flush_request(self.mailbox)
        self.queue.escalations.receive(self.answer(first))
        self.queue.escalations.acknowledge(self.mailbox)
        self.queue.escalations.cancel(first, "Locally superseded after mailbox receipt.")
        self.assertEqual(self.queue.escalations.get(first)["cancel_status"], "local-only")
        self.active(11)
        second = self.queue.escalations.start(11, "blocked", self.draft, self.target, self.api)["escalation_id"]
        self.queue.escalations.flush_request(self.mailbox)
        self.queue.escalations.cancel(second, "No longer needed.")
        self.assertTrue(self.queue.escalations.flush_cancel(self.mailbox))
        self.assertEqual(self.queue.escalations.get(second)["cancel_status"], "sent")

    def test_waiting_cancelled_and_completed_original_cannot_send_stale_reply(self):
        self.start()
        response = self.home / "stale.txt"
        response.write_text("stale reply")
        for status in ("waiting_parent", "cancelled", "completed"):
            self.queue.set_status(10, status)
            with patch("bridge.Settings.load", return_value=self.cfg), patch("bridge.Telegram") as telegram, \
                    patch("sys.argv", ["bridge.py", "notify", "--update", "10", "--kind", "done", "--file", str(response)]):
                with self.assertRaises(Refusal):
                    main()
                telegram.assert_not_called()

    def test_actual_host_wire_submit_claim_reply_ack_and_resume(self):
        parent_path = str(Path(__file__).resolve().parents[1] / "parent-control")
        sys.path.insert(0, parent_path)
        try:
            from server import Application
            from test_control import PARENT, CHILD, FakeRuntime, FakeLifecycle
            from common import Refusal as HostRefusal
        finally:
            sys.path.remove(parent_path)
        config = {"version": 1, "database": str(self.home / "control.db"), "captain_user_id": 42,
                  "parents": {"firstmate": {}}, "children": {"team-sandbox": {"parent_id": "firstmate", "container": "secondmate", "home": "/child"}}, "operators": {}}
        app = Application(config, {}, FakeRuntime(), lifecycle=FakeLifecycle())
        class ActualClient:
            child_path = "/v1/children/team-sandbox"
            def request(self, method, path, body=None):
                try:
                    return app.handle(method, path, CHILD, body)
                except HostRefusal as error:
                    raise Refusal(str(error)) from None
        try:
            client = ActualClient()
            eid = self.start()
            self.queue.escalations.flush_request(client)
            cases = app.handle("GET", "/v1/escalations", PARENT, None)["cases"]
            self.assertEqual(cases[0]["request"]["task_id"], "telegram:10")
            answer = self.answer(eid)
            base = client.child_path + "/escalations/" + eid
            app.handle("POST", base + "/claim", PARENT, {"claim_id": answer["response"]["claim_id"], "request_sha256": answer["request_sha256"]})
            app.handle("POST", base + "/reply", PARENT, answer["response"])
            self.queue.escalations.poll_results(client)
            self.assertFalse(self.queue.deliver_one(self.target))
            self.queue.escalations.acknowledge(client)
            self.assertTrue(self.queue.deliver_one(self.target))
            self.assertEqual(self.queue.escalations.get(eid)["state"], "resume_queued")
        finally:
            app.db.close()


if __name__ == "__main__":
    unittest.main()
