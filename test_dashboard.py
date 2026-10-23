"""验证流式推理、任务状态和本机仪表盘；不连接 QQ 或 Ollama。"""
import asyncio
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard import DashboardServer
from ollama_client import chat
import qq_bot as m
from test_qq_bot import BotFixture, event


class StreamTests(unittest.TestCase):
    def response(self, parts):
        return BytesIO(b"\n".join(json.dumps(p).encode() for p in parts) + b"\n")

    def test_thinking_arrives_before_final_response_and_is_not_returned_as_answer(self):
        chunks = []
        stream = self.response([{"message": {"thinking": "思考一"}}, {"message": {"thinking": "思考二"}},
                                {"message": {"content": "最终"}}, {"message": {"content": "回复"}},
                                {"done": True, "eval_count": 12}])
        with patch("ollama_client.request.urlopen", return_value=stream) as request:
            answer = chat("你好", prompt_path=None, think=True, num_ctx=8192, on_chunk=chunks.append)
        self.assertEqual(answer, "最终回复")
        self.assertEqual(chunks[0]["message"]["thinking"], "思考一")
        self.assertTrue(chunks[-1]["done"])
        payload = json.loads(request.call_args.args[0].data)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["options"]["num_ctx"], 8192)

    def test_interrupted_stream_and_server_error_do_not_send_partial_answer(self):
        for parts in ([{"message": {"content": "半句"}}], [{"error": "model unavailable"}]):
            with self.subTest(parts=parts), patch("ollama_client.request.urlopen", return_value=self.response(parts)):
                with self.assertRaises(RuntimeError):
                    chat("你好", prompt_path=None, on_chunk=lambda part: None)

    def test_stream_has_total_deadline_even_when_chunks_keep_arriving(self):
        stream = self.response([{"message": {"thinking": "继续"}}, {"done": True}])
        with patch("ollama_client.request.urlopen", return_value=stream), \
                patch("ollama_client.time.monotonic", side_effect=[0, 301]):
            with self.assertRaisesRegex(RuntimeError, "生成超时"):
                chat("你好", prompt_path=None, timeout=300, on_chunk=lambda part: None)


class DashboardTests(BotFixture, unittest.IsolatedAsyncioTestCase):
    async def test_live_phases_and_confirmed_send_are_observable(self):
        phases = []
        def generate(*args, on_chunk, **kwargs):
            for part in ({"message": {"thinking": "先想一下"}}, {"message": {"content": "你好啊"}},
                         {"done": True, "eval_count": 20}):
                on_chunk(part)
                phases.append(self.bot.telemetry.snapshot()["traces"][0]["stage"])
            return "你好啊"
        self.bot.on_event(event(1, "鲸鱼，打个招呼", at=True))
        job = self.bot.queue.get_nowait()
        with patch.object(m, "chat", side_effect=generate):
            await self.bot.handle(job)
        trace = self.bot.dashboard_state()["traces"][0]
        self.assertEqual(phases[:2], ["thinking", "generating"])
        self.assertEqual(trace["thinking"], "先想一下")
        self.assertEqual(trace["answer"], "你好啊")
        self.assertEqual(trace["status"], "sent")
        self.assertEqual(trace["metrics"]["eval_count"], 20)
        self.assertTrue(trace["message_id"])
        self.assertEqual(self.bot.api.sent[0][1]["message"][0]["data"]["text"], "你好啊")
        self.bot.queue.task_done()

    async def test_skip_and_reset_ignore_late_thinking_chunks(self):
        self.bot.on_event(event(1, "鲸鱼", at=True))
        job = self.bot.queue.get_nowait()
        job["mode"] = "chime"
        with patch.object(m, "chat", return_value="[SKIP]"):
            await self.bot.handle(job)
        self.bot.telemetry.chunk(job, {"message": {"thinking": "迟来的思考"}}, 1)
        trace = self.bot.dashboard_state()["traces"][0]
        self.assertEqual(trace["status"], "skipped")
        self.assertEqual(trace["thinking"], "")
        self.assertEqual(self.bot.api.sent, [])
        self.bot.clear_context()
        self.bot.telemetry.chunk(job, {"message": {"content": "旧任务"}}, 1)
        self.assertEqual(self.bot.dashboard_state()["traces"], [])
        self.assertEqual(self.bot.profiles["20"]["pool"], [])
        self.bot.queue.task_done()

    async def test_http_returns_live_pool_but_never_tokens_and_rejects_foreign_origin(self):
        self.bot.config["onebot_token"] = "SECRET-TEST-TOKEN"
        self.bot.on_event(event(1, "刚刚写入、尚未落盘的消息"))
        server = DashboardServer(asyncio.get_running_loop(), self.bot.dashboard_state, port=0)
        try:
            def fetch(path, headers=None):
                with urlopen(Request(server.url + path, headers=headers or {}), timeout=3) as response:
                    return response.status, response.read()
            status, body = await asyncio.to_thread(fetch, "/api/state")
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertEqual(data["profiles"]["20"]["pool"][0][2], "刚刚写入、尚未落盘的消息")
            self.assertNotIn(b"SECRET-TEST-TOKEN", body)
            self.assertNotIn("onebot_token", data["config"])
            for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
                with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                    await asyncio.to_thread(fetch, "/api/state", headers)
                self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                await asyncio.to_thread(fetch, "/../.local/config.json")
            self.assertEqual(error.exception.code, 404)
            for path in ("/", "/app.js", "/style.css"):
                self.assertEqual((await asyncio.to_thread(fetch, path))[0], 200)
        finally:
            await asyncio.to_thread(server.close)


if __name__ == "__main__":
    unittest.main()
