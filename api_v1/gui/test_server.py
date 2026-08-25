import unittest
from unittest.mock import Mock, patch

from gui.server import _terminate_windows_process_tree


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


if __name__ == "__main__":
    unittest.main()
