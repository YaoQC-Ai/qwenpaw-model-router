"""与宿主解耦的配置、上下文边界和单回合路由逻辑。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone

PLUGIN_ID = "qwenpaw-model-router"
VERSION = "0.1.0"
PROMPT_VERSION = "router-zh-v2"
CURRENT_LIMIT, SUMMARY_LIMIT, HISTORY_LIMIT = 8000, 2000, 4000
REASONS = {"simple", "standard", "complex", "continuation", "insufficient_context", "high_risk"}
UNAVAILABLE = {"permission_denied", "model_not_found", "incompatible_api", "rate_limited", "transient_error"}
STATE_KEY = "qwenpaw-model-router.result"


class RouterError(ValueError):
    """只携带可公开的固定错误码，禁止拼接模型异常或用户正文。"""


def get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return getattr(obj, key, default)
    except (KeyError, TypeError):
        return default


def default_config():
    return {
        "schema_version": 1, "enabled": False, "mode": "shadow", "enabled_agents": [],
        "classifier": {"provider_id": "", "model": ""},
        "tiers": {str(i): {"provider_id": "", "model": ""} for i in (1, 2, 3)},
        "classifier_timeout_seconds": 3,
    }


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RouterError("invalid_json")
            result[key] = value
        return result

    def invalid(_):
        raise RouterError("invalid_json")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError, RecursionError):
        raise RouterError("invalid_json") from None


def validate_config(value):
    if not isinstance(value, dict) or set(value) != set(default_config()):
        raise RouterError("invalid_config_fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RouterError("unsupported_schema")
    if type(value["enabled"]) is not bool or value["mode"] not in ("shadow", "active"):
        raise RouterError("invalid_mode")
    agents = value["enabled_agents"]
    if (not isinstance(agents, list) or len(agents) > 256
            or any(not isinstance(a, str) or not a or len(a) > 128 for a in agents)
            or len(set(agents)) != len(agents)):
        raise RouterError("invalid_agents")
    timeout = value["classifier_timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 10:
        raise RouterError("invalid_timeout")
    if not isinstance(value["tiers"], dict) or set(value["tiers"]) != {"1", "2", "3"}:
        raise RouterError("invalid_tiers")
    for slot in [value["classifier"], *value["tiers"].values()]:
        if not isinstance(slot, dict) or set(slot) != {"provider_id", "model"}:
            raise RouterError("invalid_model_reference")
        if any(not isinstance(s, str) or len(s) > 256 or s != s.strip()
               or any(ord(c) < 32 for c in s) for s in slot.values()):
            raise RouterError("invalid_model_reference")
        if bool(slot["provider_id"]) != bool(slot["model"]):
            raise RouterError("incomplete_model_reference")
        if value["enabled"] and not slot["model"]:
            raise RouterError("models_required")
    if value["enabled"] and not agents:
        raise RouterError("agents_required")
    return copy.deepcopy(value)


def parse_grade(text):
    if not isinstance(text, str) or len(text) > 1024:
        raise RouterError("invalid_output")
    result = strict_json(text)
    if not isinstance(result, dict) or set(result) != {"level", "reason_code"}:
        raise RouterError("invalid_output")
    level, reason = result["level"], result["reason_code"]
    if type(level) is not int or level not in (1, 2, 3) or not isinstance(reason, str) or reason not in REASONS:
        raise RouterError("invalid_output")
    return level, reason


class ConfigStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "config.json"
        self.lock = threading.Lock()

    def read(self):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise RouterError("config_unreadable")
            return validate_config(strict_json(raw))
        except FileNotFoundError:
            return default_config()
        except (OSError, ValueError):
            raise RouterError("config_unreadable") from None

    def save(self, value):
        value = validate_config(value)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock:
            filename = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                                 prefix=".config-", suffix=".tmp", delete=False) as stream:
                    filename = stream.name
                    json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(filename, self.path)
            finally:
                if filename and os.path.exists(filename):
                    os.unlink(filename)
        return value


def media_types(content, depth=0):
    if depth > 16:
        raise RouterError("unsupported_content")
    if isinstance(content, str) or content is None:
        return set()
    if isinstance(content, list):
        result = set()
        for block in content:
            result.update(media_types(block, depth + 1))
        return result
    kind = get(content, "type", "")
    if kind in {"text", "thinking", "reasoning", "analysis", "reasoning_content", "tool_use", "tool_call", "hint"}:
        return set()
    if kind in {"image", "audio", "video", "file"}:
        return {kind}
    if kind == "data":
        mime = get(get(content, "source"), "media_type", "")
        major = mime.split("/", 1)[0] if isinstance(mime, str) else ""
        return {major if major in {"image", "audio", "video"} else "file"}
    if kind == "tool_result":
        return (media_types(get(content, "content"), depth + 1)
                | media_types(get(content, "output"), depth + 1))
    # 未知结构仍旁路；媒体只提取类型，不打开文件或下载 URL。
    raise RouterError("unsupported_content")


def visible_text(msg):
    if get(msg, "role") not in {"user", "assistant"}:
        return ""
    content = get(msg, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(get(b, "text", "") for b in content
                         if get(b, "type") == "text" and isinstance(get(b, "text"), str))
    return ""


def extract_context(input_msgs, session_state):
    state = session_state or {}
    if not isinstance(state, dict):
        raise RouterError("unsupported_session_state")
    if "state" in state:
        raw = state["state"]
        if not isinstance(raw, dict):
            raise RouterError("unsupported_session_state")
        history, summary = raw.get("context", []), raw.get("summary", "")
    elif "memory" in state:
        memory = state["memory"]
        if not isinstance(memory, dict):
            raise RouterError("unsupported_session_state")
        rows = memory.get("content", [])
        if not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != 2 for r in rows):
            raise RouterError("unsupported_session_state")
        history, summary = [r[0] for r in rows], memory.get("_compressed_summary", "")
    elif state and set(state) - {"mode_state"}:
        raise RouterError("unsupported_session_state")
    else:
        history, summary = [], ""
    if not isinstance(history, list) or not isinstance(summary, str):
        raise RouterError("unsupported_session_state")
    current_media = set()
    for msg in input_msgs:
        types = media_types(get(msg, "content"))
        if get(msg, "role") == "user":
            current_media.update(types)
    current = "\n".join(visible_text(m) for m in input_msgs if get(m, "role") == "user")
    if not current.strip() and not current_media:
        raise RouterError("empty_input")
    if current.lstrip().startswith("/"):
        raise RouterError("control_command")
    current_truncated = len(current) > CURRENT_LIMIT
    if current_truncated:
        # 仅裁剪判级副本，首尾合计有界；显式标注缺失，不按字数强制升级。
        current = current[:CURRENT_LIMIT // 2] + current[-CURRENT_LIMIT // 2:]
    starts = [i for i, m in enumerate(history) if get(m, "role") == "user"]
    begin = starts[-2] if len(starts) >= 2 else (starts[0] if starts else 0)
    history_media = set()
    for msg in history[begin:]:
        history_media.update(media_types(get(msg, "content")))
    truncated = begin > 0 or len(summary) > SUMMARY_LIMIT
    recent, remaining = [], HISTORY_LIMIT
    for msg in reversed(history[begin:]):
        text = visible_text(msg)
        if not text:
            continue
        if len(text) > remaining:
            truncated = True
        if remaining:
            recent.append({"role": get(msg, "role"), "text": text[-remaining:]})
            remaining -= min(len(text), remaining)
    return {"current": current, "summary": summary[:SUMMARY_LIMIT],
            "history": list(reversed(recent)), "history_truncated": truncated,
            "current_truncated": current_truncated,
            "media": {"current": sorted(current_media), "history": sorted(history_media)}}


def bypass_reason(ctx, config):
    if not config["enabled"]:
        return "disabled"
    if ctx.agent_id not in config["enabled_agents"]:
        return "agent_not_enabled"
    request = ctx.request
    rc = get(request, "request_context") or {}
    if not isinstance(rc, dict):
        return "unknown_source"
    if get(request, "model_slot_override") is not None or rc.get("model_slot_override") is not None:
        return "explicit_override"
    if rc.get("_spawn_subagent") or get(request, "_spawn_subagent"):
        return "subagent"
    # V1 只纳入已核对的 console 转换路径；局域网网关复用该路径。
    for source in (rc.get("source"), get(request, "session_source"), rc.get("session_source"), get(request, "source")):
        if source not in (None, "", "console"):
            return "automation" if source in ("cron", "heartbeat", "mail_monitor") else "unknown_source"
    if get(request, "channel") != "console":
        return "unverified_channel"
    return None


class RouteRecords:
    def __init__(self, directory):
        self.rows = deque(maxlen=500)
        self.lock = threading.Lock()
        self.salt = secrets.token_bytes(32)
        self.directory = Path(directory)
        self.handler = None
        self.log_failed = False
        self.closed = False

    def append(self, result, agent_id, session_id):
        fingerprint = hmac.new(self.salt, f"{agent_id}\0{session_id}".encode(), hashlib.sha256).hexdigest()[:16]
        row = {**result, "time": datetime.now(timezone.utc).isoformat(),
               "agent_id": str(agent_id)[:128], "session": fingerprint}
        with self.lock:
            if self.closed:
                return
            self.rows.appendleft(row)
            try:
                if self.handler is None:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    self.handler = RotatingFileHandler(self.directory / "routing.log", maxBytes=5 * 1024 * 1024,
                                                       backupCount=3, encoding="utf-8")
                    self.handler.handleError = lambda _: setattr(self, "log_failed", True)
                record = logging.LogRecord(PLUGIN_ID, logging.INFO, "", 0,
                                           json.dumps(row, ensure_ascii=False), (), None)
                self.handler.emit(record)
            except OSError:
                self.log_failed = True

    def recent(self, limit=100):
        with self.lock:
            return copy.deepcopy(list(self.rows)[:max(1, min(100, limit))])

    def close(self):
        with self.lock:
            self.closed = True
            if self.handler:
                self.handler.close()
                self.handler = None


class RouterService:
    def __init__(self, store, records, host):
        self.store, self.records, self.host = store, records, host
        self.closed = False
        self.pending = set()
        self.test_running = 0

    async def classify(self, config, agent_id, payload, agent_config):
        self.host.validate_slot(config["classifier"])
        # ponytail: 单进程最多16个判级任务；多进程限额需在宿主统一实现。
        if len(self.pending) >= 16:
            raise RouterError("classifier_busy")
        task = asyncio.create_task(self.host.classify(config["classifier"], agent_id, payload, agent_config))
        self.pending.add(task)

        def finished(done):
            self.pending.discard(done)
            if not done.cancelled():
                done.exception()  # 回收忽略取消的 Provider 延迟异常，不记录异常正文。

        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=config["classifier_timeout_seconds"])
            if not done:
                raise RouterError("classifier_timeout")
            return parse_grade(task.result())
        finally:
            if not task.done():
                task.cancel()  # 不等待不合作的 SDK；迟到结果绝不再写请求。

    async def decide(self, config, agent_id, payload, agent_config):
        level, reason = await self.classify(config, agent_id, payload, agent_config)
        target = config["tiers"][str(level)]
        self.host.validate_slot(target)
        return {"level": level, "reason_code": reason, "target": copy.deepcopy(target)}

    async def route(self, ctx):
        if STATE_KEY in ctx.extras:
            return ctx.extras[STATE_KEY]
        result = {"outcome": "bypass", "reason_code": "pending", "mode": "unknown",
                  "level": None, "target": None, "elapsed_ms": 0}
        ctx.extras[STATE_KEY] = result
        start = time.monotonic()
        try:
            if self.closed:
                raise RouterError("plugin_unloaded")
            config = self.store.read()
            result["mode"] = config["mode"]
            reason = bypass_reason(ctx, config)
            if reason:
                raise RouterError(reason)
            if not self.host.compatible:
                raise RouterError("incompatible_host")
            agent_config = await self.host.agent_config(ctx.agent_id)
            self.host.validate_agent(agent_config)
            payload = extract_context(ctx.input_msgs, ctx.session_state)
            result.update(await self.decide(config, ctx.agent_id, payload, agent_config))
            if self.closed:
                raise RouterError("plugin_unloaded")
            # 判级等待期间其他扩展若已设置人工覆盖，也不抢占。
            if bypass_reason(ctx, config) == "explicit_override":
                raise RouterError("explicit_override")
            if config["mode"] == "active":
                ctx.request.model_slot_override = copy.deepcopy(result["target"])
                result["outcome"] = "selected"
            else:
                result["outcome"] = "shadow"
        except asyncio.CancelledError:
            result.update(outcome="cancelled", reason_code="cancelled")
            raise
        except RouterError as exc:
            result.update(outcome="bypass", reason_code=str(exc))
        except Exception:
            result.update(outcome="bypass", reason_code="classifier_or_host_error")
        finally:
            result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 2)
            self.records.append(result, ctx.agent_id, ctx.session_id)
        return result

    async def test(self, agent_id, text):
        if self.closed or not self.host.compatible:
            raise RouterError("incompatible_host")
        if self.test_running >= 2:
            raise RouterError("test_busy")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise RouterError("invalid_test_input")
        self.test_running += 1
        start = time.monotonic()
        try:
            config = self.store.read()
            agent_config = await self.host.agent_config(agent_id)
            self.host.validate_agent(agent_config)
            payload = extract_context([{"role": "user", "content": text}], None)
            result = await self.decide(config, agent_id, payload, agent_config)
            return {**result, "outcome": "test_only", "elapsed_ms": round((time.monotonic() - start) * 1000, 2)}
        finally:
            self.test_running -= 1

    def close(self, **_):
        self.closed = True
        for task in tuple(self.pending):
            task.cancel()
        self.records.close()
