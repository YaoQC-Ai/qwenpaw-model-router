# QwenPaw Model Router

[English](README.md) | 简体中文

QwenPaw 原生任务难度路由插件：对符合条件的每个用户回合独立判级，再选择 L1、L2 或 L3 执行模型。复用已配置的 Provider 和模型，无需代理服务或修改宿主源码。

**插件版本：** `0.1.0` · **判级提示词：** [`router-zh-v2`](prompt.txt) · **状态：** 首轮人工测试已通过，更完整的验收仍在进行。

## 功能

- 独立判级模型和三个可分别配置的执行模型，通过 `provider_id` + `model` 引用。
- 每个符合条件的用户回合只判级一次，该回合的工具循环保持同一执行模型。
- 原生 `/model-router` 设置页：总开关、Agent 选择、模型选择、超时、判级测试和最近路由记录。
- `shadow` 只记录建议；`active` 设置请求级模型覆盖。
- 默认关闭，不预置模型、凭据或 Agent ID。
- 不增加插件运行时依赖，不需要前端构建；界面使用宿主 React/antd SDK。

当前界面、判级提示词和合成评测样例为中文。提供英文 README 不代表已有英文界面或完成多语言质量验证。

## 兼容性

| 组件 | 支持／已核对的基线 |
| --- | --- |
| QwenPaw | 精确版本 `2.2.0b1` |
| AgentScope | 精确版本 `2.0.7` |
| Agent 后端 | `qwenpaw` |
| 上下文策略 | `native` 或 `scroll`，且开启压缩 |
| 请求入口 | 指定 Agent 的原生 `console` 用户请求 |

插件清单声明 `>=2.2.0b1, <2.2.1`，但运行时检查更严格：**只接受上述精确基线**。不承诺其他版本兼容。不兼容时仍提供管理状态，但不注册路由 Hook。

初步容器验证使用 Linux/Debian，尚未完成 Ubuntu 和 Windows 完整宿主验收。心跳、cron、子 Agent、邮件监控、未知显式来源、控制命令和已有人工模型覆盖均不接管。独立网关须另行验证其是否经过原生 console 链路。

## 安装与配置

1. 备份实例，先在隔离测试环境使用。
2. 在 QwenPaw 原生插件管理器中上传 [`dist/qwenpaw-model-router-0.1.0.zip`](dist/qwenpaw-model-router-0.1.0.zip)。应上传这个插件安装包，**不要**上传 GitHub 自动生成的源码 ZIP。
3. 在 QwenPaw 中配置 Provider 和执行模型。为目标 Agent 配置有效默认模型，并开启 `native` 或 `scroll` 上下文压缩。
4. 打开设置中的 **模型路由**，或访问 `/model-router`，确认宿主兼容。
5. 选择独立判级模型、L1/L2/L3 执行模型和参与路由的 Agent。判级可以与某档执行共用模型，但选择器彼此独立。
6. 保存配置后使用 **测试判级**。该功能仅调用判级模型，可能产生 Provider 费用；不执行业务任务，不加载真实聊天历史。
7. 如需自动观察，开启插件并使用 `shadow`。验证模型与工具兼容性及质量后，再考虑 `active`，并确认其 fallback 限制。

判级调用会请求关闭思考，并在私有 Agent 配置副本中关闭重试。思考是否实际关闭取决于 Provider／模型支持。不改写 Provider 配置或 Agent 默认模型。

> **重要：** 成功的 `active` 覆盖会跳过 QwenPaw 原生跨模型 fallback 链。同模型重试仍由宿主控制。判级失败时沿用宿主原模型选择。插件不会通过重新发送业务请求或重复执行工具来恢复失败。旁路不等于判为 L3。

## 难度标准

版本化提示词选择足够胜任的最低等级：

| 等级 | 适用任务 |
| --- | --- |
| L1 | 日常问答、改写、翻译、简单计算、单次查询、简单文字提取或转写 |
| L2 | 常规分析、比较、汇总、普通脚本、局部排错及有限步骤工作 |
| L3 | 明确需要最难的深层推理、带自适应计划和持续状态维护的长程执行，或大规模复杂数据计算 |

图片、附件、长文本、风险标签、专业术语或多个文件名**不会自动触发 L3**。任务目标不明通常建议 L2。任何等级都必须遵守相应权限、审批和结果校验要求。

### 多模态与上下文边界

- 判级使用任务文字和媒体**类型**，不读取媒体正文，不打开附件、下载 URL 或分析图片／音频／视频。
- 媒体提取不会添加媒体 URL、文件名、base64、工具正文或隐藏思考块；用户可见文字仍会进入判级输入。
- 真实执行输入及其中的媒体保持不变。执行模型必须支持对应格式；支持图片不代表同时支持音频、视频或任意文件。
- 判级输入上限：当前文字 8,000 字符、摘要 2,000 字符、最近两轮可见文字合计 4,000 字符。超长当前文字取首尾各 4,000 字符，并明确标记截断；不等于理解完整文档。
- 未知内容／会话结构保守旁路。兼容旧媒体块及 AgentScope 2.0 的 `data`、`tool_call` 和 `tool_result.output`。
- 判级总超时默认 3 秒，可配置为 1–10 秒。每进程最多 16 个判级任务，设置页测试最多 2 并发。取消向上传播，迟到结果不覆盖请求；无法强杀不配合取消的 SDK 后台线程。

仅接受严格 JSON，例如 `{"level":2,"reason_code":"standard"}`。额外字段、重复键、非整数等级、Markdown 围栏及非法 JSON 会被拒绝，不增加修复调用。

## 工作原理

```text
原生 console 请求 → session_load → PRE_AGENT_BUILD
  → 适用条件检查 → 有界判级输入 → 独立判级
  → shadow：记录建议
  → active：设置 request.model_slot_override
  → 宿主模型 + formatter + 上下文管理 + 工具循环
```

插件不修改 `agent.model`、全局 active model、宿主源码或已安装包。业务执行、SSE 流式输出、工具审批和上下文压缩继续由宿主负责。

管理接口继承宿主认证与访问策略：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET / PUT | `/api/model-router/config` | 读取／保存插件配置 |
| POST | `/api/model-router/classify` | 测试 `{"agent_id":"your-agent-id","text":"your task"}` |
| GET | `/api/model-router/status` | 兼容状态和安全投影的模型目录 |
| GET | `/api/model-router/recent?limit=100` | 最近路由元数据 |

前端向 `host.fetch` 传入 `/model-router...`，SDK 自动补 `/api`。不要再添加第二个 `/api` 前缀。

## 隐私、持久化与更新

判级请求会把有界的用户文字／历史发送给所选判级 Provider，请按数据处理要求选择 Provider。插件自身不记录提示词、任务正文、回答、思考、凭据或 Provider 原始异常。

以下路径均相对于宿主工作目录：

```text
plugins/qwenpaw-model-router/                 插件文件
plugin-data/qwenpaw-model-router/config.json  持久化配置
plugin-data/qwenpaw-model-router/routing.log  路由元数据
```

配置采用原子写入。日志包含 Agent／模型标识、判定结果、耗时、固定原因码及进程级加盐会话指纹；运行日志仍应视为可能敏感。日志按 5 MB 轮转，保留三个备份。内存最多保存 500 条，API 最多返回 100 条。路由记录不代表业务执行成功或最终计费。

关闭总开关会让后续回合旁路。通过 QwenPaw 原生插件管理器更新或卸载。配置与日志位于插件包之外，插件不会在卸载时删除它们。生命周期操作前应备份并验证宿主清理行为。对外可访问的部署应保持宿主认证开启；插件不增加匿名管理入口。

## 开发与打包

开发环境使用 Python 3.11+。核心测试仅依赖标准库；API／契约测试和现有打包工具还需导入 FastAPI 与 httpx：

```sh
python -m pip install "fastapi==0.141.1" "httpx==0.28.1"
python -B -m unittest discover -s . -p "test_*.py" -v
python -B test_host_contract.py --package
```

请在本仓库根目录运行命令。测试不会调用真实 Provider。打包器按十文件白名单重新生成插件 ZIP，包含中英文 README，不包含宿主数据、凭据、缓存或宿主源码副本。

如需宿主源码契约检查，请另行取得匹配版本的 QwenPaw 源码，用 `QWENPAW_SOURCE` 指定根目录，或放入 `qwenpaw-source/`。本仓库不捆绑宿主源码。使用同级源码目录的示例：

```sh
export QWENPAW_SOURCE=../qwenpaw-source
python -B -m unittest discover -s . -p "test_*.py" -v
```

PowerShell 对应命令：

```powershell
$env:QWENPAW_SOURCE = "../qwenpaw-source"
python -B -m unittest discover -s . -p "test_*.py" -v
```

前端契约检查需要提供 `node:module.stripTypeScriptTypes` 的 Node.js，已使用 Node 24 测试。原生消息对象检查需要 AgentScope `2.0.7`。缺少源码、Node 或 AgentScope 时会跳过相关检查；跳过不算通过。

### 测试状态与评测数据

使用者反馈首轮人工测试通过。开发验证还包括：本地 25 项测试通过，1 项原生消息检查跳过；基线容器全部 15 项核心测试通过，包括该原生消息检查；8 条独立判级抽测符合预期。这些检查不能证明广泛准确率、成本收益或任意模型兼容性。

本次脱敏副本另行完成离线检查：不提供独立宿主源码时，18 项通过、8 项跳过；提供匹配宿主源码时，25 项通过、1 项因本机未安装 AgentScope 而跳过。本次发布准备未修改容器，也未调用真实模型。

[`evaluation.jsonl`](evaluation.jsonl) 包含 60 条合成中文样例，使用**旧 v1 标签**：39 条 calibration、21 条 holdout。部分旧标签把风险、信息不明或长度作为 L3 依据，不再符合 v2。该数据集保留供参考，用于 v2 正式验收前必须独立重新标注，不应让标签迎合模型输出。

现有评分命令读取 JSONL 预测结果，必需字段为 `id`、`level`（失败用 `null`）；可选 `elapsed_ms`、`reason_code`、`input_tokens` 和 `output_tokens`：

```sh
python -B test_host_contract.py --score predictions.jsonl
```

评分使用数据集当前标签。缺少预测的样例仍计入准确率分母；未知价格不会被换算为宣称的成本收益。完整质量／成本评测、广泛跨 Provider 执行、真实媒体执行、长上下文压缩、停止／取消及生命周期 Hook 清理仍需进一步验收。

## 上传此公开副本

将**本目录内的内容**作为 GitHub 仓库根目录上传，默认展示英文 README。请包含中文 README 和 `dist/` 内的脱敏插件安装包。不要上传上级开发目录、运行数据、部署脚本、聊天历史、备份或旧安装包。

此副本不含实例专属配置或凭据。测试中的文档示例 IP、`.invalid`／`.test` 域名及固定假 token 都是离线测试数据，不是真实凭据。`.gitignore` 有助于避免误加入文件，但无法保护直接拖入 GitHub 网页上传的内容，上传前仍须检查文件清单。

## 许可证

采用 [MIT 许可证](LICENSE) 发布。公开可见本身不等于拥有开源许可，本仓库以上述 MIT 条款发布。QwenPaw 和 AgentScope 保留各自的许可条款。
