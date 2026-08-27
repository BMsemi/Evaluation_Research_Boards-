import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gui.server import _sweep_resume_info, _terminate_windows_process_tree


class WindowsProcessTreeTerminationTests(unittest.TestCase):
    @patch("gui.server.subprocess.run")
    def test_taskkill_terminates_parent_and_children(self, run: Mock) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "SUCCESS"

        _terminate_windows_process_tree(1234)

        run.assert_called_once_with(
            ["taskkill", "/PID", "1234", "/T", "/F"],
            text=True,
            stdout=-1,
            stderr=-2,
        )

    @patch("gui.server.subprocess.run")
    def test_taskkill_failure_is_reported(self, run: Mock) -> None:
        run.return_value.returncode = 128
        run.return_value.stdout = "process not found"

        with self.assertRaisesRegex(OSError, "process not found"):
            _terminate_windows_process_tree(1234)


class SweepResumeTests(unittest.TestCase):
    @staticmethod
    def reset_rows(count: int) -> list[dict[str, object]]:
        rails = [(vcc_set, vcc_wl) for vcc_set in (3.3, 3.4, 3.5, 3.6, 3.7) for vcc_wl in (1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.3)]
        return [
            {
                "operation": "reset",
                "ok": True,
                "cellAddress": {"row": 18, "col": 0},
                "vcc_set_V": vcc_set,
                "vcc_wl_set_V": vcc_wl,
            }
            for vcc_set, vcc_wl in rails[:count]
        ]

    def test_completed_reset_sweep_is_not_resumable(self) -> None:
        info = _sweep_resume_info(Path("gui_test_r18c00_reset"), self.reset_rows(40))

        self.assertFalse(info["canResume"])
        self.assertEqual(info["completedPulses"], 40)
        self.assertEqual(info["remainingPulses"], 0)

    def test_interrupted_reset_sweep_remains_resumable(self) -> None:
        info = _sweep_resume_info(Path("gui_test_r18c00_reset"), self.reset_rows(17))

        self.assertTrue(info["canResume"])
        self.assertEqual(info["completedPulses"], 17)
        self.assertEqual(info["remainingPulses"], 23)


if __name__ == "__main__":
    unittest.main()
