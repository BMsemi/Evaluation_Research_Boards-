import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cell_api import CommandRunner, RailVoltages, ScanDebugCellAPI, ScanDebugConfig


class CommandRunnerPasswordSshTests(unittest.TestCase):
    def test_windows_password_ssh_uses_paramiko_and_combines_output(self) -> None:
        channel = Mock()
        channel.makefile.return_value.read.return_value = b"remote output\n"
        channel.recv_exit_status.return_value = 7
        transport = Mock()
        transport.is_active.return_value = True
        transport.open_session.return_value = channel
        client = Mock()
        client.get_transport.return_value = transport

        with patch("paramiko.SSHClient", return_value=client):
            result = CommandRunner._ssh_with_paramiko_password(
                "user@example.test",
                "secret",
                "hostname",
                timeout_s=30,
            )

        self.assertEqual(result.args, ["ssh", "user@example.test", "hostname"])
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "remote output\n")
        self.assertEqual(result.stderr, "")
        client.connect.assert_called_once_with(
            hostname="example.test",
            username="user",
            password="secret",
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        channel.set_combine_stderr.assert_called_once_with(True)
        channel.exec_command.assert_called_once_with("hostname")
        channel.close.assert_called_once_with()
        client.close.assert_called_once_with()

    def test_windows_password_ssh_requires_user_at_hostname(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "user@hostname"):
            CommandRunner._ssh_with_paramiko_password("example.test", "secret", "hostname")


class CaptureCopyTests(unittest.TestCase):
    def test_remote_copy_falls_back_to_scp_when_rsync_is_not_on_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(
                ScanDebugConfig(
                    run_dir=Path(temp_dir),
                    saleae_host="user@example.test",
                    dry_run=True,
                )
            )
            api.runner = Mock()
            api.runner.run.return_value.returncode = 0
            api.runner.run.return_value.stdout = ""
            with patch("cell_api.shutil.which", side_effect=lambda name: None if name == "rsync" else "scp.exe"):
                local = api._copy_capture("/remote/capture", 3, "read", RailVoltages(1.0, 2.5))

            api.runner.run.assert_called_once_with(
                ["scp.exe", "-r", "user@example.test:/remote/capture/.", str(local)]
            )


if __name__ == "__main__":
    unittest.main()
