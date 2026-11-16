"""Independent, offline safety checks for the one-click maintenance workflow."""

import base64
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from maintenance import build
from maintenance.core.process_lock import AlreadyRunning, process_lock
from maintenance.model_config import ModelConfig


class BuildWorkflowReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "data").mkdir()
        (self.root / "app").mkdir()
        self.pubspec = self.root / "app/pubspec.yaml"
        self.original = "name: couple_app\nversion: 0.1.260927+6\n"
        self.pubspec.write_text(self.original, encoding="utf-8")
        self.key = "private-workflow-probe-key-012345"
        self.config = ModelConfig(self.key)
        self.lines = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.loader = self.stack.enter_context(
            patch.object(build, "load_model_config", return_value=self.config)
        )
        self.synchronizer = self.stack.enter_context(
            patch.object(
                build,
                "run_sync",
                return_value={
                    "status": "completed",
                    "voice_failed": 0,
                    "after": {"voice_states": {"asr_accepted": 4, "verified": 2}},
                },
            )
        )
        self.publisher = self.stack.enter_context(
            patch.object(
                build,
                "build_release",
                return_value={
                    "status": "released",
                    "version_name": "0.1.260927",
                    "version_code": 7,
                    "apk": "private-test-output.apk",
                },
            )
        )

    def run_workflow(self, **kwargs):
        return build.run_build(self.root, emit=self.lines.append, **kwargs)

    def assert_no_data_stage(self):
        self.synchronizer.assert_not_called()
        self.publisher.assert_not_called()
        self.assertEqual(self.pubspec.read_text(encoding="utf-8"), self.original)
        self.assertFalse((self.root / "data/backups").exists())
        self.assertFalse((self.root / "releases").exists())

    def test_sync_failure_report_stops_before_any_release_work(self):
        self.synchronizer.return_value = {
            "status": "failed",
            "error_code": "import_failed",
            "voice_failed": 0,
        }
        with self.assertRaises(Exception):
            self.run_workflow()
        self.publisher.assert_not_called()
        self.assertEqual(self.pubspec.read_text(encoding="utf-8"), self.original)

    def test_partial_asr_or_adoption_never_counts_as_success(self):
        for reason in ("transcription_unavailable", "adoption_failed", "voice_retry_needed"):
            with self.subTest(reason=reason):
                self.synchronizer.return_value = {
                    "status": "partial",
                    "error_code": reason,
                    "voice_failed": 0,
                    "after": {"voice_states": {"pending": 2}},
                }
                with self.assertRaises(Exception):
                    self.run_workflow()
                self.publisher.assert_not_called()

    def test_inconsistent_success_with_failed_or_pending_asr_is_rejected(self):
        for failed, pending in ((1, 0), (0, 1)):
            with self.subTest(failed=failed, pending=pending):
                self.synchronizer.return_value = {
                    "status": "completed",
                    "voice_failed": failed,
                    "after": {"voice_states": {"pending": pending}},
                }
                with self.assertRaises(Exception):
                    self.run_workflow()
                self.publisher.assert_not_called()

    def test_deliberate_discards_and_missing_originals_do_not_fake_asr_failure(self):
        self.synchronizer.return_value = {
            "status": "completed",
            "voice_failed": 0,
            "after": {
                "voice_states": {
                    "verified": 3,
                    "discarded": 2,
                    "missing_audio": 1,
                    "missing_reference": 1,
                }
            },
        }
        self.run_workflow(software_version="0.2")
        self.publisher.assert_called_once()
        self.assertEqual(self.publisher.call_args.kwargs["software_version"], "0.2")

    def test_unexpected_sync_exception_stops_build_and_releases_lock(self):
        self.synchronizer.side_effect = OSError("temporary read failure")
        with self.assertRaises(Exception):
            self.run_workflow()
        self.publisher.assert_not_called()
        with process_lock(self.root / "data/maintenance.lock"):
            pass

    def test_one_real_common_lock_covers_both_stages_without_reentry(self):
        order = []

        def verify_lock(stage, **kwargs):
            self.assertIs(kwargs["_maintenance_lock_held"], True)
            with self.assertRaises(AlreadyRunning):
                with process_lock(self.root / "data/maintenance.lock"):
                    self.fail("Another maintenance process could enter the pipeline")
            order.append(stage)

        report = self.synchronizer.return_value
        artifact = self.publisher.return_value

        def synchronize(**kwargs):
            verify_lock("sync", **kwargs)
            return report

        def publish(*args, **kwargs):
            verify_lock("release", **kwargs)
            return artifact

        self.synchronizer.side_effect = synchronize
        self.publisher.side_effect = publish
        self.run_workflow(version_name="0.2.260927")
        self.assertEqual(order, ["sync", "release"])
        self.assertEqual(self.publisher.call_args.kwargs["version_name"], "0.2.260927")

    def test_existing_maintenance_lock_blocks_before_sync(self):
        with process_lock(self.root / "data/maintenance.lock"):
            with self.assertRaises(Exception):
                self.run_workflow()
        self.assert_no_data_stage()

    def test_duplicate_build_requests_cannot_overlap(self):
        started, finish = threading.Event(), threading.Event()
        failures = []
        report = self.synchronizer.return_value

        def synchronize(**kwargs):
            started.set()
            if not finish.wait(3):
                raise AssertionError("Test did not release the first workflow")
            return report

        def first_request():
            try:
                self.run_workflow()
            except Exception as error:
                failures.append(error)

        self.synchronizer.side_effect = synchronize
        worker = threading.Thread(target=first_request)
        worker.start()
        try:
            self.assertTrue(started.wait(3), "The first request never entered sync")
            with self.assertRaises(Exception):
                self.run_workflow()
            self.assertEqual(self.synchronizer.call_count, 1)
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.publisher.assert_called_once()

    def test_bad_software_versions_fail_before_loading_key_or_touching_archive(self):
        for value in ("1", "1.2.3", "-1.2", "1.2;echo", "x.y"):
            with self.subTest(version=value):
                with self.assertRaises(Exception):
                    self.run_workflow(software_version=value)
                self.loader.assert_not_called()
                self.assert_no_data_stage()

    def test_bad_full_versions_and_impossible_dates_fail_before_data_changes(self):
        for value in ("1.2", "1.2.260230", "1.2.261327", "1.2.260927+8", "../1.2.260927"):
            with self.subTest(version=value):
                with self.assertRaises(Exception):
                    self.run_workflow(version_name=value)
                self.loader.assert_not_called()
                self.assert_no_data_stage()

    def test_conflicting_full_and_software_versions_are_not_silently_chosen(self):
        with self.assertRaises(Exception):
            self.run_workflow(version_name="1.3.260927", software_version="1.2")
        self.loader.assert_not_called()
        self.assert_no_data_stage()

    def test_plain_and_encoded_key_never_appear_in_stage_logs_or_errors(self):
        encoded = base64.b64encode(self.key.encode()).decode()

        def synchronize(**kwargs):
            kwargs["callback"]("upstream diagnostic " + self.key)
            return {"status": "completed", "voice_failed": 0, "after": {"voice_states": {}}}

        def fail_release(*args, **kwargs):
            kwargs["emit"]("upstream encoded diagnostic " + encoded)
            raise RuntimeError("upstream exception " + self.key + " " + encoded)

        self.synchronizer.side_effect = synchronize
        self.publisher.side_effect = fail_release
        with self.assertRaises(Exception) as caught:
            self.run_workflow()
        exposed = "\n".join(self.lines) + str(caught.exception)
        self.assertNotIn(self.key, exposed)
        self.assertNotIn(encoded, exposed)
        self.assertNotIn(self.key, str(self.synchronizer.call_args))
        self.assertNotIn(self.key, str(self.publisher.call_args))


if __name__ == "__main__":
    unittest.main()
