"""QwenPaw 2.2.0b1 插件入口：原生 Hook、模型工厂和管理接口。"""
from __future__ import annotations

import asyncio
import copy
import inspect
from importlib.metadata import version as package_version
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .router import (
    PLUGIN_ID, VERSION, PROMPT_VERSION, ConfigStore, RouteRecords, RouterError,
    RouterService, UNAVAILABLE, get, strict_json, validate_config,
)

PROMPT = Path(__file__).with_name("prompt.txt").read_text(encoding="utf-8")


class NativeHost:
    def __init__(self, api):
        self.version = "unknown"
        self.agentscope_version = "unknown"
        self.checks = {}
        try:
            from qwenpaw.__version__ import __version__
            self.version = __version__
            self.agentscope_version = package_version("agentscope")
            from qwenpaw.agents.model_factory import create_model_and_formatter_async
            from qwenpaw.config.config import load_agent_config
            from qwenpaw.config.utils import load_config
            from qwenpaw.providers import ProviderManager
            from qwenpaw.runtime.hooks import HookContext
            from qwenpaw.utils.io_utils import run_sync_io
            from qwenpaw.utils.model_response import consume_model_response
            from agentscope.message import Msg, TextBlock

            self.checks = {
                "qwenpaw_version": self.version == "2.2.0b1",
                "agentscope_version": self.agentscope_version == "2.0.7",
                "plugin_api": all(callable(getattr(api, name, None)) for name in (
                    "register_runtime_hook", "register_http_router", "register_uninstall_hook", "register_shutdown_hook")),
                "model_factory": {"agent_id", "model_slot_override", "agent_config"}.issubset(
                    inspect.signature(create_model_and_formatter_async).parameters),
                "hook_context": {"request", "extras", "session_state", "input_msgs"}.issubset(HookContext.__dataclass_fields__),
            }
            self.factory, self.consume = create_model_and_formatter_async, consume_model_response
            self.load_agent, self.load_config = load_agent_config, load_config
            self.io, self.manager = run_sync_io, ProviderManager.get_instance
            self.Msg, self.TextBlock = Msg, TextBlock
        except Exception:
            self.checks["imports"] = False
        self.compatible = bool(self.checks) and all(self.checks.values())

    async def agent_config(self, agent_id):
        if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 128:
            raise RouterError("invalid_agent")
        root = await self.io(self.load_config)
        if agent_id not in root.agents.profiles:
            raise RouterError("agent_missing")
        return await self.io(self.load_agent, agent_id)

    def validate_slot(self, slot):
        if not slot or not slot.get("provider_id") or not slot.get("model"):
            raise RouterError("models_required")
        provider = self.manager().get_provider(slot["provider_id"])
        if provider is None:
            raise RouterError("provider_missing")
        info = provider.get_model_info(slot["model"])
        if info is None:
            raise RouterError("model_missing")
        if get(info, "availability_status") in UNAVAILABLE:
            raise RouterError("model_unavailable")
        window = provider.get_context_size(slot["model"])
        if type(window) is not int or window < 1000:
            raise RouterError("invalid_context_window")
        return info

    def validate_agent(self, config):
        if config.backend != "qwenpaw":
            raise RouterError("unsupported_backend")
        lcc = config.running.light_context_config
        if not lcc.context_compact_config.enabled:
            raise RouterError("compression_disabled")
        if lcc.strategy not in ("native", "scroll"):
            raise RouterError("unsupported_context_strategy")
        default = config.active_model
        if not default or not get(default, "provider_id") or not get(default, "model"):
            default = self.manager().get_active_model()
        if not default or not get(default, "provider_id") or not get(default, "model"):
            raise RouterError("default_model_required")

    async def classify(self, slot, agent_id, payload, agent_config):
        private = copy.deepcopy(agent_config)
        private.running.llm_retry_enabled = False
        private.thinking_level = "off"
        model, _ = await self.factory(agent_id=agent_id, model_slot_override=copy.deepcopy(slot), agent_config=private)
        # 工厂在线程中构建时无法强杀线程；取消后不启动模型调用。
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        messages = [
            self.Msg(name="system", role="system", content=[self.TextBlock(type="text", text=PROMPT)]),
            self.Msg(name="user", role="user", content=[self.TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))]),
        ]
        return await self.consume(model, messages, max_tokens=128, disable_thinking=True)

    async def validate_references(self, config):
        if not self.compatible:
            raise RouterError("incompatible_host")
        for agent_id in config["enabled_agents"]:
            self.validate_agent(await self.agent_config(agent_id))
        for slot in [config["classifier"], *config["tiers"].values()]:
            if slot["model"]:
                self.validate_slot(slot)

    async def catalog(self):
        # 与 /api/models 复用相同目录。只投影白名单字段，不返回 API key、URL、headers 或 kwargs。
        providers = await self.manager().list_provider_info()
        models = []
        for provider in providers:
            seen = set()
            for info in [*provider.models, *provider.extra_models]:
                if info.id in seen or info.id in (get(provider, "hidden_model_ids", []) or []):
                    continue
                seen.add(info.id)
                native = self.manager().get_provider(provider.id)
                models.append({"provider_id": provider.id, "provider_name": provider.name,
                               "model": info.id, "name": info.name,
                               "availability": get(info, "availability_status", "unverified"),
                               "context_window": native.get_context_size(info.id) if native else None})
        root = await self.io(self.load_config)
        agents = []
        for agent_id in root.agents.profiles:
            try:
                config = await self.io(self.load_agent, agent_id)
                reason = None
                try:
                    self.validate_agent(config)
                except RouterError as exc:
                    reason = str(exc)
                agents.append({"id": agent_id, "name": config.name, "bypass_reason": reason})
            except Exception:
                agents.append({"id": agent_id, "name": agent_id, "bypass_reason": "agent_unreadable"})
        return models, agents


def create_hook(service):
    from qwenpaw.runtime.hooks import HookBase, HookResult
    from qwenpaw.runtime.phases import Phase

    class ModelRouterHook(HookBase):
        phase = Phase.PRE_AGENT_BUILD
        name = "model_router.select"
        after = ("session_load",)
        priority = 100

        async def run(self, ctx):
            await service.route(ctx)
            return HookResult()

    return ModelRouterHook()


async def read_body(request, limit=65536):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > limit:
            raise RouterError("body_too_large")
    return strict_json(bytes(content))


def create_router(service):
    api = APIRouter()

    def error(code, status=400):
        return JSONResponse({"error": code}, status_code=status)

    @api.get("/config")
    async def config_get():
        try:
            return service.store.read()
        except RouterError as exc:
            return error(str(exc), 503)

    @api.put("/config")
    async def config_put(request: Request):
        try:
            config = validate_config(await read_body(request))
            try:
                old = service.store.read()
            except RouterError:
                old = None
            # 允许原样关闭失效配置，不让已删除的模型阻止紧急停用。
            disabling = old is not None and not config["enabled"] and {**old, "enabled": False} == config
            if not disabling:
                await service.host.validate_references(config)
            return await asyncio.to_thread(service.store.save, config)
        except RouterError as exc:
            return error(str(exc))
        except OSError:
            return error("config_write_failed", 503)
        except Exception:
            return error("host_unavailable", 503)

    @api.post("/classify")
    async def classify(request: Request):
        try:
            body = await read_body(request)
            if not isinstance(body, dict) or set(body) != {"agent_id", "text"}:
                raise RouterError("invalid_test_fields")
            return await service.test(body["agent_id"], body["text"])
        except RouterError as exc:
            code = str(exc)
            return error(code, 429 if code in {"test_busy", "classifier_busy"} else 400)
        except asyncio.CancelledError:
            raise
        except Exception:
            return error("classifier_or_host_error", 503)

    @api.get("/status")
    async def status():
        host = service.host
        result = {"plugin_version": VERSION, "prompt_version": PROMPT_VERSION,
                  "qwenpaw_version": host.version, "agentscope_version": host.agentscope_version,
                  "compatible": host.compatible, "checks": host.checks,
                  "closed": service.closed, "log_write_failed": service.records.log_failed,
                  "models": [], "agents": [], "catalog_error": None,
                  "fallback": "成功覆盖后无原生跨模型 fallback；同模型重试仍由宿主控制。",
                  "verification": "离线检查不等于真实宿主、跨 Provider 或容器验收。"}
        if host.compatible:
            try:
                result["models"], result["agents"] = await host.catalog()
            except Exception:
                result["catalog_error"] = "catalog_unavailable"
        return result

    @api.get("/recent")
    async def recent(request: Request):
        try:
            limit = int(request.query_params.get("limit", "100"))
            if not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            return error("invalid_limit")
        return {"records": service.records.recent(limit)}

    return api


class ModelRouterPlugin:
    def register(self, api):
        from qwenpaw.constant import WORKING_DIR

        host = NativeHost(api)
        directory = Path(WORKING_DIR) / "plugin-data" / PLUGIN_ID
        service = RouterService(ConfigStore(directory), RouteRecords(directory), host)
        api.register_http_router(create_router(service), prefix="/model-router", tags=[PLUGIN_ID])
        if host.compatible:
            api.register_runtime_hook(create_hook(service))
        api.register_shutdown_hook("model_router.close", service.close)
        api.register_uninstall_hook("model_router.uninstall", service.close)


plugin = ModelRouterPlugin()
