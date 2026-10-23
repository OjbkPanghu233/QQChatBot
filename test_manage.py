"""管理命令不启动真实应用、不重建真实模型。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import manage


class ManagementTests(unittest.TestCase):
    def test_compose_keeps_original_project_and_explicit_file(self):
        with patch.object(manage, "executable", return_value="/docker"):
            command = manage.compose_command("up", "--detach")
        self.assertIn("napcat", command)
        self.assertEqual(command[command.index("--project-name") + 1], "napcat")
        self.assertEqual(command[command.index("--file") + 1], str(manage.NAPCAT))
        self.assertEqual(command[-2:], ["up", "--detach"])

    def test_layout_works_without_any_legacy_model_directory(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = home / "workspace/runtime/models"
            (store / "blobs").mkdir(parents=True)
            (store / "manifests").mkdir()
            modelfile = home / "model"
            modelfile.touch()
            compose = home / "compose"
            compose.touch()
            with patch.object(Path, "home", return_value=home), patch.object(manage, "MODEL_STORE", store), \
                    patch.object(manage, "MODELFILE", modelfile), patch.object(manage, "NAPCAT", compose):
                manage.check_layout()
                self.assertFalse((home / ".ollama/models").exists())

    def test_ollama_service_explicitly_sets_model_directory(self):
        import ollamactl
        service = ollamactl.service_config()
        self.assertEqual(service["EnvironmentVariables"]["OLLAMA_MODELS"], str(manage.MODEL_STORE))
        self.assertEqual(service["WorkingDirectory"], str(manage.ROOT))
        self.assertTrue(service["RunAtLoad"])
        self.assertEqual(service["ProgramArguments"][-1], "serve")

    def test_stop_with_docker_offline_stops_only_bot(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.json"
            config.write_text("{}")
            with patch.object(manage, "CONFIG", config), patch.object(manage, "bot") as bot, \
                    patch.object(manage, "docker_ready", return_value=False), patch.object(manage, "run") as command:
                manage.main(["stop"])
            bot.assert_called_once_with("stop")
            command.assert_not_called()

    def test_rebuild_uses_central_modelfile_and_configured_server(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.json"
            config.write_text(json.dumps({"model": "my-model:latest", "ollama_url": "http://127.0.0.1:11434"}))
            with patch.object(manage, "CONFIG", config), patch.object(manage, "check_layout"), \
                    patch.object(manage, "ollama_ready", return_value=True), \
                    patch.object(manage, "executable", return_value="/ollama"), \
                    patch.object(manage.subprocess, "run") as command:
                manage.main(["rebuild-model"])
            self.assertEqual(command.call_args.args[0], ["/ollama", "create", "my-model:latest", "--file", str(manage.MODELFILE)])
            self.assertEqual(command.call_args.kwargs["env"]["OLLAMA_HOST"], "http://127.0.0.1:11434")


if __name__ == "__main__":
    unittest.main()
