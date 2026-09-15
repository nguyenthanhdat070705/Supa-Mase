import json
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from bridge import Queue, Refusal, Settings, Target, Telegram, authorize, atomic_json, is_codex, proc_info


def message(sender=42, chat=42, kind="private", bot=False, topic=None):
    result = {"message_id": 7, "chat": {"id": chat, "type": kind},
              "from": {"id": sender, "is_bot": bot, "first_name": "Captain (untrusted name)"},
              "text": "Please review $(whoami); do not execute this as shell"}
    if topic:
        result.update(message_thread_id=topic, is_topic_message=True)
    return result


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Settings(Path(self.temp.name), 42, frozenset([-100]))
        self.queue = Queue(self.cfg)

    def tearDown(self):
        self.queue.db.close()
        self.temp.cleanup()

    def test_captain_in_private_requires_verified_sender_and_private_type(self):
        self.assertEqual(authorize(message(), self.cfg), "captain")
        self.assertIsNone(authorize(message(sender=23), self.cfg))
        self.assertIsNone(authorize(message(kind="group"), self.cfg))
        self.assertIsNone(authorize(message(chat=23), self.cfg))

    def test_captain_in_allowed_group_is_still_captain(self):
        self.assertEqual(authorize(message(chat=-100, kind="supergroup"), self.cfg), "captain")
        self.assertEqual(authorize(message(sender=23, chat=-100, kind="group"), self.cfg), "team")

    def test_unknown_chats_and_bot_or_anonymous_senders_rejected(self):
        self.assertIsNone(authorize(message(chat=-200, kind="supergroup"), self.cfg))
        self.assertIsNone(authorize(message(bot=True), self.cfg))
        self.assertIsNone(authorize(message(sender=23, chat=23), self.cfg))
        anonymous = message(chat=-100, kind="supergroup")
        anonymous["sender_chat"] = {"id": -100}
        self.assertIsNone(authorize(anonymous, self.cfg))

    def test_topic_and_reply_are_bound_to_verified_envelope(self):
        self.queue.ingest({"update_id": 100, "message": message(chat=-100, kind="supergroup", topic=11)})
        envelope = json.loads(self.queue.row(100)["envelope"])
        self.assertEqual(envelope["routing"], {"update_id": 100, "role": "captain", "chat_id": -100,
                                             "sender_id": 42, "message_id": 7, "message_thread_id": 11})
        self.assertIn("$(whoami)", envelope["telegram_data"]["text"])

    def test_duplicate_update_never_overwrites_authorized_original(self):
        self.queue.ingest({"update_id": 100, "message": message()})
        self.queue.set_status(100, "delivered")
        duplicate = message(sender=23, chat=-100, kind="group")
        duplicate["text"] = "replacement"
        self.queue.ingest({"update_id": 100, "message": duplicate})
        row = self.queue.row(100)
        self.assertEqual(row["status"], "delivered")
        self.assertNotIn("replacement", row["envelope"])
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM updates").fetchone()[0], 1)
        self.assertEqual(self.queue.cursor(), 101)

    def test_unknown_updates_are_recorded_before_cursor_without_content(self):
        self.queue.ingest({"update_id": 100, "message": message(chat=900, sender=900)})
        row = self.queue.db.execute("SELECT * FROM updates WHERE update_id=100").fetchone()
        self.assertEqual(row["status"], "ignored")
        self.assertIsNone(row["envelope"])
        self.assertEqual(self.queue.cursor(), 101)

    def test_only_verified_captain_can_record_unknown_group_candidate(self):
        captain_message = message(chat=-200, kind="supergroup")
        captain_message["chat"]["title"] = "Mac team"
        captain_message["text"] = "Private content must not be retained"
        self.queue.ingest({"update_id": 100, "message": captain_message})
        self.assertEqual(self.queue.pending_group_candidates(), [{"chat_id": -200, "title": "Mac team",
                         "sender_id": 42, "message_id": 7, "update_id": 100}])
        row = self.queue.db.execute("SELECT * FROM updates WHERE update_id=100").fetchone()
        self.assertEqual(row["status"], "ignored")
        self.assertIsNone(row["envelope"])
        self.assertNotIn("Private content", json.dumps(self.queue.pending_group_candidates()))
        target = Mock()
        self.assertFalse(self.queue.deliver_one(target))
        target.check_ready.assert_not_called()

    def test_unknown_users_bots_and_anonymous_senders_create_no_candidates(self):
        samples = [message(sender=23, chat=-200, kind="group"),
                   message(chat=-201, kind="supergroup", bot=True),
                   message(chat=200, kind="private")]
        anonymous = message(chat=-202, kind="supergroup")
        anonymous["sender_chat"] = {"id": -202}
        samples.append(anonymous)
        for uid, sample in enumerate(samples, start=100):
            self.queue.ingest({"update_id": uid, "message": sample})
        self.assertEqual(self.queue.pending_group_candidates(), [])
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM updates WHERE status='ignored'").fetchone()[0], 4)

    def test_group_candidate_is_durable_and_latest_metadata_only(self):
        sample = message(chat=-200, kind="group")
        self.queue.ingest({"update_id": 100, "message": sample})
        sample["message_id"] = 8
        sample["chat"]["title"] = "New title"
        self.queue.ingest({"update_id": 101, "message": sample})
        self.queue.db.close()
        self.queue = Queue(self.cfg)
        candidate = self.queue.pending_group_candidates()
        self.assertEqual(len(candidate), 1)
        self.assertEqual(candidate[0]["message_id"], 8)
        self.assertEqual(candidate[0]["update_id"], 101)
        self.assertEqual(candidate[0]["title"], "New title")
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM updates WHERE status='ignored'").fetchone()[0], 2)

    def test_allowlisted_group_has_no_pending_candidate_and_is_not_auto_enrolled(self):
        self.queue.ingest({"update_id": 100, "message": message(chat=-200, kind="group")})
        self.assertNotIn(-200, self.queue.cfg.groups)
        self.queue.ingest({"update_id": 101, "message": message(chat=-100, kind="group")})
        self.assertEqual(len(self.queue.pending_group_candidates()), 1)
        self.queue.cfg = Settings(self.cfg.home, self.cfg.captain, frozenset([-100, -200]))
        self.assertEqual(self.queue.pending_group_candidates(), [])
        self.assertEqual(self.queue.db.execute("SELECT status FROM updates WHERE update_id=100").fetchone()[0], "ignored")

    def test_not_ready_keeps_message_pending_across_reopen(self):
        self.queue.ingest({"update_id": 100, "message": message()})
        target = Mock()
        target.check_ready.side_effect = Refusal("not ready")
        with self.assertRaises(Refusal):
            self.queue.deliver_one(target)
        self.queue.db.close()
        self.queue = Queue(self.cfg)
        self.assertEqual(self.queue.cursor(), 101)
        self.assertEqual(self.queue.row(100)["status"], "pending")
        target.inject.assert_not_called()

    def test_delivery_failure_is_durable_and_never_auto_replayed(self):
        self.queue.ingest({"update_id": 100, "message": message()})
        target = Mock()
        target.inject.side_effect = RuntimeError("tmux failed after possible paste")
        with self.assertRaises(Refusal):
            self.queue.deliver_one(target)
        self.queue.db.close()
        self.queue = Queue(self.cfg)
        self.assertEqual(self.queue.row(100)["status"], "uncertain")
        self.assertTrue((self.cfg.state / "inbox/100.json").is_file())
        self.assertFalse(self.queue.deliver_one(target))
        self.assertEqual(target.inject.call_count, 1)

    def test_one_outstanding_message_serializes_delivery(self):
        for uid in [100, 101]:
            self.queue.ingest({"update_id": uid, "message": message()})
        target = Mock()
        self.assertTrue(self.queue.deliver_one(target))
        self.assertFalse(self.queue.deliver_one(target))
        self.assertEqual(self.queue.row(100)["status"], "delivered")
        self.assertEqual(self.queue.row(101)["status"], "pending")
        self.queue.set_status(100, "completed")
        self.assertTrue(self.queue.deliver_one(target))

    def test_shell_target_refused_even_with_matching_marker_and_pid(self):
        (self.cfg.home / ".fm-secondmate-home").write_text("team-sandbox\n")
        atomic_json(self.cfg.state / "runtime.json", {"home": str(self.cfg.home), "target": self.cfg.target,
                                                       "pid": 123, "start": "456", "nonce": "test"})
        target = Target(self.cfg)
        target.tmux = Mock(return_value="%0|123|0|bash|/dev/pts/0")
        with patch("bridge.proc_info", return_value={"start": "456", "argv": ["/bin/bash"]}):
            with self.assertRaisesRegex(Refusal, "shell target refused"):
                target.check_identity()

    def test_literal_pointer_never_contains_telegram_input(self):
        target = Target(self.cfg)
        target.check_identity = Mock(return_value=({"nonce": "test"}, "%17"))
        target.tmux = Mock()
        target.inject(100)
        calls = target.tmux.call_args_list
        self.assertEqual(calls[0].args[:3], ("load-buffer", "-b", "secondmate-inbox-100"))
        self.assertIn("/inbox/100.json", calls[0].kwargs["input"])
        self.assertNotIn("$(whoami)", calls[0].kwargs["input"])
        self.assertNotIn("\n", calls[0].kwargs["input"])
        self.assertTrue(calls[0].kwargs["input"].startswith("# SECONDMATE_INBOX 100 "))
        self.assertEqual(calls[1].args[-2:], ("-t", "%17"))
        self.assertEqual(calls[2].args, ("send-keys", "-t", "%17", "Enter"))

    def test_identity_change_after_paste_withholds_enter(self):
        target = Target(self.cfg)
        identity = ({"nonce": "original"}, "%17")
        target.check_identity = Mock(side_effect=[identity, identity, ({"nonce": "replacement"}, "%18")])
        target.tmux = Mock()
        with self.assertRaisesRegex(Refusal, "Enter withheld"):
            target.inject(100)
        self.assertEqual(target.tmux.call_count, 2)
        self.assertEqual(target.tmux.call_args.args, ("paste-buffer", "-d", "-b", "secondmate-inbox-100", "-t", "%17"))

    def test_identity_change_before_paste_never_pastes(self):
        target = Target(self.cfg)
        target.check_identity = Mock(side_effect=[({"nonce": "original"}, "%17"), ({"nonce": "replacement"}, "%18")])
        target.tmux = Mock()
        with self.assertRaisesRegex(Refusal, "before paste"):
            target.inject(100)
        self.assertEqual(target.tmux.call_count, 1)

    def setup_process_target(self, foreground="node", runtime_pid=123):
        (self.cfg.home / ".fm-secondmate-home").write_text("team-sandbox\n")
        runtime = {"home": str(self.cfg.home), "target": self.cfg.target,
                   "pid": runtime_pid, "start": "456", "nonce": "verified"}
        atomic_json(self.cfg.state / "runtime.json", runtime)
        target = Target(self.cfg)
        target.tmux = Mock(return_value=f"%17|123|0|{foreground}|/dev/pts/0")
        return target, runtime

    def test_verified_codex_node_wrapper_foreground_is_accepted(self):
        target, runtime = self.setup_process_target()
        process = {"start": "456", "argv": ["/usr/bin/node", "/opt/codex/lib/node_modules/@openai/codex/bin/codex.js"],
                   "pgrp": 123, "tty_nr": 34816, "tpgid": 123}
        environment = f"FM_HOME={self.cfg.home}\0SM_AGENT_RUNTIME_ID=verified\0".encode()
        with patch("bridge.proc_info", return_value=process), patch.object(Path, "read_bytes", return_value=environment), \
                patch("bridge.tty_identity", return_value=(136, 0)), \
                patch("bridge.os.tcgetpgrp", side_effect=OSError(errno.ENOTTY, "not caller controlling tty"), create=True) as ioctl:
            self.assertEqual(target.check_identity(), (runtime, "%17"))
            ioctl.assert_not_called()

    def test_proc_stat_foreground_and_controlling_tty_fields(self):
        fields = ["S", "1", "123", "123", "34816", "123"] + ["0"] * 13 + ["456"]
        with patch.object(Path, "read_text", return_value="123 (node) " + " ".join(fields)), \
                patch.object(Path, "read_bytes", return_value=b"/usr/bin/node\0/opt/codex/bin/codex\0"):
            result = proc_info(123)
        self.assertEqual((result["tty_nr"], result["tpgid"], result["pgrp"], result["start"]), (34816, 123, 123, "456"))

    def test_kernel_foreground_group_must_still_match_agent(self):
        target, _ = self.setup_process_target(foreground="codex")
        process = {"start": "456", "argv": ["/usr/bin/codex"], "pgrp": 123, "tty_nr": 34816, "tpgid": 999}
        environment = f"FM_HOME={self.cfg.home}\0SM_AGENT_RUNTIME_ID=verified\0".encode()
        with patch("bridge.proc_info", return_value=process), patch.object(Path, "read_bytes", return_value=environment), \
                patch("bridge.tty_identity", return_value=(136, 0)):
            with self.assertRaisesRegex(Refusal, "Foreground process group differs"):
                target.check_identity()

    def test_kernel_controlling_tty_must_match_pane_device(self):
        target, _ = self.setup_process_target(foreground="codex")
        process = {"start": "456", "argv": ["/usr/bin/codex"], "pgrp": 123, "tty_nr": 34816, "tpgid": 123}
        environment = f"FM_HOME={self.cfg.home}\0SM_AGENT_RUNTIME_ID=verified\0".encode()
        with patch("bridge.proc_info", return_value=process), patch.object(Path, "read_bytes", return_value=environment), \
                patch("bridge.tty_identity", return_value=(136, 1)):
            with self.assertRaisesRegex(Refusal, "controlling terminal differs"):
                target.check_identity()

    def test_process_without_controlling_tty_is_refused(self):
        target, _ = self.setup_process_target(foreground="codex")
        process = {"start": "456", "argv": ["/usr/bin/codex"], "pgrp": 123, "tty_nr": 0, "tpgid": -1}
        environment = f"FM_HOME={self.cfg.home}\0SM_AGENT_RUNTIME_ID=verified\0".encode()
        with patch("bridge.proc_info", return_value=process), patch.object(Path, "read_bytes", return_value=environment):
            with self.assertRaisesRegex(Refusal, "no controlling terminal"):
                target.check_identity()

    def test_arbitrary_node_foreground_is_rejected(self):
        target, _ = self.setup_process_target()
        process = {"start": "456", "argv": ["/usr/bin/node", "/tmp/pretend-agent.js"], "pgrp": 123}
        with patch("bridge.proc_info", return_value=process):
            with self.assertRaisesRegex(Refusal, "shell target refused"):
                target.check_identity()

    def test_stale_runtime_pid_is_rejected(self):
        target, _ = self.setup_process_target(runtime_pid=124)
        with self.assertRaisesRegex(Refusal, "Pane runtime identity mismatch"):
            target.check_identity()

    def test_reused_pid_with_different_start_time_is_rejected(self):
        target, _ = self.setup_process_target()
        with patch("bridge.proc_info", return_value={"start": "789", "argv": ["/usr/bin/codex"]}):
            with self.assertRaisesRegex(Refusal, "shell target refused"):
                target.check_identity()

    def test_process_with_stale_runtime_nonce_is_rejected(self):
        target, _ = self.setup_process_target(foreground="codex")
        environment = f"FM_HOME={self.cfg.home}\0SM_AGENT_RUNTIME_ID=stale\0".encode()
        with patch("bridge.proc_info", return_value={"start": "456", "argv": ["/usr/bin/codex"]}), \
                patch.object(Path, "read_bytes", return_value=environment):
            with self.assertRaisesRegex(Refusal, "home/nonce mismatch"):
                target.check_identity()

    def test_stale_readiness_is_rejected(self):
        target = Target(self.cfg)
        target.check_identity = Mock(return_value=({"nonce": "current"}, "%17"))
        atomic_json(target.ready, {"nonce": "previous", "pane": "%17"})
        with self.assertRaisesRegex(Refusal, "stale runtime"):
            target.check_ready()

    def test_bot_identity_and_state_binding(self):
        api = Telegram(self.cfg)
        api.call = Mock(return_value={"id": 77, "is_bot": True, "username": "AnotherBot"})
        with self.assertRaises(Refusal):
            api.verify()
        self.queue.bind_bot(77)
        self.queue.bind_bot(77)
        with self.assertRaises(Refusal):
            self.queue.bind_bot(78)

    def test_outbound_allowlist_and_reply_topic(self):
        api = Telegram(self.cfg)
        api.call = Mock(return_value=True)
        with self.assertRaises(Refusal):
            api.send(999, "hello")
        with self.assertRaises(Refusal):
            api.send(42, "hello", topic=11)
        api.call.assert_not_called()
        api.send(-100, "hello", topic=11, reply_to=7)
        payload = api.call.call_args.args[1]
        self.assertEqual(payload["message_thread_id"], 11)
        self.assertEqual(payload["reply_parameters"], {"message_id": 7, "allow_sending_without_reply": False})

    def test_parent_status_cursor_waits_for_newline_and_forwards_once(self):
        path = self.cfg.home / "parent.status"
        path.write_text("done: PR https://example.test/123\nfailed: incom", encoding="utf-8")
        self.queue.collect_parent_reports(path)
        api = Mock()
        self.assertTrue(self.queue.forward_parent_report(api))
        self.queue.collect_parent_reports(path)
        self.assertFalse(self.queue.forward_parent_report(api))
        with open(path, "a", encoding="utf-8") as out:
            out.write("plete action\n")
        self.queue.collect_parent_reports(path)
        self.assertTrue(self.queue.forward_parent_report(api))
        self.assertEqual(api.send.call_count, 2)
        self.assertIn("failed: incomplete action", api.send.call_args.args[1])

    def test_failed_parent_forward_is_retained_without_duplicate_retry(self):
        path = self.cfg.home / "parent.status"
        path.write_text("failed: model unavailable\n", encoding="utf-8")
        self.queue.collect_parent_reports(path)
        api = Mock()
        api.send.side_effect = Refusal("network failure")
        with self.assertRaises(Refusal):
            self.queue.forward_parent_report(api)
        self.assertFalse(self.queue.forward_parent_report(api))
        self.assertEqual(self.queue.db.execute("SELECT status FROM parent_reports").fetchone()[0], "uncertain")


if __name__ == "__main__":
    unittest.main()
