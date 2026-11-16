import io
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from maintenance.app import (
    MaintenanceApp,
    initial_window_size,
    repair_request,
    replacement_model_config,
    suggested_version,
)
from maintenance.core.process_lock import AlreadyRunning
from maintenance.model_config import ModelConfig
from maintenance.tencent_asr_config import TencentAsrConfig


class MaintenanceRequestTests(unittest.TestCase):
    def test_version_suggestion_uses_current_software_and_beijing_archive_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            (root / "data").mkdir()
            (root / "app/pubspec.yaml").write_text("version: 2.5.230101+8\n")
            with closing(sqlite3.connect(root / "data/couple_chat.db")) as db, db:
                db.execute("CREATE TABLE messages (ts INTEGER)")
                db.execute("INSERT INTO messages VALUES (strftime('%s', '2026-09-26 16:01:00'))")
            self.assertEqual(suggested_version(root), "2.5.260927")

    def test_initial_window_fits_scaled_and_small_screens(self):
        self.assertEqual(initial_window_size(1536, 960), (940, 740))
        self.assertEqual(initial_window_size(1024, 768), (924, 668))
        for screen_width, screen_height in ((1920, 1080), (800, 600), (640, 480)):
            width, height = initial_window_size(screen_width, screen_height)
            self.assertLessEqual(width, screen_width - 100)
            self.assertLessEqual(height, screen_height - 100)
            self.assertLessEqual(width, 940)
            self.assertLessEqual(height, 740)

    def test_blank_key_preserves_current_key_model_and_endpoint(self):
        current = ModelConfig("private-test-key-12345", "https://example.test/v1", "private-model")
        self.assertEqual(replacement_model_config(current, "  "), current)
        replaced = replacement_model_config(current, "  replacement-test-key  ")
        self.assertEqual(replaced.api_key, "replacement-test-key")
        self.assertEqual(replaced.base_url, current.base_url)
        self.assertEqual(replaced.model, current.model)

    def test_manual_repair_without_candidate_keeps_hash_and_listening_proof(self):
        item = {"msg_id": "123", "expected_text": "原文", "expected_audio_sha256": "a" * 64}
        body = repair_request(item, "verified", " 修正文字。 ", False)
        self.assertEqual(body["transcript"], "修正文字。")
        self.assertFalse(body["listened"])
        self.assertEqual(body["expected_audio_sha256"], "a" * 64)
        self.assertNotIn("candidate_model", body)
        rejected = repair_request(item, "rejected", "不应写回", True)
        self.assertIsNone(rejected["transcript"])
        self.assertTrue(rejected["listened"])

    def test_existing_candidate_audit_is_preserved(self):
        item = {
            "msg_id": "123",
            "expected_text": "原文",
            "expected_audio_sha256": "a" * 64,
            "candidate_model": "asr-model",
            "candidate_parameters": "{}",
            "expected_candidate": "候选",
        }
        body = repair_request(item, "verified", "文字", True)
        for field in ("candidate_model", "candidate_parameters", "expected_candidate"):
            self.assertEqual(body[field], item[field])


class MaintenanceWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.model = ModelConfig("private-test-key-12345")
        self.tencent = TencentAsrConfig("tencent-test-id-123", "tencent-test-secret-123")
        with (
            patch("maintenance.app.load_model_config", return_value=self.model),
            patch("maintenance.app.load_tencent_asr", return_value=self.tencent),
        ):
            self.app = MaintenanceApp(self.root)

    def tearDown(self):
        if hasattr(self, "app"):
            self.app._closed = True
            self.root.after_cancel(self.app._poll_id)
            self.root.destroy()

    def pump_until_idle(self):
        deadline = time.monotonic() + 3
        while self.app._busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app._busy, "maintenance operation did not finish")

    def test_only_one_build_entry_uses_full_workflow_without_credentials(self):
        notebook = next(child for child in self.root.winfo_children() if child.winfo_class() == "TNotebook")
        self.assertEqual(len(notebook.tabs()), 3)
        self.assertFalse(hasattr(self.app, "sync_tab"))
        self.app.version_name.set("0.1.260927")
        with patch.object(self.app, "_run") as run:
            with patch("maintenance.app.load_model_config", return_value=self.model):
                self.app._build_apk()
            self.assertEqual(
                run.call_args.args[1][-4:], ["-m", "maintenance.build", "--version-name", "0.1.260927"]
            )
            self.assertNotIn(self.model.api_key, str(run.call_args_list))

    def test_invalid_version_does_not_save_key_or_start_build(self):
        self.app.version_name.set("0.1.260229")
        with (
            patch("maintenance.app.save_model_config") as save,
            patch.object(self.app, "_run") as run,
            patch("maintenance.app.messagebox.showerror") as error,
        ):
            self.app._change_key()
        save.assert_not_called()
        run.assert_not_called()
        error.assert_called_once()

    def test_key_build_uses_the_same_workflow_and_entered_version(self):
        self.app.version_name.set("2.1.260926")
        with (
            patch("maintenance.app.load_model_config", return_value=self.model),
            patch("maintenance.app.save_model_config", return_value=self.model),
            patch("maintenance.app.process_lock"),
            patch.object(self.app, "_run") as run,
        ):
            self.app._change_key()
        self.assertEqual(
            run.call_args.args[1][-4:], ["-m", "maintenance.build", "--version-name", "2.1.260926"]
        )

    def test_four_step_progress_is_visible_in_status(self):
        self.app._events.put(("log", "[3/4] 正在生成每日短句……\n"))
        self.root.after_cancel(self.app._poll_id)
        self.app._drain_events()
        self.assertEqual(self.app.task_status.get(), "[3/4] 正在生成每日短句……")

    def test_log_is_compact_and_reserved_before_the_main_tabs(self):
        packed = self.root.pack_slaves()
        notebook = next(child for child in packed if child.winfo_class() == "TNotebook")
        log_frame = next(child for child in packed if child.winfo_class() == "TLabelframe")
        self.assertLess(packed.index(log_frame), packed.index(notebook))
        self.assertEqual(self.app.task_output.cget("height"), 6)
        width, height = initial_window_size(self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        minimum_width, minimum_height = self.root.minsize()
        self.assertLessEqual(minimum_width, width)
        self.assertLessEqual(minimum_height, height)

    def test_key_save_releases_process_lock_before_build(self):
        actions = []

        class Lock:
            def __enter__(self):
                actions.append("lock")

            def __exit__(self, *args):
                actions.append("unlock")

        self.app.model_key.set("")
        with (
            patch("maintenance.app.process_lock", return_value=Lock()),
            patch("maintenance.app.load_model_config", return_value=self.model),
            patch(
                "maintenance.app.save_model_config",
                side_effect=lambda path, config: actions.append("save") or config,
            ) as save,
            patch.object(self.app, "_run", side_effect=lambda *args: actions.append("build")),
        ):
            self.app._change_key()
        self.assertEqual(actions, ["lock", "save", "unlock", "build"])
        self.assertEqual(save.call_args.args[1].api_key, self.model.api_key)

    def test_global_task_lock_prevents_overlapping_actions(self):
        release = threading.Event()
        second_ran = []
        try:
            self.app._operation("first", lambda: release.wait(2))
            self.app._operation("second", lambda: second_ran.append(True))
            self.assertTrue(all(str(button.cget("state")) == "disabled" for button in self.app._buttons))
        finally:
            release.set()
            self.pump_until_idle()
        self.assertEqual(second_ran, [])
        self.assertTrue(all(str(button.cget("state")) == "normal" for button in self.app._buttons))

    def test_child_process_output_is_streamed_and_redacted(self):
        owner = self

        class Process:
            stdout = io.StringIO("开始处理\n" + owner.model.api_key + "\n完成转写\n")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def wait(self):
                return 0

        with patch("maintenance.app.subprocess.Popen", return_value=Process()) as popen:
            self.app._run("同步", ["python", "-m", "maintenance.sync"])
            self.pump_until_idle()
        output = self.app.task_output.get("1.0", "end")
        self.assertIn("开始处理", output)
        self.assertIn("完成转写", output)
        self.assertNotIn(self.model.api_key, output)
        self.assertIn("••••", output)
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")

    def test_opening_repair_uses_current_text_not_an_old_candidate(self):
        self.app.voice_listened.set(True)
        self.app._show_voice(
            {
                "item": {
                    "msg_id": "123",
                    "occurred_at_beijing": "2026-09-27 12:30",
                    "speaker": "self",
                    "current_text": "已经人工修正的文字。",
                    "candidate": "旧的错误候选",
                }
            }
        )
        self.assertEqual(self.app.voice_text.get("1.0", "end-1c"), "已经人工修正的文字。")
        self.assertIn("北京时间", self.app.voice_meta.get())
        self.assertIn(" · 你 · ", self.app.voice_meta.get())
        self.assertFalse(self.app.voice_listened.get())

    def test_other_process_lock_error_is_explained(self):
        self.assertIn("另一个维护任务", self.app._error_text(AlreadyRunning("internal lock message")))


if __name__ == "__main__":
    unittest.main()
