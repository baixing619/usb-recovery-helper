"""UI logic checks without creating a Tk window or accessing devices."""

import math
import unittest

from usb_recovery.ui import (
    UiState,
    count_labels,
    device_label,
    format_bytes,
    partition_label,
    reduce_event,
    summary_text,
)


class UiHelpersTests(unittest.TestCase):
    def test_unknown_sizes_are_not_shown_as_zero(self):
        for value in (None, -1, "bad", math.nan, math.inf):
            self.assertEqual(format_bytes(value), "未知")
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1024), "1.0 KiB")
        self.assertEqual(format_bytes(32 * 1024 ** 3), "32.0 GiB")

    def test_device_label_keeps_drive_capacity_and_disk_identity(self):
        text = device_label({"label": "资料", "model": "USB Storage",
                             "size": 32 * 1024 ** 3, "disk_number": 4,
                             "drive_letter": "R:"})
        for detail in ("资料", "USB Storage", "32.0 GiB", "4", "R:"):
            self.assertIn(detail, text)

    def test_partition_label_shows_offset_and_unconfirmed_warnings(self):
        text = partition_label({"filesystem": "exFAT", "offset": 1048576,
                                "size": 1024, "label": "资料", "warnings": ["缺块"]})
        self.assertIn("exFAT", text)
        self.assertIn("1048576", text)
        self.assertIn("缺块", text)

    def test_progress_at_one_hundred_does_not_mean_complete(self):
        state = reduce_event(UiState(running=True),
                             {"type": "progress", "stage": "recovering", "progress": 100})
        self.assertTrue(state.running)
        self.assertIsNone(state.complete)
        self.assertNotIn("完整恢复", summary_text(state))

    def test_partial_result_remains_partial_even_with_success_message(self):
        state = reduce_event(UiState(running=True, progress=98),
                             {"type": "result", "stage": "completed", "complete": False,
                              "progress": 100, "message": "扫描完成", "job_dir": "output"})
        self.assertFalse(state.running)
        self.assertFalse(state.complete)
        self.assertIn("部分", summary_text(state))
        self.assertNotIn("完整", summary_text(state))

    def test_missing_completeness_is_unknown_after_result(self):
        state = reduce_event(UiState(running=True),
                             {"type": "result", "message": "处理完成", "counts": {}})
        self.assertIsNone(state.complete)
        self.assertIn("核对", summary_text(state))

    def test_cancel_and_error_preserve_existing_results(self):
        state = UiState(running=True, job_dir="keep", report_path="keep/report.html")
        cancelled = reduce_event(state, {"type": "state", "stage": "cancelled", "can_resume": True})
        self.assertFalse(cancelled.running)
        self.assertFalse(cancelled.complete)
        self.assertEqual(cancelled.job_dir, "keep")
        self.assertTrue(cancelled.can_resume)
        failed = reduce_event(state, {"type": "error", "message": "权限不足"})
        self.assertFalse(failed.running)
        self.assertFalse(failed.complete)
        self.assertEqual(failed.report_path, "keep/report.html")
        self.assertIn("错误", summary_text(failed))

    def test_history_does_not_become_structure_checked(self):
        labels = dict(count_labels({"structure_checked": 2, "repaired_candidate": 3,
                                   "partial": 4, "unverified": 5, "historical": 6,
                                   "temporary": 1}))
        self.assertEqual(labels["结构检查通过"], 2)
        self.assertEqual(labels["修复候选"], 3)
        self.assertEqual(labels["残缺或未确认"], 9)
        self.assertEqual(labels["历史候选"], 6)
        self.assertEqual(labels["临时或系统文件"], 1)

    def test_bad_progress_and_negative_counts_do_not_mislead(self):
        state = reduce_event(UiState(), {"type": "progress", "progress": math.nan,
                                       "counts": {"partial": -3, "historical": "bad"}})
        self.assertIsNone(state.progress)
        self.assertEqual(dict(count_labels(state.counts))["历史候选"], 0)
        self.assertEqual(dict(count_labels(state.counts))["残缺或未确认"], 0)

    def test_image_result_retains_map_for_user_selected_recovery(self):
        state = reduce_event(UiState(running=True),
                             {"type": "result", "can_recover": True,
                              "image_path": "task/source.img", "map_path": "task/source.map"})
        self.assertTrue(state.can_recover)
        self.assertEqual(state.image_path, "task/source.img")
        self.assertEqual(state.map_path, "task/source.map")

    def test_late_progress_does_not_hide_pending_stop(self):
        state = UiState(running=True, stage="cancelling", message="等待安全停止")
        state = reduce_event(state, {"type": "progress", "stage": "imaging", "progress": 17})
        self.assertEqual(state.stage, "cancelling")
        self.assertTrue(state.running)
        self.assertIn("停止", summary_text(state))
        ended = reduce_event(state, {"type": "result", "stage": "cancelled", "complete": False})
        self.assertFalse(ended.running)
        self.assertEqual(ended.stage, "cancelled")


if __name__ == "__main__":
    unittest.main()
