/* Server values stay text, including untrusted tool output. */
"use strict";
const $ = id => document.getElementById(id);
const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const money = cents => cents == null ? "—" : `¥${Math.trunc(cents / 100).toLocaleString("zh-CN")}.${String(Math.abs(cents % 100)).padStart(2, "0")}`;
const json = value => JSON.stringify(value, null, 2);
const orderStatus = {pending_payment:"待支付", pending_shipment:"待发货", shipped:"已发货", delivered:"已签收", completed:"已完成", aftersale:"售后中", cancelled:"已取消"};
let state = null, busy = false, liveEvents = [];

function mergeTraces(previous, next) {
  const merged = new Map([...previous, ...next].map(event => [event.seq, event]));
  return [...merged.values()].sort((left, right) => left.seq - right.seq);
}

function notify(text) {
  $("status").textContent = text;
  $("status").style.display = "block";
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:json(body)
  });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : json(data.detail));
  return data;
}
async function task(action) {
  if (busy) return;
  busy = true;
  $("status").style.display = "none";
  document.body.setAttribute("aria-busy", "true");
  try { await action(); }
  catch (error) { notify(error.message); }
  finally {
    busy = false;
    document.body.removeAttribute("aria-busy");
  }
}
function setState(session) {
  if (state?.session_id === session.session_id) {
    session.trace_events = mergeTraces(state.trace_events, session.trace_events);
  }
  state = session;
  localStorage.setItem("aftersales-session", session.session_id);
  render();
}
async function restoreSession(sessionId) {
  let cursor = state?.session_id === sessionId ? (state.trace_events.at(-1)?.seq ?? 0) : 0;
  setState(await api(`/api/sessions/${sessionId}`));
  const history = [];
  let more;
  do {
    const page = await api(`/api/sessions/${sessionId}/traces?after_seq=${cursor}`);
    history.push(...page.events);
    cursor = page.next_seq;
    more = page.has_more;
  } while (more);
  if (state?.session_id === sessionId) {
    state.trace_events = mergeTraces(state.trace_events, history);
    renderOutcomes();
    audit();
  }
}

async function streamMessage(sessionId, content) {
  const response = await fetch(`/api/sessions/${sessionId}/messages/stream`, {
    method:"POST", headers:{"Content-Type":"application/json"}, body:json({content})
  });
  if (!response.ok) {
    const data = await response.json();
    throw Error(typeof data.detail === "string" ? data.detail : json(data.detail));
  }
  const messages = $("messages"), preview = el("div", "", "bubble assistant");
  const buyer = el("div", content, "bubble user");
  buyer.setAttribute("aria-label", "买家");
  preview.setAttribute("aria-label", "售后管家正在回复");
  messages.append(buyer, preview);
  messages.scrollTop = messages.scrollHeight;
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "", result, failure;
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += value;
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let event = "message";
        const payload = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) event = line.slice(6).replace(/^ /, "");
          if (line.startsWith("data:")) payload.push(line.slice(5).replace(/^ /, ""));
        }
        if (!payload.length) continue;
        const data = JSON.parse(payload.join("\n"));
        if (event === "assistant_delta") {
          if (data.replace) preview.textContent = "";
          preview.textContent += data.text;
          messages.scrollTop = messages.scrollHeight;
        } else if (event === "turn_end") {
          result = data;
        } else if (event === "error") {
          failure = typeof data.message === "string" ? data.message : data.message.message;
        } else if (["tool_call", "guard_verdict", "confirm_verdict", "tool_result", "proposal_created"].includes(event)) {
          if (event === "tool_call") preview.textContent = "";
          liveEvents.push({event, data});
          audit();
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    buyer.remove();
    preview.remove();
    liveEvents = [];
    audit();
  }
  if (!result) throw Error(failure || "连接已中断，请刷新查看已处理的结果。");
  setState(result.session);
  if (failure) notify(failure);
}

function detail(parent, title, value) {
  const node = el("details");
  node.append(el("summary", title), el("pre", json(value)));
  parent.append(node);
}
function table(headers, className) {
  const node = el("table", undefined, className), head = el("tr"), body = el("tbody");
  headers.forEach(header => head.append(el("th", header)));
  const thead = el("thead");
  thead.append(head);
  node.append(thead, body);
  return [node, body];
}
function renderOrders() {
  $("user-label").textContent = state.user_name || "未认证";
  $("orders").replaceChildren();
  state.orders.forEach(snapshot => {
    const order = snapshot.orders[0], card = el("div", undefined, "order");
    const header = el("div", undefined, "order-head");
    header.append(el("strong", order.id, "record-id"), el("span", orderStatus[order.status] || order.status, "order-status"));
    const [items, rows] = table(["商品行 / SKU", "件数", "券后实付", "已退"], "items");
    snapshot.order_items.forEach(item => {
      const row = el("tr");
      row.append(el("td", `${item.id} / ${item.sku_id}`, "record-id"), el("td", item.qty, "num"), el("td", money(item.paid_share_cents), "num"), el("td", item.is_partially_returned ? `${money(item.refunded_cents)} · 部分退货` : money(item.refunded_cents), "num"));
      rows.append(row);
    });
    card.append(header, items);
    detail(card, "详情", {address:order.address, aftersales:snapshot.aftersales, refunds:snapshot.refunds});
    $("orders").append(card);
  });
  if (!state.orders.length) $("orders").append(el("p", "暂无订单", "empty"));
}
function renderProposals() {
  $("proposals").replaceChildren();
  state.proposals.filter(proposal => proposal.status === "pending").forEach(proposal => {
    const card = el("section", undefined, "proposal");
    card.append(el("h2", `${proposal.proposal_id} · v${proposal.version}`, "record-id"));
    proposal.actions.forEach(action => {
      const row = el("div", undefined, "action"), args = action.canonical_args;
      const heading = el("div", undefined, "action-head");
      heading.append(el("strong", `${action.tool_name} · ${args.order_id || args.user_id}`));
      if (action.preview_amount_cents !== null) heading.append(el("span", money(action.preview_amount_cents), "proposal-amount num"));
      row.append(heading);
      if (action.requires_approval) row.append(el("div", "P08 · 需人工审批", "notice approval"));
      if (args.address) row.append(el("div", `${args.address.receiver_name} / ${args.address.phone} · ${args.address.province}${args.address.city}${args.address.district}${args.address.detail}`, "line-item"));
      if (args.items) row.append(el("div", args.items.map(item => `${item.order_item_id || item.sku_id} ×${item.qty}`).join(" / "), "line-item"));
      if (args.order_item_id) row.append(el("div", `${args.order_item_id}${args.new_sku_id ? " → " + args.new_sku_id : ""}`, "line-item"));
      detail(row, "参数", args);
      card.append(row);
    });
    const buttons = el("div", undefined, "button-row");
    [["确认执行", "approve"], ["暂不执行", "reject"]].forEach(([label, decision]) => {
      const button = el("button", label, decision === "reject" ? "danger" : "");
      button.onclick = () => task(async () => setState(await api(`/api/sessions/${state.session_id}/proposals/${proposal.proposal_id}/decision`, {version:proposal.version, decision})));
      buttons.append(button);
    });
    const modify = el("button", "修改方案", "quiet");
    modify.onclick = () => {
      $("message").value = proposal.actions.some(action => action.tool_name === "modify_address") ? "只改地址，先不退差" : "修改方案：";
      $("message").focus();
    };
    buttons.append(modify);
    card.append(buttons);
    $("proposals").append(card);
  });
}
function renderOutcomes() {
  $("outcomes").replaceChildren();
  state.action_executions.forEach(execution => {
    const parts = ["已执行", execution.tool_name];
    if (execution.tool_name !== "modify_address") parts.push(money(execution.result.amount_cents));
    parts.push(...execution.result.record_ids);
    $("outcomes").append(el("div", parts.join(" · "), "notice"));
  });
  state.approvals.forEach(approval => $("outcomes").append(el("div", `${approval.id} · ${approval.order_id} · ${approval.status === "pending" ? "待审批" : approval.status}`, "notice approval")));
  state.trace_events.filter(event => ["guard_verdict", "confirm_verdict"].includes(event.event) && event.data.allowed === false && !event.data.audit_only).slice(-6).forEach(event => {
    $("outcomes").append(el("div", `${(event.data.rule_ids || []).join(" / ")} · ${event.data.reason || "未授权"}`, "notice blocked"));
  });
}
function render() {
  const demo = state.demo;
  $("demo-label").textContent = demo ? `${demo.step} / ${demo.total}` : "";
  $("advance").disabled = $("run-all").disabled = !demo || demo.step >= demo.total;
  document.querySelectorAll("[data-scene]").forEach(button => {
    const selected = Number(button.dataset.scene) === demo?.scenario;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  renderOrders();
  renderProposals();
  renderOutcomes();
  $("messages").replaceChildren();
  state.messages.filter(message => message.role !== "tool" && message.content && !message.tool_calls.length && !message.content.startsWith("[离线演示]")).forEach(message => {
    const bubble = el("div", message.content, `bubble ${message.role}`);
    bubble.setAttribute("aria-label", message.role === "user" ? "买家" : "售后管家");
    $("messages").append(bubble);
  });
  $("messages").scrollTop = $("messages").scrollHeight;
  audit();
}
function audit() {
  const events = [...(state?.trace_events || []), ...liveEvents], proposals = state?.proposals || [], executions = state?.action_executions || [];
  const blocked = events.filter(event => event.data.allowed === false && !event.data.audit_only).length;
  const tokens = events.filter(event => event.event === "llm_call").reduce((sum, event) => sum + (event.data.tokens?.input_tokens || 0) + (event.data.tokens?.output_tokens || 0), 0);
  $("live-metrics").replaceChildren();
  [[state ? executions.length : "—", "已执行"], [state ? blocked : "—", "已拦截"], [state ? tokens.toLocaleString() : "—", "Token"]].forEach(([value, label]) => {
    const metric = el("div", undefined, "metric");
    metric.append(el("span", label), el("strong", value, "num"));
    $("live-metrics").append(metric);
  });
  const [binding, rows] = table(["方案", "授权", "执行"], "binding-table");
  proposals.forEach(proposal => proposal.actions.forEach(action => {
    const row = el("tr"), intent = el("td"), consent = el("td"), actual = el("td");
    intent.append(el("strong", `${proposal.proposal_id} v${proposal.version} / ${action.action_id}`, "record-id"), el("div", action.tool_name));
    if (action.preview_amount_cents !== null) intent.append(el("div", money(action.preview_amount_cents), "num"));
    detail(intent, "参数", action.canonical_args);
    consent.append(el("div", proposal.confirm_source || "未确认"), el("div", `${proposal.status} / ${action.status}`, "muted"));
    const verdicts = events.filter(event => ["confirm_verdict", "guard_verdict"].includes(event.event) && event.data.proposal_id === proposal.proposal_id && (!event.data.action_id || event.data.action_id === action.action_id));
    detail(consent, "校验记录", verdicts.map(event => ({event:event.event, ...event.data})));
    const execution = executions.find(record => record.proposal_id === proposal.proposal_id && record.action_id === action.action_id);
    if (execution) {
      actual.append(el("strong", "已执行"));
      detail(actual, "执行记录", {execution_key:execution.execution_key, args_hash:execution.args_hash, canonical_args:execution.canonical_args, result:execution.result});
    } else actual.append(el("span", "未执行", "muted"));
    row.append(intent, consent, actual);
    rows.append(row);
  }));
  if (!proposals.length) {
    const row = el("tr"), cell = el("td", "暂无方案", "empty");
    cell.colSpan = 3;
    row.append(cell);
    rows.append(row);
  }
  $("binding").replaceChildren(binding);
  $("snapshots").replaceChildren();
  executions.forEach(execution => {
    const node = el("details", undefined, "snapshot");
    node.append(el("summary", `${execution.proposal_id}/${execution.action_id} · ${execution.tool_name}`));
    const [diff, diffRows] = table(["业务字段", "Before", "After"], "diff-table");
    changes(execution.before_snapshot, execution.after_snapshot).forEach(change => {
      const row = el("tr");
      row.append(el("td", change.path), el("td", json(change.before), "removed"), el("td", json(change.after), "added"));
      diffRows.append(row);
    });
    node.append(diff);
    detail(node, "原始快照", {before:execution.before_snapshot, after:execution.after_snapshot});
    $("snapshots").append(node);
  });
  if (!executions.length) $("snapshots").append(el("p", "暂无执行记录", "empty"));
  $("timeline").replaceChildren();
  events.forEach(event => {
    const data = event.data, denied = data.allowed === false && !data.audit_only;
    const node = el("details", undefined, `event ${denied ? "blocked" : data.permission === "write" ? "write" : ""}`);
    const summary = el("summary");
    summary.append(el("time", event.timestamp?.slice(11, 19) || ""), el("strong", event.event), el("span", data.name || data.status || ""));
    if (event.event === "llm_call") {
      const cached = data.tokens?.cached_tokens;
      summary.append(el("span", `${data.tokens?.input_tokens || 0} in / ${data.tokens?.output_tokens || 0} out${cached ? " · 缓存 " + cached : ""} · ${data.latency_ms}ms`, "muted num"));
    }
    node.append(summary, el("pre", json(data)));
    $("timeline").append(node);
  });
  if (!events.length) $("timeline").append(el("p", "暂无调用记录", "empty"));
}
function changes(before, after) {
  const result = [];
  function walk(a, b, path) {
    if (JSON.stringify(a) === JSON.stringify(b)) return;
    if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a) && !Array.isArray(b)) {
      new Set([...Object.keys(a), ...Object.keys(b)]).forEach(key => walk(a[key], b[key], `${path}.${key}`));
    } else result.push({path, before:a ?? null, after:b ?? null});
  }
  new Set([...Object.keys(before), ...Object.keys(after)]).forEach(name => {
    const key = row => row.id || row.order_id || row.sku_id;
    const a = new Map((before[name] || []).map(row => [key(row), row]));
    const b = new Map((after[name] || []).map(row => [key(row), row]));
    new Set([...a.keys(), ...b.keys()]).forEach(id => walk(a.get(id), b.get(id), `${name}[${id}]`));
  });
  return result;
}
async function loadReports() {
  const entries = await api("/api/evals/reports");
  const reports = await Promise.all(entries.map(entry => api(`/api/evals/reports/${entry.run_id}`).catch(() => ({run_id:entry.run_id, metrics:{}, trials:[]}))));
  const [node, rows] = table(["运行 / 确认门", "通过率", "未确认写入 / 拦截", "Token / USD"], "reports-table");
  reports.forEach(report => {
    const row = el("tr"), identity = el("td"), metrics = report.metrics || {};
    const open = el("button", report.run_id.slice(0, 10), "quiet record-id");
    open.onclick = () => showReport(report);
    const mode = report.config?.proposal_gate;
    identity.append(open, el("div", mode === true ? "Proposal" : mode === false ? "文本确认" : "—", "muted"));
    row.append(identity, el("td", metrics["pass^1"] === undefined ? "—" : `${(metrics["pass^1"] * 100).toFixed(1)}%`, "num"), el("td", `${metrics.unconfirmed_writes ?? "—"} / ${(metrics.guard_blocks || 0) + (metrics.confirmation_blocks || 0)}`, "num"), el("td", `${((metrics.input_tokens || 0) + (metrics.output_tokens || 0)).toLocaleString()} / $${metrics.cost_usd ?? "—"}`, "num"));
    rows.append(row);
  });
  $("reports").replaceChildren(node);
  if (!entries.length) $("reports").append(el("p", "暂无报告", "empty"));
}
function showReport(report) {
  const target = $("trial-detail");
  target.replaceChildren(el("h3", report.run_id, "record-id"));
  (report.trials || []).forEach(trial => {
    const node = el("details", undefined, "trial"), grade = trial.grade;
    const labels = [trial.task_id, (grade.pass ?? grade.passed) ? "通过" : "失败", ...grade.failure_attribution];
    node.append(el("summary", labels.join(" · ")));
    if (grade.database_diff.length) {
      const [diff, rows] = table(["业务表", "Gold", "Agent"], "diff-table");
      grade.database_diff.forEach(change => {
        const row = el("tr");
        row.append(el("td", change.table), el("td", json(change.missing), "removed"), el("td", json(change.extra), "added"));
        rows.append(row);
      });
      node.append(diff);
    } else node.append(el("p", "双库终态一致", "muted"));
    detail(node, "完整记录", {grade, replies:trial.replies, trace_id:trial.trace_id});
    target.append(node);
  });
}

document.querySelectorAll("[data-scene]").forEach(button => button.onclick = () => task(async () => setState(await api(`/api/demos/${button.dataset.scene}`, {}))));
$("advance").onclick = () => task(async () => setState(await api(`/api/sessions/${state.session_id}/demo/advance`, {})));
$("run-all").onclick = () => task(async () => {
  while (state.demo.step < state.demo.total) setState(await api(`/api/sessions/${state.session_id}/demo/advance`, {}));
});
$("refresh").onclick = () => task(async () => { if (state) await restoreSession(state.session_id); });
$("new-chat").onclick = () => task(async () => setState(await api("/api/sessions", {})));
$("chat-form").onsubmit = event => {
  event.preventDefault();
  task(async () => {
    if (!state) setState(await api("/api/sessions", {}));
    await streamMessage(state.session_id, $("message").value);
    $("message").value = "";
  });
};
for (const id of ["live", "benchmark"]) $(`${id}-tab`).onclick = () => {
  for (const name of ["live", "benchmark"]) {
    $(name).hidden = name !== id;
    $(`${name}-tab`).setAttribute("aria-selected", String(name === id));
  }
  if (id === "benchmark") task(loadReports);
};
$("load-reports").onclick = () => task(loadReports);
document.querySelectorAll("[data-pane]").forEach(button => {
  if (button.tagName !== "BUTTON") return;
  button.onclick = () => {
    document.body.dataset.pane = button.dataset.pane;
    document.querySelectorAll("button[data-pane]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  };
});
audit();
task(async () => {
  const saved = localStorage.getItem("aftersales-session");
  if (saved) {
    try { await restoreSession(saved); }
    catch { localStorage.removeItem("aftersales-session"); notify("会话不可用，请新建对话。"); }
  }
});
