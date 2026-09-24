"""离线假模型测试；不导入 QwenPaw，不发送网络请求。"""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import router as core


def configured(mode="active"):
    config = core.default_config()
    config.update(enabled=True, mode=mode, enabled_agents=["pilot"])
    config["classifier"] = {"provider_id": "judge", "model": "fast"}
    config["tiers"] = {str(i): {"provider_id": f"p{i}", "model": f"tier:{i}"} for i in (1, 2, 3)}
    return config


def agent_config():
    return NS(id="pilot", backend="qwenpaw", active_model=NS(provider_id="original", model="default"),
              thinking_level="high", running=NS(llm_retry_enabled=True,
              light_context_config=NS(strategy="scroll", context_compact_config=NS(enabled=True))))


def context(text="2 task", session="session", history=None, **request_fields):
    request = NS(channel="console", request_context={}, **request_fields)
    return NS(request=request, session_id=session, agent_id="pilot", extras={}, agent_config=None,
              input_msgs=[{"role": "user", "content": text}], session_state=history)


class FakeHost:
    compatible = True
    version = "2.2.0b1"
    agentscope_version = "2.0.7"
    checks = {"fixture": True}

    def __init__(self):
        self.config = agent_config()
        self.calls = []
        self.delay = 0
        self.failure = None
        self.answer = None
        self.deleted = set()
        self.started = asyncio.Event()
        self.release = None

    async def agent_config(self, agent_id):
        if agent_id != "pilot":
            raise core.RouterError("agent_missing")
        return self.config

    def validate_agent(self, config):
        if not config.running.light_context_config.context_compact_config.enabled:
            raise core.RouterError("compression_disabled")

    def validate_slot(self, slot):
        if not slot["model"]:
            raise core.RouterError("models_required")
        if slot["model"] in self.deleted:
            raise core.RouterError("model_missing")

    async def validate_references(self, config):
        for a in config["enabled_agents"]:
            self.validate_agent(await self.agent_config(a))
        for slot in [config["classifier"], *config["tiers"].values()]:
            if slot["model"]:
                self.validate_slot(slot)

    async def catalog(self):
        return [], [{"id": "pilot", "name": "试点", "bypass_reason": None}]

    async def classify(self, slot, agent_id, payload, config):
        self.calls.append(copy.deepcopy((slot, agent_id, payload)))
        self.started.set()
        if self.release:
            await self.release.wait()
        await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        return self.answer or json.dumps({"level": int(payload["current"][0]), "reason_code": "standard"})


class ConfigurationTests(unittest.TestCase):
    def test_defaults_strict_config_and_output(self):
        self.assertFalse(core.validate_config(core.default_config())["enabled"])
        bad = [dict(configured(), extra="no"), dict(configured(), enabled=1),
               dict(configured(), enabled_agents=["pilot", "pilot"]),
               dict(configured(), classifier_timeout_seconds=float("nan")),
               dict(configured(), classifier_timeout_seconds=True),
               dict(configured(), schema_version=True), dict(configured(), mode="auto")]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(core.RouterError):
                core.validate_config(value)
        for text in ('```json\n{}\n```', '{}', '[]', '{"level":true,"reason_code":"simple"}',
                     '{"level":1.0,"reason_code":"simple"}', '{"level":4,"reason_code":"simple"}',
                     '{"level":1,"reason_code":"other"}', '{"level":1,"reason_code":[]}',
                     '{"level":1,"reason_code":"simple","model":"evil"}',
                     '{"level":1,"level":3,"reason_code":"complex"}',
                     '{"level":NaN,"reason_code":"simple"}', "x" * 1025):
            with self.subTest(text=text), self.assertRaises(core.RouterError):
                core.parse_grade(text)
        for level in (1, 2, 3):
            self.assertEqual(core.parse_grade(json.dumps({"level": level, "reason_code": "continuation"})), (level, "continuation"))
        self.assertEqual(core.parse_grade('{"level":1,"reason_code":"high_risk"}'), (1, "high_risk"))
        self.assertEqual(core.parse_grade('{"level":2,"reason_code":"insufficient_context"}')[0], 2)
        prompt = Path(__file__).with_name("prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(prompt.splitlines()[0], "固定提示词版本：" + core.PROMPT_VERSION)

    def test_atomic_save_failure_restart_and_unicode(self):
        with tempfile.TemporaryDirectory(prefix="模型路由-") as directory:
            store = core.ConfigStore(directory)
            self.assertFalse(store.read()["enabled"])
            old = configured()
            store.save(old)
            raw = store.path.read_bytes()
            new = configured("shadow")
            with patch.object(core.os, "replace", side_effect=OSError("disk")), self.assertRaises(OSError):
                store.save(new)
            self.assertEqual(store.path.read_bytes(), raw)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            self.assertEqual(core.ConfigStore(directory).read(), old)
            snapshot = store.read()
            snapshot["tiers"]["1"]["model"] = "mutated"
            self.assertEqual(store.read(), old)
            store.path.write_text("broken", encoding="utf-8")
            with self.assertRaises(core.RouterError):
                store.read()

    def test_context_bounds_and_no_hidden_content(self):
        history = []
        for i in range(3):
            history += [{"role": "user", "content": f"question-{i}"},
                        {"role": "assistant", "content": [{"type": "thinking", "text": "HIDDEN"},
                         {"type": "tool_use", "input": {"secret": "TOOL-ARG"}}, {"type": "text", "text": "reply" * 1000}]},
                        {"role": "tool", "content": [{"type": "tool_result", "content": "TOOL-OUTPUT"}]}]
        data = core.extract_context([{"role": "user", "content": "继续"}], {"state": {"summary": "摘" * 3000, "context": history}})
        serialized = json.dumps(data)
        for hidden in ("HIDDEN", "TOOL-ARG", "TOOL-OUTPUT", "question-0"):
            self.assertNotIn(hidden, serialized)
        self.assertLessEqual(sum(len(m["text"]) for m in data["history"]), 4000)
        self.assertEqual(len(data["summary"]), 2000)
        self.assertTrue(data["history_truncated"])
        legacy = {"memory": {"content": [[{"role": "user", "content": "上一问"}, []]], "_compressed_summary": "摘要"}}
        self.assertEqual(core.extract_context([{"role": "user", "content": "继续"}], legacy)["summary"], "摘要")
        with self.assertRaises(core.RouterError):
            core.extract_context([{"role": "user", "content": "你好"}], {"unknown": {}})

    def test_media_in_current_old_history_and_tool_result(self):
        for kind in ("image", "audio", "video", "file"):
            media = {"type": kind, "source": {"url": "SECRET-not-fetched"}}
            for content in ([media], [{"type": "tool_result", "content": [media]}],
                            [{"type": "tool_result", "output": [media]}]):
                state = {"state": {"context": [{"role": "assistant", "content": content}]}}
                data = core.extract_context([{"role": "user", "content": "hello"}], state)
                self.assertEqual(data["media"]["history"], [kind])
                self.assertNotIn("SECRET", json.dumps(data))
            data = core.extract_context([{"role": "user", "content": [media]}], None)
            self.assertEqual(data["media"]["current"], [kind])
            self.assertEqual(data["current"], "")
        block = {"type": "data", "source": {"type": "base64", "media_type": "image/png", "data": "SECRET"}}
        self.assertEqual(core.media_types(block), {"image"})
        self.assertEqual(core.media_types({"type": "data", "source": {"media_type": "application/pdf"}}), {"file"})
        self.assertEqual(core.media_types({"type": "tool_call", "input": "SECRET"}), set())
        with self.assertRaisesRegex(core.RouterError, "unsupported_content"):
            core.extract_context([{"role": "user", "content": [{"type": "unknown"}]}], None)
        nested = block
        for _ in range(18):
            nested = [nested]
        with self.assertRaisesRegex(core.RouterError, "unsupported_content"):
            core.media_types(nested)
        history = [{"role": "user", "content": [{"type": "unknown"}]},
                   {"role": "user", "content": "上一问"}, {"role": "user", "content": "最近一问"}]
        data = core.extract_context([{"role": "user", "content": "新任务"}], {"state": {"context": history}})
        self.assertEqual(data["media"]["history"], [])

    def test_native_agentscope_media_objects(self):
        try:
            from agentscope.message import Msg, DataBlock, TextBlock, URLSource, ToolCallBlock, ToolResultBlock
        except ImportError:
            self.skipTest("本机未安装 AgentScope；在测试容器运行此检查")
        media = DataBlock(source=URLSource(url="https://example.invalid/private.png", media_type="image/png"))
        user = Msg(name="user", role="user", content=[TextBlock(text="读出文字"), media])
        assistant = Msg(name="assistant", role="assistant", content=[
            ToolCallBlock(id="call1", name="view_image", input='{"private":"hidden"}'),
            ToolResultBlock(id="call1", name="view_image", output=[media])])
        data = core.extract_context([user], {"state": {"context": [assistant]}})
        self.assertEqual(data["current"], "读出文字")
        self.assertEqual(data["media"], {"current": ["image"], "history": ["image"]})
        self.assertNotIn("private", json.dumps(data))
        self.assertNotIn("hidden", json.dumps(data))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-test-")
        self.store = core.ConfigStore(self.temp.name)
        self.store.save(configured())
        self.host = FakeHost()
        self.records = core.RouteRecords(self.temp.name)
        self.service = core.RouterService(self.store, self.records, self.host)

    async def asyncTearDown(self):
        self.service.close()
        await asyncio.sleep(0)
        self.temp.cleanup()

    async def test_ten_interleaved_sessions_no_shared_mutation(self):
        self.host.delay = 0.015
        before = copy.deepcopy(vars(self.host.config))
        config_bytes = self.store.path.read_bytes()
        contexts = [context(f"{i % 3 + 1} task-{i}", f"private-session-{i}",
                            {"state": {"context": [{"role": "user", "content": f"history-{i}"}]}}) for i in range(10)]
        original_inputs = copy.deepcopy([c.input_msgs for c in contexts])
        original_states = copy.deepcopy([c.session_state for c in contexts])
        await asyncio.gather(*(self.service.route(c) for c in contexts))
        for i, ctx in enumerate(contexts):
            self.assertEqual(ctx.request.model_slot_override, configured()["tiers"][str(i % 3 + 1)])
            # 重复 Hook/工具循环不再次判级；业务执行不在插件职责内。
            for _ in range(3):
                await self.service.route(ctx)
        self.assertEqual(len(self.host.calls), 10)
        for _, _, payload in self.host.calls:
            i = payload["current"].split("-")[-1]
            self.assertEqual(payload["history"], [{"role": "user", "text": f"history-{i}"}])
        self.assertEqual([c.input_msgs for c in contexts], original_inputs)
        self.assertEqual([c.session_state for c in contexts], original_states)
        self.assertEqual(vars(self.host.config), before)
        self.assertEqual(self.store.path.read_bytes(), config_bytes)
        logs = (Path(self.temp.name) / "routing.log").read_text(encoding="utf-8")
        self.assertNotIn("private-session-", logs)
        self.assertNotIn("history-", logs)
        self.assertNotIn("task-", logs)
        self.assertEqual(len({r["session"] for r in self.records.recent()}), 10)

    async def test_snapshot_shadow_and_next_turn_disable(self):
        self.host.release = asyncio.Event()
        ctx = context()
        task = asyncio.create_task(self.service.route(ctx))
        await self.host.started.wait()
        new = configured("shadow")
        self.store.save(new)
        self.host.release.set()
        await task
        self.assertEqual(ctx.extras[core.STATE_KEY]["outcome"], "selected")
        shadow = context()
        await self.service.route(shadow)
        self.assertFalse(hasattr(shadow.request, "model_slot_override"))
        self.assertEqual(shadow.extras[core.STATE_KEY]["outcome"], "shadow")
        new["enabled"] = False
        self.store.save(new)
        result = await self.service.route(context())
        self.assertEqual(result["reason_code"], "disabled")
        self.assertEqual(len(self.host.calls), 2)

    async def test_all_bypass_sources_and_overrides(self):
        cases = [("request_context", {"source": s}) for s in ("cron", "heartbeat", "mail_monitor", "unknown")]
        cases += [("session_source", "cron"), ("request_context", {"_spawn_subagent": True}),
                  ("_spawn_subagent", True), ("model_slot_override", {}),
                  ("request_context", {"model_slot_override": {"provider_id": "manual", "model": "fixed"}}),
                  ("channel", "email"), ("source", "background")]
        for field, value in cases:
            ctx = context()
            setattr(ctx.request, field, value)
            snapshot = copy.deepcopy(vars(ctx.request))
            result = await self.service.route(ctx)
            self.assertEqual(result["outcome"], "bypass")
            self.assertEqual(vars(ctx.request), snapshot)
        other = context()
        other.agent_id = "other"
        self.assertEqual((await self.service.route(other))["reason_code"], "agent_not_enabled")
        self.assertEqual((await self.service.route(context("/stop")))["reason_code"], "control_command")
        self.assertEqual(self.host.calls, [])

    async def test_failure_missing_target_and_compression(self):
        self.host.answer = "not-json"
        self.assertEqual((await self.service.route(context()))["outcome"], "bypass")
        self.host.answer = None
        self.host.deleted.add("tier:2")
        self.assertEqual((await self.service.route(context()))["reason_code"], "model_missing")
        self.host.deleted.clear()
        self.host.failure = RuntimeError("SECRET-provider-token")
        self.assertEqual((await self.service.route(context()))["reason_code"], "classifier_or_host_error")
        self.host.config.running.light_context_config.context_compact_config.enabled = False
        self.assertEqual((await self.service.route(context()))["reason_code"], "compression_disabled")
        self.assertEqual(len(self.host.calls), 3)
        self.assertNotIn("SECRET", json.dumps(self.records.recent()))

    async def test_long_input_is_classified_without_forced_l3(self):
        text = "1" + "需" * 9000 + "结束"
        ctx = context(text)
        result = await self.service.route(ctx)
        self.assertEqual(result["level"], 1)
        self.assertEqual(ctx.input_msgs[0]["content"], text)
        payload = self.host.calls[0][2]
        self.assertEqual(len(payload["current"]), 8000)
        self.assertTrue(payload["current_truncated"])
        self.assertTrue(payload["current"].startswith("1"))
        self.assertTrue(payload["current"].endswith("结束"))
        self.host.answer = '{"level":3,"reason_code":"complex"}'
        self.assertEqual((await self.service.route(context(text)))["level"], 3)
        self.host.deleted.add("tier:3")
        self.assertEqual((await self.service.route(context(text)))["reason_code"], "model_missing")

    async def test_media_routes_once_without_mutating_execution_input(self):
        for level in (1, 2, 3):
            self.host.answer = json.dumps({"level": level, "reason_code": "standard"})
            ctx = context()
            ctx.input_msgs[0]["content"] = [
                {"type": "text", "text": "按文字要求处理附件"},
                {"type": "data", "source": {"media_type": "image/png", "url": "SECRET-media-url"}}]
            before = copy.deepcopy(ctx.input_msgs)
            result = await self.service.route(ctx)
            self.assertEqual(result["level"], level)
            self.assertEqual(result["outcome"], "selected")
            self.assertEqual(ctx.request.model_slot_override, configured()["tiers"][str(level)])
            self.assertEqual(ctx.input_msgs, before)
            await self.service.route(ctx)
        self.assertEqual(len(self.host.calls), 3)
        self.assertNotIn("SECRET", json.dumps(self.host.calls))
        self.assertNotIn("SECRET", json.dumps(self.records.recent()))
        self.host.answer = '{"level":2,"reason_code":"insufficient_context"}'
        ctx = context()
        ctx.input_msgs[0]["content"] = [{"type": "image"}]
        self.assertEqual((await self.service.route(ctx))["level"], 2)

    async def test_cancel_propagates_without_default_or_late_override(self):
        self.host.release = asyncio.Event()
        ctx = context()
        task = asyncio.create_task(self.service.route(ctx))
        await self.host.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.host.release.set()
        await asyncio.sleep(0)
        self.assertFalse(hasattr(ctx.request, "model_slot_override"))
        self.assertEqual(ctx.extras[core.STATE_KEY]["outcome"], "cancelled")
        self.assertEqual(len(self.host.calls), 1)

    async def test_timeout_covers_build_and_ignores_suppressed_cancel(self):
        config = configured()
        config["classifier_timeout_seconds"] = 1
        self.store.save(config)
        release = asyncio.Event()
        async def stubborn(*_):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await release.wait()
            return '{"level":3,"reason_code":"complex"}'
        self.host.classify = stubborn
        ctx = context()
        started = time.monotonic()
        result = await self.service.route(ctx)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result["reason_code"], "classifier_timeout")
        release.set()
        await asyncio.sleep(0.02)
        self.assertFalse(hasattr(ctx.request, "model_slot_override"))
        self.assertEqual(result["reason_code"], "classifier_timeout")
        self.assertEqual(len(self.service.pending), 0)

    async def test_test_endpoint_concurrency_and_unload(self):
        self.host.release = asyncio.Event()
        tasks = [asyncio.create_task(self.service.test("pilot", "2 task")) for _ in range(2)]
        await self.host.started.wait()
        with self.assertRaisesRegex(core.RouterError, "test_busy"):
            await self.service.test("pilot", "2 task")
        self.host.release.set()
        results = await asyncio.gather(*tasks)
        self.assertTrue(all(r["outcome"] == "test_only" for r in results))
        self.assertEqual(self.records.recent(), [])
        self.service.close()
        self.assertEqual((await self.service.route(context()))["reason_code"], "plugin_unloaded")

    async def test_deleted_classifier_and_incompatible_host_no_calls(self):
        self.host.deleted.add("fast")
        self.assertEqual((await self.service.route(context()))["reason_code"], "model_missing")
        self.host.compatible = False
        self.assertEqual((await self.service.route(context()))["reason_code"], "incompatible_host")
        self.assertEqual(self.host.calls, [])


if __name__ == "__main__":
    unittest.main()
