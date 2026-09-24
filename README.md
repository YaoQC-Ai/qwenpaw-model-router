# QwenPaw Model Router

English | [简体中文](README.zh-CN.md)

A native QwenPaw plugin that classifies each eligible user turn and selects an L1, L2, or L3 execution model. It reuses your configured providers and models, without a proxy service or host source changes.

**Version:** `0.1.0` · **Classifier prompt:** [`router-zh-v2`](prompt.txt) · **Status:** initial manual testing passed; broader acceptance testing is ongoing.

## Features

- Independent classifier and three independently configurable execution-model slots, referenced by `provider_id` + `model`.
- One classification per eligible user turn; the selected model stays fixed throughout that turn's tool loop.
- Native settings page at `/model-router`: enable switch, Agent selection, model selectors, timeout, classification test, and recent routing records.
- `shadow` records recommendations; `active` applies a request-scoped model override.
- Disabled by default, with no preconfigured models, credentials, or Agent IDs.
- No additional plugin runtime dependencies or frontend build step; the UI uses the host React/antd SDK.

The current UI, classifier prompt, and synthetic evaluation samples are in Chinese. This English README does not imply an English UI or multilingual quality validation.

## Compatibility

| Component | Supported/tested baseline |
| --- | --- |
| QwenPaw | Exactly `2.2.0b1` |
| AgentScope | Exactly `2.0.7` |
| Agent backend | `qwenpaw` |
| Context strategy | `native` or `scroll`, with compression enabled |
| Entry point | Selected Agents' native `console` user requests |

The manifest declares `>=2.2.0b1, <2.2.1`, but the runtime gate is intentionally stricter: **only the exact baseline above is accepted**. Other versions are not claimed to work. On incompatibility, the plugin exposes management status but does not register the routing hook.

Initial container checks used Linux/Debian. Complete Ubuntu and Windows host acceptance has not been established. Heartbeats, cron jobs, sub-Agents, email monitoring, unknown explicit sources, control commands, and existing manual model overrides are excluded. A separate gateway is supported only after its path through the native console pipeline is verified.

## Install and configure

1. Back up your instance and start with an isolated test environment.
2. In QwenPaw's native plugin manager, upload [`dist/qwenpaw-model-router-0.1.0.zip`](dist/qwenpaw-model-router-0.1.0.zip). Upload this plugin ZIP, **not** GitHub's automatically generated source ZIP.
3. Configure providers and execution models in QwenPaw. Give each target Agent a valid default model and enable context compression with `native` or `scroll`.
4. Open **模型路由** under Settings, or navigate to `/model-router`. Verify host compatibility.
5. Select the independent classifier, L1/L2/L3 models, and eligible Agents. A classifier can share an execution model, but the settings remain independent.
6. Save the configuration and use **测试判级**. This calls the classifier only, may incur provider charges, and does not run the business task or load real chat history.
7. For automatic observation, enable the plugin in `shadow` mode. After validating model/tool compatibility and quality, consider `active` and acknowledge its fallback limitation.

The classifier requests thinking to be disabled and disables retries on its private Agent-config copy. Whether thinking is actually disabled depends on the provider/model. No provider or Agent default-model settings are rewritten.

> **Important:** A successful `active` override bypasses QwenPaw's native cross-model fallback chain. Same-model retries remain host-controlled. Classification failures leave the original model selection in place. The plugin never resubmits a business request or repeats tools to recover from failure. A bypass is not an L3 classification.

## Difficulty policy

The versioned prompt chooses the lowest adequate tier:

| Tier | Intended tasks |
| --- | --- |
| L1 | Everyday Q&A, rewriting, translation, simple calculations, a single lookup, basic text extraction or transcription |
| L2 | Routine analysis, comparisons, summaries, ordinary scripts, local debugging, and bounded multi-step work |
| L3 | Clear evidence of very difficult reasoning, long-running stateful execution with adaptive planning, or large-scale complex data computation |

Images, attachments, long text, risk labels, professional terminology, or multiple file names do **not** automatically trigger L3. Missing task intent normally maps to L2. Every tier still requires appropriate permissions, approvals, and validation.

### Multimodal and context boundaries

- Classification uses task text and media **types**, not media contents. It does not open attachments, download URLs, or inspect images/audio/video.
- Media URLs, file names, base64 data, tool bodies, and hidden reasoning blocks are not added by media extraction. User-visible text is still part of the classifier payload.
- Actual execution input, including media, remains unchanged. Execution models must support the relevant input formats; image support does not imply audio, video, or arbitrary-file support.
- Classifier limits: 8,000 current-text characters, 2,000 summary characters, and 4,000 visible-text characters across the last two turns. Oversized current text uses the first and last 4,000 characters with an explicit truncation flag. This is not full-document understanding.
- Unknown content/session structures conservatively bypass routing. Legacy media blocks and AgentScope 2.0 `data`, `tool_call`, and `tool_result.output` are handled.
- Total classifier timeout defaults to 3 seconds, configurable from 1–10 seconds. At most 16 classifier tasks and 2 settings-page tests run concurrently per process. Cancellation propagates; late results cannot override a request. An uncooperative SDK background thread cannot be forcibly terminated.

Only strict JSON such as `{"level":2,"reason_code":"standard"}` is accepted. Extra fields, duplicate keys, non-integer levels, fenced text, and malformed JSON are rejected without a repair call.

## How it works

```text
Native console request → session_load → PRE_AGENT_BUILD
  → eligibility checks → bounded classifier payload → independent classifier
  → shadow: record recommendation
  → active: set request.model_slot_override
  → host model + formatter + context management + tool loop
```

The plugin does not modify `agent.model`, the global active model, host source, or installed packages. The host continues to own execution, SSE streaming, tool approval, and context compression.

Management endpoints inherit the host authentication and access policy:

| Method | Path | Purpose |
| --- | --- | --- |
| GET / PUT | `/api/model-router/config` | Read/save plugin settings |
| POST | `/api/model-router/classify` | Test `{"agent_id":"your-agent-id","text":"your task"}` |
| GET | `/api/model-router/status` | Compatibility and sanitized model catalog |
| GET | `/api/model-router/recent?limit=100` | Recent routing metadata |

The frontend passes `/model-router...` to `host.fetch`; the SDK adds `/api`. Do not add a second `/api` prefix.

## Privacy, persistence, and updates

Classifier requests send bounded user text/history to the configured classifier provider. Choose that provider according to your data-handling requirements. The plugin does not itself log prompts, task text, answers, reasoning, credentials, or raw provider exceptions.

Paths below are relative to the host working directory:

```text
plugins/qwenpaw-model-router/                 Plugin files
plugin-data/qwenpaw-model-router/config.json  Persisted settings
plugin-data/qwenpaw-model-router/routing.log  Routing metadata
```

Settings are saved atomically. Logs contain Agent/model identifiers, decisions, timings, fixed reason codes, and per-process salted session fingerprints; treat runtime logs as potentially sensitive. Rotation is 5 MB with three backups. Memory retains up to 500 records; the API exposes at most 100. Records are not proof of business success or final billing.

Disable the switch to bypass subsequent turns. Update/uninstall through QwenPaw's native plugin manager. Settings and logs are outside the plugin package; this plugin does not delete them during uninstall. Back up before lifecycle changes and verify host cleanup behavior. Keep host authentication enabled for exposed deployments; this plugin does not add an anonymous management entry point.

## Development and packaging

Use Python 3.11+ in a development environment. The core tests use the standard library; API/contract tests and the existing packager additionally import FastAPI and httpx:

```sh
python -m pip install "fastapi==0.141.1" "httpx==0.28.1"
python -B -m unittest discover -s . -p "test_*.py" -v
python -B test_host_contract.py --package
```

Run these commands from this repository root. No live provider calls occur during these tests. The packager rebuilds the plugin ZIP from a ten-file allowlist, including both READMEs; it does not include host data, credentials, caches, or the host source checkout.

For host-source contract checks, obtain the matching QwenPaw source separately and set `QWENPAW_SOURCE` to its root, or place it in `qwenpaw-source/`. Host code is not bundled here. Example using a sibling checkout:

```sh
export QWENPAW_SOURCE=../qwenpaw-source
python -B -m unittest discover -s . -p "test_*.py" -v
```

PowerShell equivalent:

```powershell
$env:QWENPAW_SOURCE = "../qwenpaw-source"
python -B -m unittest discover -s . -p "test_*.py" -v
```

The frontend contract check needs Node.js with `node:module.stripTypeScriptTypes` (tested on Node 24). Native message-object checks need AgentScope `2.0.7`. Missing source, Node, or AgentScope causes the related tests to skip; skipped checks are not passes.

### Test status and evaluation data

The operator reports that the first manual test passed. Development evidence also includes 25 local tests passing with one native-message check skipped; all 15 core tests, including that native-message check, passed in the baseline container. Eight independent classifier spot checks matched their expected tiers. These checks do not establish broad accuracy, cost savings, or universal model compatibility.

This sanitized snapshot was also checked offline: without the separate host source, 18 tests passed and 8 were skipped; with the matching host source, 25 passed and 1 was skipped because AgentScope was not installed locally. No container changes or live model calls were made for this publishing step.

[`evaluation.jsonl`](evaluation.jsonl) contains 60 synthetic, Chinese, **legacy v1-labeled** samples: 39 calibration and 21 holdout. Some old labels encode risk, ambiguity, or length as L3 and no longer match v2. Keep the dataset for reference; independently relabel it before using it for v2 acceptance. Do not change labels to fit predictions.

The existing score command accepts JSONL predictions with `id` and `level` (`null` for failures), plus optional `elapsed_ms`, `reason_code`, `input_tokens`, and `output_tokens`:

```sh
python -B test_host_contract.py --score predictions.jsonl
```

It scores against the labels currently in the dataset. Missing samples count against accuracy; unknown pricing is not converted into claimed cost savings. Full quality/cost evaluation, broad cross-provider execution, real-media execution, long-context compression, stop/cancel behavior, and lifecycle hook cleanup remain areas for further acceptance testing.

## Publishing this snapshot

Upload the **contents of this directory** as the GitHub repository root so this English README appears by default. Include the Chinese README and the sanitized plugin ZIP in `dist/`. Do not upload the parent development folder, runtime data, deployment scripts, chat histories, backups, or old packages.

This snapshot contains no installation-specific configuration or credentials. Documentation-range IPs, `.invalid`/`.test` domains, and fixed fake token strings in tests are deliberate offline fixtures, not live credentials. `.gitignore` helps prevent accidental additions but does not protect files dragged into GitHub's web upload; check the file list manually.

## License

Released under the [MIT License](LICENSE). Public availability alone is not an open-source license; this repository is published under the MIT terms above. QwenPaw and AgentScope retain their own licensing terms.
