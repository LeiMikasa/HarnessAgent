"""Check native chooser process boundaries without opening windows on the user's desktop."""

import io
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import HarnessCase
from agent.web.folder_picker import PICKER_SCRIPT, pick_directory


class FolderPickerTests(HarnessCase):
    def test_selected_unicode_path_and_cancel_are_decoded(self):
        nested = self.tmp / "两层目录" / "项目"
        nested.mkdir(parents=True)
        responses = [subprocess.CompletedProcess([], 0, json.dumps({"path": str(nested)}), ""),
                     subprocess.CompletedProcess([], 0, '{"path":null}', "")]
        with patch("agent.web.folder_picker.subprocess.run", side_effect=responses) as run:
            self.assertEqual(pick_directory(str(self.tmp)), str(nested))
            args, options = run.call_args
            self.assertEqual(args[0][1:3], ["-I", "-c"])
            self.assertEqual(json.loads(options["input"]), {"initial_path": str(self.tmp.resolve())})
            self.assertNotIn("shell", options)
            self.assertEqual(options["timeout"], 180)
            self.assertIsNone(pick_directory(str(self.tmp)))

    def test_missing_desktop_invalid_output_and_timeout_offer_manual_input(self):
        for result in (subprocess.CompletedProcess([], 1, "", "Tk unavailable"),
                       subprocess.CompletedProcess([], 0, "invalid", "")):
            with patch("agent.web.folder_picker.subprocess.run", return_value=result):
                with self.assertRaises(RuntimeError):
                    pick_directory(str(self.tmp))
        with patch("agent.web.folder_picker.subprocess.run", side_effect=subprocess.TimeoutExpired("picker", 180)):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                pick_directory(str(self.tmp))

    def test_native_script_requests_existing_directories_and_destroys_root(self):
        root = Mock()
        ask = Mock(return_value=str(self.tmp))
        fake_tk = SimpleNamespace(Tk=Mock(return_value=root), filedialog=SimpleNamespace(askdirectory=ask))
        output = io.StringIO()
        with patch.dict("sys.modules", {"tkinter": fake_tk}), \
             patch("sys.stdin", io.StringIO(json.dumps({"initial_path": str(self.tmp)}))), \
             patch("sys.stdout", output):
            exec(compile(PICKER_SCRIPT, "<native-folder-picker>", "exec"), {})
        root.withdraw.assert_called_once()
        root.destroy.assert_called_once()
        self.assertTrue(ask.call_args.kwargs["mustexist"])
        self.assertEqual(ask.call_args.kwargs["parent"], root)
        self.assertEqual(json.loads(output.getvalue()), {"path": str(self.tmp)})
