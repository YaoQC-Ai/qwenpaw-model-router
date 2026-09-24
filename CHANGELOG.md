# 版本记录

## 0.1.0 — 2026-09-24

首次公开版本（公开脱敏快照）。

- QwenPaw 原生插件：为指定 Agent 的每个合格用户回合判定 L1/L2/L3，并以 `provider_id` + `model` 选择对应执行模型；分类器与三个执行模型槽位互相独立。
- 精确兼容基线：QwenPaw `2.2.0b1`、AgentScope `2.0.7`、Agent 后端 `qwenpaw`、上下文策略 `native` 或 `scroll`（需开启压缩）。清单声明 `>=2.2.0b1, <2.2.1`，运行时门禁更严格，只接受上述精确基线；不兼容时只暴露管理状态，不注册路由 Hook。
- 默认关闭。`shadow` 只记录推荐；`active` 施加请求级模型覆盖，并会绕过宿主原生跨模型回退链（同模型重试仍由宿主控制）。
- 发布内容：插件本体（`plugin.py`、`router.py`、`prompt.txt`、`plugin.json`、`ui/index.js`）、脱敏评测集 `evaluation.jsonl`（60 条合成样本，v1 旧标签）、测试（`test_router.py`、`test_host_contract.py`）、打包产物 `dist/qwenpaw-model-router-0.1.0.zip`（按十文件白名单由宿主原生插件管理器上传）。
- 许可证：本仓库采用 MIT（`LICENSE`）；QwenPaw 与 AgentScope 保留各自的许可条款。
- 验证状态：首次人工测试通过。离线契约测试 26 项（18 通过、8 跳过——缺少宿主源码、Node 或 AgentScope 时相关检查跳过）。本步骤未进行真实模型调用；不构成对判级准确率、成本收益或全模型兼容性的声明。跨 provider 执行、真实媒体、长上下文压缩、停止/取消行为与生命周期 Hook 清理的验收仍待进行。
