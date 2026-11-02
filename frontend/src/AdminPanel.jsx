import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

const tabs = { overview: "概览", users: "账号", refunds: "退款工单", runs: "运行记录", audit: "操作审计" };
const roleLabels = { user: "用户", agent: "客服", admin: "管理员" };
const statusLabels = {
  pending_review: "待审核",
  approved: "已批准",
  rejected: "已拒绝",
  completed: "已完成",
  cancelled: "已撤回",
};
const ticketStatusLabels = {
  pending: "待处理",
  processing: "处理中",
  resolved: "已解决",
  rejected: "已拒绝",
  cancelled: "已撤回",
};
const kindLabels = { role: "角色", compliance: "合规", node: "节点", tool: "工具", business: "售后业务" };
const actionLabels = {
  role_changed: "角色变更",
  refund_requested: "申请退款",
  refund_cancelled: "撤回退款",
  information_submitted: "补充信息",
  approve: "批准",
  reject: "拒绝",
  need_info: "要求补充",
};
const outcomeLabels = { success: "成功", error: "失败", fallback: "降级", interrupted: "中断" };

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

const auditKinds = [
  ["", "全部"],
  ["role", "角色变更"],
  ["compliance", "合规命中"],
  ["node", "节点"],
  ["tool", "工具"],
  ["business", "售后业务"],
];

const tableHeaders = {
  users: ["用户名", "用户 ID", "角色", "创建时间"],
  refunds: ["退款单", "订单", "用户", "退款状态", "工单状态", "恢复状态", "会话 ID"],
  runs: ["运行 ID", "用户", "开始时间", "事件数", "失败", "降级", "会话 ID"],
  audit: ["时间", "类型", "操作", "执行者", "目标", "结果", "详情"],
};

function Filters({ label, value, onChange, options }) {
  return (
    <div className="admin-filters">
      <label>
        {label}
        <select value={value} onChange={(event) => onChange(event.target.value)}>
          {options.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
        </select>
      </label>
    </div>
  );
}

function Overview({ data }) {
  const stats = [
    ["待处理工单", data.pending_tickets],
    ["失败工单", data.failed_tickets],
    ["等待补充信息", data.waiting_for_customer],
    ["等待恢复", data.pending_resume],
  ];
  const users = data.users || {};
  return (
    <>
      <div className="admin-stat-grid">
        {stats.map(([label, value]) => (
          <div className="admin-stat" key={label}>
            <span>{label}</span>
            <strong>{value ?? "—"}</strong>
          </div>
        ))}
      </div>
      <section className="panel-card">
        <h3>退款状态分布</h3>
        <dl className="kv-list">
          {Object.entries(data.by_status || {}).map(([key, value]) => (
            <div className="kv-row" key={key}><dt>{statusLabels[key] || key}</dt><dd>{value}</dd></div>
          ))}
          {Object.keys(data.by_status || {}).length === 0 && <p className="muted">暂无退款申请</p>}
        </dl>
      </section>
      <section className="panel-card">
        <h3>账号分布</h3>
        <dl className="kv-list">
          {Object.entries(roleLabels).map(([key, label]) => (
            <div className="kv-row" key={key}><dt>{label}</dt><dd>{users[key] ?? 0}</dd></div>
          ))}
        </dl>
      </section>
    </>
  );
}

function Table({ rows, tab, changeRole, openRun }) {
  if (rows.length === 0) return <p className="muted">暂无记录</p>;
  return (
    <div className="admin-table-wrap">
      <table className="admin-table">
        <thead>
          <tr>{tableHeaders[tab].map((header) => <th key={header}>{header}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={row.id ? `event-${row.id}` : `row-${index}`}>
              {tab === "users" && (
                <>
                  <td>{row.username}</td>
                  <td>{row.id}</td>
                  <td>
                    <select
                      value={row.role}
                      onChange={(event) => changeRole(row.username, event.target.value)}
                      aria-label={`${row.username} 的角色`}
                    >
                      <option value="user">用户</option>
                      <option value="agent">客服</option>
                      <option value="admin">管理员</option>
                    </select>
                  </td>
                  <td>{formatTime(row.created_at)}</td>
                </>
              )}
              {tab === "refunds" && (
                <>
                  <td>{row.id}</td>
                  <td>{row.order_id}</td>
                  <td>{row.username || row.user_id}</td>
                  <td>{statusLabels[row.status] || row.status}</td>
                  <td>{ticketStatusLabels[row.ticket_status] || row.ticket_status || "—"}</td>
                  <td>{row.resume_status || "—"}</td>
                  <td>{row.session_id || "—"}</td>
                </>
              )}
              {tab === "runs" && (
                <>
                  <td><button className="text-button" onClick={() => openRun(row.trace_id)}>{row.trace_id}</button></td>
                  <td>{row.actor_id || "—"}</td>
                  <td>{formatTime(row.started_at)}</td>
                  <td>{row.event_count}</td>
                  <td>{row.error_count || 0}</td>
                  <td>{row.fallback_count || 0}</td>
                  <td>{row.session_id || "—"}</td>
                </>
              )}
              {tab === "audit" && (
                <>
                  <td>{formatTime(row.created_at)}</td>
                  <td>{kindLabels[row.kind] || row.kind || "—"}</td>
                  <td>{actionLabels[row.action] || row.action}</td>
                  <td>{row.actor_id || "—"}</td>
                  <td>{row.target_id || row.refund_id || row.session_id || "—"}</td>
                  <td>{outcomeLabels[row.outcome] || row.outcome || "—"}</td>
                  <td>{row.detail || "—"}</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Pager({ offset, count, onPrev, onNext }) {
  return (
    <div className="admin-pager">
      <button disabled={offset === 0} onClick={onPrev}>上一页</button>
      <span>第 {offset / 20 + 1} 页</span>
      <button disabled={count < 20} onClick={onNext}>下一页</button>
    </div>
  );
}

function RunDetail({ events, error }) {
  return (
    <section className="panel-card">
      <h3>运行详情（节点 / 工具 / 失败原因 / 降级结果）</h3>
      {error && <p className="error" role="alert">{error}</p>}
      {!error && events.length === 0 && <p className="muted">暂无事件</p>}
      {events.length > 0 && (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>{["时间", "类型", "操作", "结果", "耗时(ms)", "详情"].map((header) => <th key={header}>{header}</th>)}</tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>{formatTime(event.created_at)}</td>
                  <td>{kindLabels[event.kind] || event.kind}</td>
                  <td>{actionLabels[event.action] || event.action}</td>
                  <td>{outcomeLabels[event.outcome] || event.outcome}</td>
                  <td>{event.duration_ms == null ? "—" : Math.round(event.duration_ms)}</td>
                  <td>{event.detail || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function AdminPanel() {
  const [tab, setTab] = useState("overview");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [selectedRun, setSelectedRun] = useState(null);
  const [runError, setRunError] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [tick, setTick] = useState(0);

  const query = useCallback(() => {
    const params = new URLSearchParams({ limit: "20", offset: String(offset) });
    if (tab === "users" && roleFilter) params.set("role", roleFilter);
    if (tab === "refunds" && statusFilter) params.set("status", statusFilter);
    if (tab === "audit" && kindFilter) params.set("kind", kindFilter);
    return params.toString();
  }, [tab, offset, roleFilter, statusFilter, kindFilter]);

  const load = useCallback(async () => {
    setError("");
    setData(null);
    try {
      setData(await api(`/api/admin/${tab}?${query()}`));
    } catch (err) {
      setError(err.message);
    }
  }, [tab, query]);

  useEffect(() => { load(); }, [load]);

  function resetTab(next) {
    setTab(next);
    setOffset(0);
    setSelectedRun(null);
    setRunError("");
    setMessage("");
    setError("");
  }

  async function changeRole(username, role) {
    if (!window.confirm(`确认将 ${username} 的角色改为「${roleLabels[role]}」？`)) {
      setTick((value) => value + 1); // 取消时触发重渲染，把受控 select 恢复为原角色
      return;
    }
    setMessage("");
    setError("");
    try {
      await api("/api/admin/users/role", { method: "POST", body: JSON.stringify({ username, role }) });
      setMessage("角色已更新");
      await load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function openRun(id) {
    setRunError("");
    try {
      setSelectedRun((await api(`/api/admin/runs/${encodeURIComponent(id)}?limit=100`)).events);
    } catch (err) {
      setRunError(err.message);
    }
  }

  const rows = data?.[tab] || data?.events || [];

  return (
    <section className="chat-panel admin-panel">
      <div className="chat-heading"><h2>管理员工作区</h2></div>
      <nav className="ops-tabs" aria-label="管理员功能">
        {Object.entries(tabs).map(([key, label]) => (
          <button key={key} className={tab === key ? "active" : ""} onClick={() => resetTab(key)}>{label}</button>
        ))}
        <button onClick={load}>刷新</button>
      </nav>
      <div className="ops-body">
        {error && <p className="error" role="alert">{error}</p>}
        {message && <p className="after-sales-notice" role="status">{message}</p>}
        {!data && !error && <p className="muted">正在加载...</p>}
        {data && tab === "overview" && <Overview data={data} />}
        {data && tab !== "overview" && (
          <>
            {tab === "users" && (
              <Filters label="角色" value={roleFilter} onChange={(value) => { setRoleFilter(value); setOffset(0); }} options={[["", "全部"], ["user", "用户"], ["agent", "客服"], ["admin", "管理员"]]} />
            )}
            {tab === "refunds" && (
              <Filters label="退款状态" value={statusFilter} onChange={(value) => { setStatusFilter(value); setOffset(0); }} options={[["", "全部"], ...Object.entries(statusLabels)]} />
            )}
            {tab === "audit" && (
              <Filters label="事件类型" value={kindFilter} onChange={(value) => { setKindFilter(value); setOffset(0); }} options={auditKinds} />
            )}
            <Table key={tick} rows={rows} tab={tab} changeRole={changeRole} openRun={openRun} />
            <Pager offset={offset} count={rows.length} onPrev={() => setOffset(Math.max(0, offset - 20))} onNext={() => setOffset(offset + 20)} />
          </>
        )}
        {selectedRun && <RunDetail events={selectedRun} error={runError} />}
      </div>
    </section>
  );
}
