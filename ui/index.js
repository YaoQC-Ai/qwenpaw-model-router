(() => {
  "use strict";
  const sdk = window.QwenPaw;
  if (!sdk?.host?.React || !sdk.host.antd || !sdk.host.fetch) return;
  const { React: R, antd: A } = sdk.host;
  const h = R.createElement;
  const id = "qwenpaw-model-router";
  const routeId = `${id}.settings`;
  const reasonNames = {
    disabled: "插件已关闭", agent_not_enabled: "Agent 未启用", explicit_override: "已有人工模型覆盖",
    automation: "自动化请求", subagent: "子 Agent", unknown_source: "未知来源",
    unverified_channel: "入口尚未验收", multimodal: "旧版多模态旁路（未判级）", unsupported_content: "未知内容结构，沿用默认模型", compression_disabled: "上下文压缩已关闭",
    classifier_timeout: "判级超时", invalid_output: "判级输出不合法", invalid_json: "JSON 不合法",
    model_missing: "模型已删除", model_unavailable: "模型当前不可用", provider_missing: "Provider 不存在",
    incompatible_host: "宿主版本或契约不兼容", unsupported_backend: "非 QwenPaw 后端",
    default_model_required: "须先配置原默认模型", catalog_unavailable: "模型目录读取失败",
    models_required: "请配齐四个模型", agents_required: "请选择至少一个 Agent",
    test_busy: "测试并发已满", classifier_busy: "判级并发已满", classifier_or_host_error: "判级或宿主调用失败",
    config_unreadable: "配置文件无法读取", config_write_failed: "配置保存失败",
    invalid_test_input: "测试文本须为 1–16000 字符", input_too_long: "旧版长文本保护规则",
    simple: "轻量", standard: "标准", complex: "复杂", continuation: "多轮续问",
    insufficient_context: "任务信息不足", high_risk: "高风险（不等于高难度）",
  };
  const explain = code => reasonNames[code] || code;
  const slotKey = slot => slot?.model ? JSON.stringify([slot.provider_id, slot.model]) : undefined;
  const parseSlot = key => {
    if (!key) return { provider_id: "", model: "" };
    const [provider_id, model] = JSON.parse(key);
    return { provider_id, model };
  };
  async function request(path, init) {
    const response = await sdk.host.fetch(`/model-router${path}`, init);
    const data = await response.json();
    if (!response.ok) throw new Error(explain(data.error || "请求失败"));
    return data;
  }
  const jsonBody = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  function Settings() {
    const [config, setConfig] = R.useState(null);
    const [status, setStatus] = R.useState(null);
    const [records, setRecords] = R.useState([]);
    const [error, setError] = R.useState("");
    const [notice, setNotice] = R.useState("");
    const [busy, setBusy] = R.useState(false);
    const [text, setText] = R.useState("");
    const [agent, setAgent] = R.useState(undefined);
    const [test, setTest] = R.useState(null);
    const [testing, setTesting] = R.useState(false);
    const [ack, setAck] = R.useState(false);
    const alive = R.useRef(true);

    R.useEffect(() => {
      alive.current = true;
      const controller = new AbortController();
      Promise.all([request("/config", { signal: controller.signal }), request("/status", { signal: controller.signal }),
        request("/recent", { signal: controller.signal })]).then(([c, s, r]) => {
        if (!alive.current) return;
        setConfig(c); setStatus(s); setRecords(r.records); setAgent(c.enabled_agents[0]);
      }).catch(e => { if (alive.current && e.name !== "AbortError") setError(e.message); });
      return () => { alive.current = false; controller.abort(); };
    }, []);

    async function save() {
      setBusy(true); setError(""); setNotice("");
      try {
        const saved = await request("/config", jsonBody("PUT", config));
        if (alive.current) { setConfig(saved); setNotice("已保存，仅影响后续用户回合。"); }
      } catch (e) { if (alive.current) setError(e.message); }
      finally { if (alive.current) setBusy(false); }
    }
    async function classify() {
      setTesting(true); setError(""); setTest(null);
      try {
        const data = await request("/classify", jsonBody("POST", { agent_id: agent, text }));
        if (alive.current) setTest(data);
      } catch (e) { if (alive.current) setError(e.message); }
      finally { if (alive.current) setTesting(false); }
    }
    async function refresh() {
      try {
        const [s, r] = await Promise.all([request("/status"), request("/recent")]);
        if (alive.current) { setStatus(s); setRecords(r.records); }
      } catch (e) { if (alive.current) setError(e.message); }
    }
    const field = (label, control) => h("div", { style: { marginBottom: 16 } }, h("div", { style: { marginBottom: 6 } }, label), control);
    if (!config || !status) return h("div", { style: { padding: 24 } }, error ? h(A.Alert, { type: "error", message: error }) : h(A.Spin));
    const models = status.models.map(m => ({
      value: slotKey(m),
      label: `${m.provider_name} / ${m.model} · ${m.availability || "unverified"} · ${m.context_window || "未知"} tokens`,
      disabled: ["permission_denied", "model_not_found", "incompatible_api", "rate_limited", "transient_error"].includes(m.availability),
    }));
    const agents = status.agents.map(a => ({ value: a.id, label: `${a.name} (${a.id})${a.bypass_reason ? ` — ${explain(a.bypass_reason)}` : ""}`, disabled: !!a.bypass_reason }));
    const modelSelector = (label, slot, update) => field(label, h(A.Select, {
      "aria-label": label, value: slotKey(slot), options: models, showSearch: true, optionFilterProp: "label", allowClear: true,
      style: { width: "100%" }, onChange: value => update(parseSlot(value)),
    }));
    const needAck = config.enabled && config.mode === "active";
    const columns = [
      { title: "时间", dataIndex: "time", render: v => new Date(v).toLocaleString() },
      { title: "Agent", dataIndex: "agent_id" }, { title: "会话（脱敏）", dataIndex: "session" },
      { title: "等级", dataIndex: "level", render: v => v ? `L${v}` : "—" },
      { title: "建议目标", dataIndex: "target", render: v => v ? `${v.provider_id} / ${v.model}` : "—" },
      { title: "耗时 ms", dataIndex: "elapsed_ms" }, { title: "模式", dataIndex: "mode" },
      { title: "结果", dataIndex: "outcome", render: v => ({ bypass: "旁路：沿用原模型", selected: "已选择", shadow: "仅建议", cancelled: "已取消" }[v] || v) },
      { title: "原因", dataIndex: "reason_code", render: explain },
    ];
    return h("main", { style: { padding: 24, maxWidth: 1400, margin: "0 auto" } },
      h("h1", null, "任务难度模型路由"),
      error && h(A.Alert, { type: "error", message: error, showIcon: true, style: { marginBottom: 12 } }),
      notice && h(A.Alert, { type: "success", message: notice, style: { marginBottom: 12 } }),
      h(A.Alert, { type: "warning", showIcon: true, message: "先 shadow 验证，再按需开启 active", description:
        "成功路由会跳过宿主跨模型 fallback 链，仅保留同模型重试。判级失败沿用原默认模型，不代表判为 L3。仅指定 Agent 的 console 用户入口参与；自动化和关闭压缩的 Agent 旁路。多模态按文字任务与媒体类型判级，不读取媒体正文；执行模型须支持相应输入。", style: { marginBottom: 16 } }),
      h("p", null, `插件 ${status.plugin_version} · 提示词 ${status.prompt_version} · QwenPaw ${status.qwenpaw_version} · AgentScope ${status.agentscope_version}`),
      h("p", null, "L1：日常轻量任务；L2：常规分析与有限步骤；L3：仅高复杂度推理、长程执行或大规模复杂计算。图片、附件、长文本和风险标签本身不升级。"),
      !status.compatible && h(A.Alert, { type: "error", message: "宿主不兼容：自动路由已旁路，请核对版本，不要修改生产宿主。" }),
      status.catalog_error && h(A.Alert, { type: "error", message: explain(status.catalog_error) }),
      status.log_write_failed && h(A.Alert, { type: "error", message: "路由日志写入失败，请检查持久目录权限或磁盘。" }),
      field("总开关", h(A.Switch, { "aria-label": "总开关", checked: config.enabled, onChange: enabled => { setConfig({ ...config, enabled }); setAck(false); } })),
      field("工作模式", h(A.Radio.Group, { value: config.mode, onChange: e => { setConfig({ ...config, mode: e.target.value }); setAck(false); }, options: [{ label: "shadow：只记录建议", value: "shadow" }, { label: "active：覆盖本回合模型", value: "active" }] })),
      field("启用的 Agent", h(A.Select, { "aria-label": "启用的 Agent", mode: "multiple", value: config.enabled_agents, options: agents, style: { width: "100%" }, onChange: enabled_agents => setConfig({ ...config, enabled_agents }) })),
      modelSelector("独立判级模型（调用时请求关闭思考）", config.classifier, classifier => setConfig({ ...config, classifier })),
      ...[1, 2, 3].map(level => h("div", { key: level }, modelSelector(`L${level} 执行模型`, config.tiers[level], slot => setConfig({ ...config, tiers: { ...config.tiers, [level]: slot } })))),
      field("判级总超时（秒，1–10）", h(A.InputNumber, { "aria-label": "判级总超时", min: 1, max: 10, step: 0.5, value: config.classifier_timeout_seconds, onChange: classifier_timeout_seconds => setConfig({ ...config, classifier_timeout_seconds }) })),
      needAck && field("active 确认", h(A.Checkbox, { checked: ack, onChange: e => setAck(e.target.checked) }, "我已了解跨模型 fallback 限制，并已完成目标模型工具调用、历史压缩和小样本质量验收。")),
      h(A.Button, { type: "primary", loading: busy, disabled: (needAck && !ack) || (config.enabled && !status.compatible), onClick: save }, "保存配置"),
      h("hr"), h("h2", null, "测试判级"),
      h("p", null, "使用已保存配置，只做独立判级，不执行业务任务、不保存聊天。会产生模型调用费用；不自动传入真实会话历史。"),
      field("测试 Agent", h(A.Select, { "aria-label": "测试 Agent", value: agent, options: agents, style: { width: "100%" }, onChange: setAgent })),
      field("测试文本", h(A.Input.TextArea, { "aria-label": "测试文本", value: text, rows: 4, maxLength: 16000, showCount: true, onChange: e => setText(e.target.value) })),
      h(A.Button, { loading: testing, disabled: !agent || !text.trim() || !status.compatible, onClick: classify }, "调用判级模型（可能付费）"),
      test && h("p", { role: "status" }, `L${test.level} · ${test.target.provider_id} / ${test.target.model} · ${explain(test.reason_code)} · ${test.elapsed_ms} ms`),
      h("hr"), h("h2", null, "最近路由记录"),
      h("p", null, "仅代表选中/建议目标，不代表业务执行成功或最终计费。最近记录保留在内存，重启后清空；磁盘日志滚动保留。"),
      h(A.Button, { onClick: refresh }, "刷新记录与模型目录"),
      h(A.Table, { dataSource: records.map((r, i) => ({ ...r, key: i })), columns, size: "small", pagination: { pageSize: 10 }, scroll: { x: 1100 } }),
      h("small", null, status.verification),
    );
  }
  sdk.route.add(id, { id: routeId, path: "/model-router", component: Settings });
  sdk.menu.add(id, { id: `${id}.menu`, label: "模型路由", route: routeId, location: "primary.settings", order: 80 });
})();
