"""离线加载少量原生源码验证契约；完整宿主/容器/模型调用仍需另行验收。"""
from __future__ import annotations

import ast
import asyncio
from collections import Counter
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
import httpx
import router as core
from test_router import FakeHost, agent_config, configured, context

ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("QWENPAW_SOURCE", ROOT / "qwenpaw-source"))
SRC = SOURCE / "src" / "qwenpaw"
FILES = ["plugin.json", "plugin.py", "router.py", "prompt.txt", "ui/index.js",
         "test_router.py", "test_host_contract.py", "evaluation.jsonl", "README.md", "README.zh-CN.md"]


def load_module(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.modules["_router_test_plugin.router"] = core
entry = load_module("_router_test_plugin", ROOT / "plugin.py", package=True)


def package_stub(name, **attributes):
    module = ModuleType(name)
    module.__path__ = []
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


def load_native_hooks():
    for name in ("_router_host", "_router_host.runtime"):
        package_stub(name)
    package_stub("_router_host.exceptions", HookCycleError=RuntimeError)
    phases = load_module("_router_host.runtime.phases", SRC / "runtime" / "phases.py")
    hooks = load_module("_router_host.runtime.hooks", SRC / "runtime" / "hooks.py")
    return phases, hooks


def sample_payload(sample):
    text = sample["current"] * sample.get("repeat", 1) + sample.get("suffix", "")
    state = {"state": {"summary": sample.get("summary", ""), "context": sample.get("history", [])}}
    return core.extract_context([{"role": "user", "content": text}], state)


def score_predictions(samples, rows):
    expected = {s["id"]: s for s in samples}
    predicted = {}
    for row in rows:
        if row["id"] not in expected or row["id"] in predicted:
            raise ValueError("预测 ID 未知或重复")
        if row.get("level") is not None and (type(row["level"]) is not int or row["level"] not in (1, 2, 3)):
            raise ValueError("预测等级无效")
        predicted[row["id"]] = row
    groups = {}
    for split in ("all", "calibration", "holdout"):
        subset = [s for s in samples if split == "all" or s["split"] == split]
        valid = [predicted[s["id"]] for s in subset if predicted.get(s["id"], {}).get("level") in (1, 2, 3)]
        matched = sum(predicted.get(s["id"], {}).get("level") == s["level"] for s in subset)
        lowered = sum(s["level"] == 3 and predicted.get(s["id"], {}).get("level") == 1 for s in subset)
        groups[split] = {"samples": len(subset), "valid": len(valid), "accuracy": matched / len(subset), "l3_to_l1": lowered}
    elapsed = [r["elapsed_ms"] for r in rows if type(r.get("elapsed_ms")) in (int, float) and math.isfinite(r["elapsed_ms"]) and r["elapsed_ms"] >= 0]
    elapsed.sort()
    percentile = lambda p: elapsed[max(0, math.ceil(len(elapsed) * p) - 1)] if elapsed else None
    def tokens(key):
        values = [r[key] for r in rows if type(r.get(key)) is int and r[key] >= 0]
        return {"known_samples": len(values), "sum": sum(values) if values else None}
    return {"groups": groups, "prediction_coverage": len(rows) / len(samples),
            "p50_ms": percentile(.5), "p95_ms": percentile(.95),
            "timeout_rate": sum(r.get("reason_code") == "classifier_timeout" for r in rows) / len(rows) if rows else None,
            "tier_counts": dict(Counter(str(r.get("level")) for r in rows)),
            "input_tokens": tokens("input_tokens"), "output_tokens": tokens("output_tokens"),
            "cost": None, "note": "仅统计导入结果；业务质量、真实价格与基线对照未由此证明。"}


class AdapterAndApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-api-")
        self.host = FakeHost()
        self.store = core.ConfigStore(self.temp.name)
        self.store.save(configured())
        self.service = core.RouterService(self.store, core.RouteRecords(self.temp.name), self.host)
        self.app = FastAPI()
        self.app.include_router(entry.create_router(self.service), prefix="/api/model-router")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://offline.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.service.close()
        await asyncio.sleep(0)
        self.temp.cleanup()

    async def test_api_config_test_limits_and_disable_missing_models(self):
        response = await self.client.get("/api/model-router/config")
        self.assertEqual(response.json(), configured())
        response = await self.client.post("/api/model-router/classify", json={"agent_id": "pilot", "text": "2 task"})
        self.assertEqual(response.json()["outcome"], "test_only")
        self.assertEqual(self.service.records.recent(), [])
        for body in ({"agent_id": "pilot", "text": "2 task", "api_key": "SECRET"},
                     {"agent_id": "pilot", "text": "a" * 16001}):
            response = await self.client.post("/api/model-router/classify", json=body)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("SECRET", response.text)
        response = await self.client.put("/api/model-router/config", content=b"x" * 65537)
        self.assertEqual(response.json(), {"error": "body_too_large"})
        response = await self.client.get("/api/model-router/recent?limit=101")
        self.assertEqual(response.status_code, 400)
        self.host.deleted.add("tier:1")
        config = configured()
        config["enabled"] = False
        response = await self.client.put("/api/model-router/config", json=config)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.store.read()["enabled"])

    async def test_api_write_failure_and_exception_redaction(self):
        before = self.store.path.read_bytes()
        with patch.object(self.store, "save", side_effect=OSError("SECRET-disk-location")):
            response = await self.client.put("/api/model-router/config", json=configured())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.text)
        self.assertEqual(before, self.store.path.read_bytes())
        self.host.failure = RuntimeError("SECRET-token-in-error")
        response = await self.client.post("/api/model-router/classify", json={"agent_id": "pilot", "text": "2 task"})
        self.assertEqual(response.json(), {"error": "classifier_or_host_error"})
        response = await self.client.get("/api/model-router/status")
        self.assertNotIn("SECRET", response.text)

    async def test_native_adapter_private_config_and_native_stream_consumer(self):
        host = entry.NativeHost.__new__(entry.NativeHost)
        config = agent_config()
        original = copy.deepcopy(config)
        observed = {}
        async def model(messages, **kwargs):
            observed.update(messages=messages, kwargs=kwargs)
            async def chunks():
                yield {"content": [{"type": "thinking", "text": "SECRET-reasoning"}]}
                yield {"content": [{"type": "text", "text": '{"level":'}]}
                yield {"content": [{"type": "text", "text": '{"level":2,"reason_code":"standard"}'}]}
            return chunks()
        async def factory(**kwargs):
            observed["factory"] = kwargs
            return model, object()
        host.factory = factory
        if not (SRC / "utils" / "model_response.py").is_file():
            self.skipTest("缺少附带宿主源码")
        consumer = load_module("_native_response_consumer", SRC / "utils" / "model_response.py")
        host.consume = consumer.consume_model_response
        host.Msg = lambda **kwargs: kwargs
        host.TextBlock = lambda **kwargs: kwargs
        result = await host.classify(configured()["classifier"], "pilot", {"current": "payload-only"}, config)
        self.assertEqual(core.parse_grade(result)[0], 2)
        self.assertEqual(config, original)
        self.assertIsNot(observed["factory"]["agent_config"], config)
        self.assertFalse(observed["factory"]["agent_config"].running.llm_retry_enabled)
        self.assertEqual(observed["factory"]["agent_config"].thinking_level, "off")
        self.assertEqual(observed["kwargs"], {"max_tokens": 128, "disable_thinking": True})
        self.assertEqual([m["role"] for m in observed["messages"]], ["system", "user"])
        self.assertNotIn("payload-only", observed["messages"][0]["content"][0]["text"])
        self.assertNotIn("SECRET", result)

    async def test_native_catalog_secret_projection_and_model_validation(self):
        host = entry.NativeHost.__new__(entry.NativeHost)
        info = NS(id="existing", name="模型", availability_status="unverified")
        provider_info = NS(id="p", name="供应商", models=[info], extra_models=[], hidden_model_ids=[],
                           discovered_models=[NS(id="not-configured")], api_key="SECRET", custom_headers={"Authorization": "SECRET"})
        native = NS(get_context_size=lambda _: 8192, get_model_info=lambda model: info if model == "existing" else None)
        async def listing():
            return [provider_info]
        manager = NS(list_provider_info=listing, get_provider=lambda _: native, get_active_model=lambda: None)
        host.manager = lambda: manager
        host.load_config = lambda: NS(agents=NS(profiles={"pilot": {}}))
        host.load_agent = lambda _: agent_config()
        async def io(fn, *args):
            return fn(*args)
        host.io = io
        models, agents = await host.catalog()
        self.assertNotIn("SECRET", json.dumps([models, agents]))
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["context_window"], 8192)
        host.validate_slot({"provider_id": "p", "model": "existing"})
        with self.assertRaisesRegex(core.RouterError, "model_missing"):
            host.validate_slot({"provider_id": "p", "model": "not-configured"})
        info.availability_status = "permission_denied"
        with self.assertRaisesRegex(core.RouterError, "model_unavailable"):
            host.validate_slot({"provider_id": "p", "model": "existing"})
        config = agent_config()
        for strategy in ("native", "scroll"):
            config.running.light_context_config.strategy = strategy
            host.validate_agent(config)
        config.active_model = NS(provider_id="", model="")
        manager.get_active_model = lambda: NS(provider_id="p", model="existing")
        host.validate_agent(config)
        config.backend = "other"
        with self.assertRaisesRegex(core.RouterError, "unsupported_backend"):
            host.validate_agent(config)

    async def test_runtime_version_gate_and_plugin_registration(self):
        if not (SRC / "runtime" / "hooks.py").is_file():
            self.skipTest("缺少附带宿主源码")
        phases, hooks = load_native_hooks()
        async def factory(agent_id=None, model_slot_override=None, agent_config=None):
            raise AssertionError("注册插件不能调用模型")
        def module(name, **attributes):
            value = ModuleType(name)
            value.__dict__.update(attributes)
            return value
        replacements = {
            "qwenpaw.__version__": module("version", __version__="2.2.0b1"),
            "qwenpaw.agents.model_factory": module("factory", create_model_and_formatter_async=factory),
            "qwenpaw.config.config": module("config", load_agent_config=lambda _: None),
            "qwenpaw.config.utils": module("config_utils", load_config=lambda: None),
            "qwenpaw.providers": module("providers", ProviderManager=NS(get_instance=lambda: None)),
            "qwenpaw.runtime.hooks": hooks, "qwenpaw.runtime.phases": phases,
            "qwenpaw.utils.io_utils": module("io", run_sync_io=lambda *args: None),
            "qwenpaw.utils.model_response": module("response", consume_model_response=lambda *args: None),
            "agentscope.message": module("message", Msg=dict, TextBlock=dict),
            "qwenpaw.constant": module("constant", WORKING_DIR=self.temp.name),
        }
        registered, cleanup = [], []
        api = NS(register_runtime_hook=lambda hook: registered.append(hook),
                 register_http_router=lambda *args, **kwargs: None,
                 register_uninstall_hook=lambda name, fn: cleanup.append(fn),
                 register_shutdown_hook=lambda name, fn: cleanup.append(fn))
        with patch.dict(sys.modules, replacements), patch.object(entry, "package_version", return_value="2.0.7"):
            host = entry.NativeHost(api)
            self.assertTrue(host.compatible)
            entry.ModelRouterPlugin().register(api)
            self.assertEqual(len(registered), 1)
            replacements["qwenpaw.__version__"].__version__ = "2.2.0"
            self.assertFalse(entry.NativeHost(api).compatible)
            entry.ModelRouterPlugin().register(api)
            self.assertEqual(len(registered), 1)
            for callback in cleanup:
                callback(plugin_id=core.PLUGIN_ID, delete_files=True)
                callback()


@unittest.skipUnless((SRC / "runtime" / "hooks.py").is_file(), "缺少附带宿主源码")
class SourceContractTests(unittest.IsolatedAsyncioTestCase):
    def test_native_imports_exist_in_source_not_only_in_stubs(self):
        tree = ast.parse((ROOT / "plugin.py").read_text(encoding="utf-8"))
        adapter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NativeHost")
        for node in ast.walk(adapter):
            if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith("qwenpaw."):
                continue
            path = SRC.joinpath(*node.module.split(".")[1:])
            path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
            bindings = set()
            for item in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bindings.add(item.name)
                elif isinstance(item, (ast.Import, ast.ImportFrom)):
                    bindings.update(alias.asname or alias.name.split(".")[0] for alias in item.names)
                elif isinstance(item, ast.Assign):
                    bindings.update(target.id for target in item.targets if isinstance(target, ast.Name))
            for alias in node.names:
                self.assertIn(alias.name, bindings, f"{node.module}.{alias.name} 不在真实模块顶层导出")

    async def test_actual_hook_order_duplicate_invocation_and_workspace_reload(self):
        phases, hooks = load_native_hooks()
        host = FakeHost()
        with tempfile.TemporaryDirectory() as directory:
            store = core.ConfigStore(directory)
            store.save(configured())
            service = core.RouterService(store, core.RouteRecords(directory), host)
            self.addCleanup(service.close)
            class SessionLoad(hooks.HookBase):
                name = "session_load"
                phase = phases.Phase.PRE_AGENT_BUILD
                priority = 10
                async def run(self, ctx):
                    ctx.session_state = {"state": {"summary": "loaded", "context": []}}
                    return hooks.HookResult()
            with patch.dict(sys.modules, {"qwenpaw.runtime.hooks": hooks, "qwenpaw.runtime.phases": phases}):
                hook = entry.create_hook(service)
            for _ in range(2):
                registry = hooks.HookRegistry()  # 模拟新 workspace，执行的是真实 HookRegistry。
                registry.register(hook)
                registry.register(SessionLoad())
                ctx = context()
                self.assertEqual([h.name for h in registry.hooks_for(phases.Phase.PRE_AGENT_BUILD)], ["session_load", "model_router.select"])
                await registry.run(phases.Phase.PRE_AGENT_BUILD, ctx)
                await registry.run(phases.Phase.PRE_AGENT_BUILD, ctx)
                self.assertEqual(ctx.request.model_slot_override["model"], "tier:2")
            self.assertEqual(len(host.calls), 2)
            self.assertTrue(all(call[2]["summary"] == "loaded" for call in host.calls))
            service.close()
            ctx = context()
            await hook.run(ctx)
            self.assertFalse(hasattr(ctx.request, "model_slot_override"))
            service.close()

    def test_static_baseline_factory_order_fallback_and_lifecycle(self):
        def source(relative):
            text = (SRC / relative).read_text(encoding="utf-8")
            ast.parse(text)
            return text
        self.assertIn('__version__ = "2.2.0b1"', source("__version__.py"))
        factory = source("agents/model_factory.py")
        self.assertIn("has_model_override or not fallback_slots", factory)
        builder = source("runtime/builder.py")
        self.assertLess(builder.index('getattr(ctx.request, "model_slot_override"'), builder.index("await self._build_scroll_components"))
        runtime = source("runtime/runtime.py")
        self.assertLess(runtime.index("hooks.run(Phase.PRE_AGENT_BUILD"), runtime.index("await builder.build(ctx)"))
        self.assertLess(runtime.index("cmd_registry.dispatch"), runtime.index("hooks.run(Phase.PRE_AGENT_BUILD"))
        self.assertEqual(runtime.count("hooks.run(Phase.PRE_AGENT_BUILD"), 1)
        api = source("plugins/api.py")
        self.assertIn("reload_safe=True", api)
        self.assertIn("def register_runtime_hook", api)
        lifecycle = source("app/routers/plugins.py")
        self.assertIn("_schedule_all_agents_reload", lifecycle)
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["qwenpaw_version"], {"min": "2.2.0b1", "max": "2.2.1"})
        self.assertNotIn("dependencies", manifest)
        architecture = load_module("_native_plugin_architecture", SRC / "plugins" / "architecture.py")
        parsed = architecture.PluginManifest.from_dict(manifest)
        self.assertEqual(parsed.plugin_type.value, "general")
        package_stub("_router_version_host")
        load_module("_router_version_host.__version__", SRC / "__version__.py")
        compat = load_module("_router_version_host._version_compat", SRC / "_version_compat.py")
        self.assertTrue(compat.check_plugin_version_compat(parsed)[0])

    async def test_actual_auth_middleware_proxy_whitelist_and_runtime_boundary(self):
        package_stub("_router_auth_host")
        package_stub("_router_auth_host.app")
        package_stub("_router_auth_host.security")
        package_stub("_router_auth_host.constant", SECRET_DIR=ROOT / "never-used", EnvVarLoader=NS())
        package_stub("_router_auth_host.security.secret_store", AUTH_SECRET_FIELDS=[],
                     decrypt_dict_fields=lambda x, *_: x, encrypt_dict_fields=lambda x, *_: x,
                     is_encrypted=lambda _: False)
        auth = load_module("_router_auth_host.app.auth", SRC / "app" / "auth.py")
        with tempfile.TemporaryDirectory() as directory:
            service = core.RouterService(core.ConfigStore(directory), core.RouteRecords(directory), FakeHost())
            self.addCleanup(service.close)
            app = FastAPI()
            app.include_router(entry.create_router(service), prefix="/api/model-router")
            app.add_middleware(auth.AuthMiddleware)
            app.add_middleware(auth.RuntimeBoundaryMiddleware)
            cfg = NS(security=NS(allow_no_auth_hosts=["127.0.0.1"]))
            trusted = auth._parse_networks(["203.0.113.10"])
            with patch.object(auth, "is_auth_enabled", return_value=True), \
                 patch.object(auth, "has_registered_users", return_value=True), \
                 patch.object(auth, "verify_token", side_effect=lambda token: "tester" if token == "offline-valid" else None), \
                 patch.object(auth, "_get_config_cached", return_value=(cfg, trusted)), \
                 patch.dict(os.environ, {"QWENPAW_RUNTIME_INTERNAL_TOKEN": ""}):
                async def request(peer, path="/status", headers=None, method="GET", payload=None):
                    transport = httpx.ASGITransport(app=app, client=(peer, 1234))
                    async with httpx.AsyncClient(transport=transport, base_url="http://offline.test") as client:
                        return await client.request(method, "/api/model-router" + path, headers=headers, json=payload)
                for path in ("/config", "/status", "/recent"):
                    self.assertEqual((await request("198.51.100.20", path)).status_code, 401)
                    self.assertEqual((await request("198.51.100.20", path, {"Authorization": "Bearer offline-valid"})).status_code, 200)
                for method, path in (("PUT", "/config"), ("POST", "/classify")):
                    self.assertEqual((await request("198.51.100.20", path, method=method, payload={})).status_code, 401)
                self.assertEqual((await request("198.51.100.20", headers={"X-Forwarded-For": "127.0.0.1"})).status_code, 401)
                self.assertEqual((await request("203.0.113.10", headers={"X-Forwarded-For": "127.0.0.1"})).status_code, 401)
                self.assertEqual((await request("127.0.0.1")).status_code, 200)
                cfg.security.allow_no_auth_hosts = ["192.0.2.8"]
                self.assertEqual((await request("203.0.113.10", headers={"X-Forwarded-For": "192.0.2.8"})).status_code, 200)
                self.assertEqual((await request("203.0.113.10", headers={"X-Forwarded-For": "192.0.2.8, 198.51.100.20"})).status_code, 401)
                with patch.dict(os.environ, {"QWENPAW_RUNTIME_INTERNAL_TOKEN": "offline-boundary"}):
                    self.assertEqual((await request("203.0.113.10", headers={"Authorization": "Bearer offline-valid"})).status_code, 401)
                    headers = {"Authorization": "Bearer offline-valid", "x-qwenpaw-runtime-token": "offline-boundary"}
                    self.assertEqual((await request("203.0.113.10", headers=headers)).status_code, 200)


class EvaluationAndUiTests(unittest.TestCase):
    def test_sixty_independent_samples_and_score_accounting(self):
        samples = [json.loads(line) for line in (ROOT / "evaluation.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(samples), 60)
        self.assertEqual(len({s["id"] for s in samples}), 60)
        self.assertEqual(Counter(s["level"] for s in samples), {1: 20, 2: 20, 3: 20})
        self.assertEqual(Counter(s["split"] for s in samples), {"calibration": 39, "holdout": 21})
        prompt = (ROOT / "prompt.txt").read_text(encoding="utf-8")
        for sample in samples:
            payload = sample_payload(sample)
            if "long_input" in sample["tags"]:
                self.assertTrue(payload["current_truncated"])
                self.assertEqual(len(payload["current"]), core.CURRENT_LIMIT)
            else:
                self.assertEqual(payload["current"], sample["current"])
            self.assertNotIn(sample["current"], prompt)
        # 只验证评分程序，不把人为构造的预测称为真实判级成绩。
        predictions = [{"id": s["id"], "level": s["level"], "elapsed_ms": i + 1} for i, s in enumerate(samples)]
        predictions[-1]["level"] = 1
        report = score_predictions(samples, predictions)
        self.assertEqual(report["groups"]["holdout"]["l3_to_l1"], 1)
        self.assertEqual(report["p95_ms"], 57)
        self.assertEqual(report["input_tokens"]["sum"], None)
        self.assertEqual(score_predictions(samples, [])["groups"]["all"]["accuracy"], 0)

    @unittest.skipUnless(shutil.which("node"), "未安装 Node")
    def test_frontend_syntax_mount_controls_and_registration(self):
        subprocess.run(["node", "--check", str(ROOT / "ui/index.js")], check=True, capture_output=True)
        if not (SOURCE / 'console/src/plugins/hostSdk/fetch.ts').is_file():
            self.skipTest('缺少附带前端 SDK 源码')
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const {stripTypeScriptTypes} = require('node:module');
const source = process.argv[2];
const native = file => stripTypeScriptTypes(fs.readFileSync(path.join(source,file),'utf8'))
 .replace(/^import .*;\r?$/gm,'').replaceAll('export ','');
const calls = [];
const networkFetch = async (url,init) => {
 calls.push({url,init});
 assert.equal(init.headers.Authorization,'Bearer offline-valid');
 assert.equal(init.headers['X-Agent-Id'],'pilot');
 const routes = {
  '/api/model-router/config':init.method==='PUT'?JSON.parse(init.body):config,
  '/api/model-router/status':status,
  '/api/model-router/recent':{records:[]},
  '/api/model-router/classify':{level:1,target:{provider_id:'test',model:'small'},reason_code:'simple',elapsed_ms:1},
 };
 return new Response(JSON.stringify(routes[url]||{detail:'Not Found'}),{status:url in routes?200:404});
};
// 使用真实 SDK 拼接 URL，只替换认证来源和最底层网络，不掩盖重复 /api。
const hostFetch = vm.runInNewContext(native('console/src/api/config.ts')+'\n'+native('console/src/plugins/hostSdk/fetch.ts')+'\nhostFetch', {
 VITE_API_BASE_URL:'', buildAuthHeaders:()=>({Authorization:'Bearer offline-valid','X-Agent-Id':'pilot'}),fetch:networkFetch,
});
const state = [], effects = []; let cursor = 0, route, menu, mounted = false;
const config = {schema_version:1,enabled:false,mode:'shadow',enabled_agents:[],classifier:{provider_id:'',model:''},tiers:{'1':{},'2':{},'3':{}},classifier_timeout_seconds:3};
const status = {models:[],agents:[],compatible:true,checks:{},plugin_version:'0.1.0',prompt_version:'router-zh-v2'};
const React = {
 createElement:(type,props,...children)=>({type,props:props||{},children}),
 useState(initial) { const i=cursor++; if (!(i in state)) state[i]=initial; return [state[i],v=>state[i]=typeof v==='function'?v(state[i]):v]; },
 useRef(initial) { return this.useState({current:initial})[0]; },
 useEffect(fn) { if(!mounted) effects.push(fn); }
};
const antd = new Proxy({}, {get:(_,key)=>key==='Input'?{TextArea:'TextArea'}:key==='Radio'?{Group:'RadioGroup'}:key});
const sdk = {
 host:{React,antd,fetch:hostFetch},
 route:{add:(id,r)=>{route=r;return {dispose(){}};}},
 menu:{add:(id,m)=>{menu=m;return {dispose(){}};}}
};
vm.runInNewContext(fs.readFileSync('ui/index.js','utf8'),{window:{QwenPaw:sdk},AbortController,console});
assert.equal(route.path,'/model-router'); assert.equal(menu.location,'primary.settings');
(async()=>{
 route.component(); effects.forEach(fn=>fn()); mounted=true;
 await new Promise(resolve=>setImmediate(resolve));
 assert.deepEqual(calls.map(c=>c.url),['/api/model-router/config','/api/model-router/status','/api/model-router/recent']);
 assert(calls.every(c=>c.init.signal instanceof AbortSignal));
 cursor=0; const tree=route.component();
 const flat=n=>n&&typeof n==='object'?[n,...(n.children||[]).flatMap(flat)]:[];
 const nodes=flat(tree); assert.equal(tree.type,'main');
 assert.equal(nodes.filter(n=>n.type==='Select').length,6);
 assert(nodes.some(n=>n.type==='TextArea'&&n.props.maxLength===16000));
 await nodes.find(n=>n.type==='Button'&&n.children.includes('保存配置')).props.onClick();
 assert.equal(calls.at(-1).url,'/api/model-router/config');
 assert.equal(calls.at(-1).init.method,'PUT');
 const selector=nodes.find(n=>n.type==='Select'&&n.props['aria-label'].startsWith('独立判级模型'));
 selector.props.onChange(JSON.stringify(['test','small']));
 cursor=0; let updated=flat(route.component());
 await updated.find(n=>n.type==='Button'&&n.children.includes('保存配置')).props.onClick();
 assert.deepEqual(JSON.parse(calls.at(-1).init.body).classifier,{provider_id:'test',model:'small'});
 updated.find(n=>n.type==='Select'&&n.props['aria-label']==='测试 Agent').props.onChange('pilot');
 updated.find(n=>n.type==='TextArea').props.onChange({target:{value:'简单任务'}});
 cursor=0; updated=flat(route.component());
 await updated.find(n=>n.type==='Button'&&n.children.includes('调用判级模型（可能付费）')).props.onClick();
 assert.equal(calls.at(-1).url,'/api/model-router/classify');
 assert.equal(calls.at(-1).init.method,'POST');
 assert.deepEqual(JSON.parse(calls.at(-1).init.body),{agent_id:'pilot',text:'简单任务'});
 await updated.find(n=>n.type==='Button'&&n.children.includes('刷新记录与模型目录')).props.onClick();
 assert.deepEqual(calls.slice(-2).map(c=>c.url),['/api/model-router/status','/api/model-router/recent']);
 const toggle=nodes.find(n=>n.type==='Switch'); toggle.props.onChange(true);
 cursor=0; const nodes2=flat(route.component()); nodes2.find(n=>n.type==='RadioGroup').props.onChange({target:{value:'active'}});
 cursor=0; const nodes3=flat(route.component());
 assert(nodes3.some(n=>n.type==='Checkbox'));
 assert(nodes3.find(n=>n.type==='Button'&&n.children.includes('保存配置')).props.disabled);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(["node", "-", str(SOURCE)], input=script, encoding="utf-8", cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def build_package():
    destination = ROOT / "dist" / f"{core.PLUGIN_ID}-{core.VERSION}.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in FILES:
            info = zipfile.ZipInfo(filename, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / filename).read_bytes())
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(FILES)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(json.dumps({"package": str(destination), "sha256": digest}, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--package"]:
        build_package()
    elif len(sys.argv) == 3 and sys.argv[1] == "--score":
        samples = [json.loads(line) for line in (ROOT / "evaluation.jsonl").read_text(encoding="utf-8").splitlines()]
        rows = [json.loads(line) for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()]
        print(json.dumps(score_predictions(samples, rows), ensure_ascii=False, indent=2))
    else:
        unittest.main()
