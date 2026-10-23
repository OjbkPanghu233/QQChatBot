"""网页管理台测试仅使用临时文件和模拟组件，不操作真实 QQ、模型或服务。"""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard import DashboardServer
import web_console as w
from test_qq_bot import BotFixture, event


CONFIG = {"model": "test:latest", "group_id": 100, "group_name": "test", "command_user_id": "20",
          "onebot_url": "ws://127.0.0.1:3001", "ollama_url": "http://127.0.0.1:11434", "think": True,
          "num_ctx": 8192, "num_predict": 2048, "onebot_token": "PRIVATE-TOKEN"}


class ConsoleFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".local").mkdir()
        (self.root / "models").mkdir()
        self.config = self.root / ".local/config.json"
        self.config.write_text(json.dumps(CONFIG))
        self.model = self.root / "models/deepseek.modelfile"
        self.model.write_text('FROM test:base\nSYSTEM "原人设"\n')
        self.profile = self.root / ".local/profiles.json"
        self.profile.write_text(json.dumps({"20": {"nick": "模拟用户", "pool": [[1, "1", "一条消息"]], "notes": []}}))
        for owner, field, value in ((w, "ROOT", self.root), (w, "CONFIG", self.config), (w.manage, "MODELFILE", self.model)):
            setting = patch.object(owner, field, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.console = w.Console()
        self.addCleanup(self.console.close)


class ConsoleTests(ConsoleFixture, unittest.TestCase):
    def test_stopped_bot_still_has_complete_console_state_and_saved_pools(self):
        state = self.console.snapshot()
        self.assertFalse(state["bot_running"])
        self.assertFalse(state["connected"])
        self.assertEqual(state["profiles"]["20"]["pool"][0][2], "一条消息")
        self.assertIn("ollama", state)
        self.assertIn("services", state)
        self.assertNotIn("onebot_token", state["config"])
        self.assertTrue(state["csrf_token"])

    def test_settings_never_echo_token_and_empty_field_preserves_it(self):
        result = self.console.api_get("/api/settings", {})
        self.assertNotIn("PRIVATE-TOKEN", json.dumps(result))
        self.assertTrue(result["onebot_token_set"])
        self.assertEqual(w.validate_config({"onebot_token": ""}, CONFIG)["onebot_token"], "PRIVATE-TOKEN")
        self.assertEqual(w.validate_config({"clear_onebot_token": True}, CONFIG)["onebot_token"], "")

    def test_invalid_or_unsupported_configuration_is_rejected(self):
        for value in ({"num_ctx": -1}, {"chime_probability": 2}, {"onebot_url": "file:///etc/passwd"},
                      {"dashboard_port": 1}, {"num_predict": True}, {"model": "x; rm"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                w.validate_config(value, CONFIG)

    def test_stop_is_component_operation_not_console_shutdown(self):
        self.console.operation = {"status": "queued"}
        with patch.object(self.console, "bot") as bot:
            self.console.execute("bot_stop", {})
        bot.assert_called_once_with("stop")
        self.assertEqual(self.console.snapshot()["operation"]["status"], "done")
        self.assertFalse(self.console.stop_event.is_set())
        self.assertTrue(self.console.api_get("/api/health", {})["ok"])

    def test_offline_memory_operations_preserve_other_profile_data(self):
        self.console.edit_offline_profiles("save_notes", {"uid": "20", "notes": ["喜欢聊天"]})
        self.console.edit_offline_profiles("clear_pool", {"uid": "20"})
        profile = json.loads(self.profile.read_text())["20"]
        self.assertEqual(profile["pool"], [])
        self.assertEqual(profile["notes"], ["喜欢聊天"])
        self.assertEqual(profile["nick"], "模拟用户")

    def test_model_test_streams_without_group_api(self):
        self.console.operation = {"status": "queued"}
        def generate(*args, on_chunk, **kwargs):
            self.assertIsNone(kwargs["prompt_path"])
            on_chunk({"message": {"thinking": "内部思考", "content": "你好"}})
            return "你好"
        with patch.object(self.console, "ensure_ollama"), patch.object(w, "chat", side_effect=generate), \
                patch.object(self.console, "bot_api") as group:
            self.console.execute("model_test", {"text": "本地测试"})
        group.assert_not_called()
        self.assertEqual(self.console.preview["thinking"], "内部思考")
        self.assertEqual(self.console.preview["answer"], "你好")

    def test_busy_and_unknown_actions_do_not_launch_extra_work(self):
        with self.assertRaises(ValueError):
            self.console.submit("arbitrary_command", {"command": "echo x"})
        self.console.operation = {"status": "running"}
        with self.assertRaisesRegex(ValueError, "尚未完成"):
            self.console.submit("bot_start", {})

    def test_configuration_saved_without_starting_stopped_bot(self):
        self.console.operation = {"status": "queued"}
        with patch.object(self.console, "bot") as bot:
            self.console.execute("config_save", {"config": {"num_ctx": 4096}})
        bot.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text())["num_ctx"], 4096)
        self.assertEqual(self.console.operation["status"], "done")

    def test_download_cannot_read_outside_export_directory(self):
        for name in ("../../.local/config.json", "../secret.txt", "/tmp/secret.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.console.api_get("/api/download", {"name": [name]})


class ControlHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_requires_same_origin_and_request_token(self):
        calls = []
        server = DashboardServer(asyncio.get_running_loop(), lambda: {"ok": True}, port=0,
                                 commands=lambda action, data: calls.append((action, data)) or {"ok": True}, csrf_token="CSRF-SECRET")
        def post(headers):
            req = Request(server.url + "/api/action", data=b'{"action":"bot_stop","data":{}}',
                          headers={"Content-Type": "application/json", **headers})
            with urlopen(req, timeout=3) as r:
                return json.load(r)
        try:
            for headers in ({}, {"X-CSRF-Token": "bad"}, {"X-CSRF-Token": "CSRF-SECRET", "Origin": "https://evil.example"}):
                with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                    await asyncio.to_thread(post, headers)
                self.assertEqual(error.exception.code, 403)
            result = await asyncio.to_thread(post, {"X-CSRF-Token": "CSRF-SECRET", "Origin": server.url})
            self.assertTrue(result["ok"])
            self.assertEqual(calls, [("bot_stop", {})])
        finally:
            await asyncio.to_thread(server.close)

    async def test_internal_state_requires_private_token(self):
        server = DashboardServer(asyncio.get_running_loop(), lambda: {"private": True}, port=0,
                                 auth_token="INTERNAL", serve_ui=False)
        def fetch(token=None):
            request = Request(server.url + "/api/state", headers={"Authorization": "Bearer " + token} if token else {})
            with urlopen(request, timeout=3) as response:
                return json.load(response)
        try:
            with self.assertRaises(HTTPError) as error:
                await asyncio.to_thread(fetch)
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(await asyncio.to_thread(fetch, "INTERNAL"), {"private": True})
        finally:
            await asyncio.to_thread(server.close)


class BotWebCommandTests(BotFixture, unittest.TestCase):
    def test_web_notes_and_clear_pool_update_live_state_and_persist(self):
        self.bot.on_event(event(1, "一条模拟消息"))
        self.bot.web_command("save_notes", {"uid": "20", "notes": ["测试备注"]})
        self.bot.web_command("clear_pool", {"uid": "20"})
        self.assertEqual(self.bot.profiles["20"]["notes"], ["测试备注"])
        self.assertEqual(self.bot.profiles["20"]["pool"], [])
        self.assertEqual(json.loads(self.profiles_path.read_text())["20"]["pool"], [])

    def test_web_chime_configuration_is_used(self):
        self.bot.config.update(chime_probability=0, chime_cooldown=0)
        self.assertIsNone(self.bot.decide("普通消息", False, None))
        self.bot.config["chime_probability"] = 1
        with patch.object(w.manage.random if hasattr(w.manage, "random") else __import__('qq_bot').random, "random", return_value=.5):
            self.assertEqual(self.bot.decide("普通消息", False, None), "chime")


if __name__ == "__main__":
    unittest.main()
