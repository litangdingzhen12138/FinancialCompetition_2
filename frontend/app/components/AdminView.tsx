"use client";

import { useEffect, useRef, useState } from "react";
import { getAdminOverview, getAdminUsers, getAudit } from "../lib/api";
import type { AdminUserSummary, AuditItem } from "../types";

type AuditSort = "newest" | "risk_desc" | "risk_asc";
type RiskFilter = "all" | "elevated" | "low" | "medium" | "high";
type UserPanel = "all" | "risk" | null;

const actionLabels: Record<string, string> = {
  "query.completed": "查询完成",
  "query.failed": "查询失败",
  "query.time_drill": "时间下钻",
  "query.exported": "导出报告",
  "query.shared": "创建分享",
  "answer.completed": "回答完成",
  "history.batch_exported": "批量导出历史",
  "share.viewed": "查看分享",
  session_clear: "清除会话",
};

const riskLabels: Record<RiskFilter, string> = {
  all: "全部风险",
  elevated: "中高风险",
  high: "高风险",
  medium: "中风险",
  low: "低风险",
};

export function AdminView() {
  const [overview, setOverview] = useState<Record<string, number>>({});
  const [users, setUsers] = useState<AdminUserSummary[]>([]);
  const [audit, setAudit] = useState<AuditItem[]>([]);
  const [sort, setSort] = useState<AuditSort>("newest");
  const [risk, setRisk] = useState<RiskFilter>("all");
  const [selectedUser, setSelectedUser] = useState("");
  const [userPanel, setUserPanel] = useState<UserPanel>(null);
  const [auditLoading, setAuditLoading] = useState(true);
  const [error, setError] = useState("");
  const auditRef = useRef<HTMLElement>(null);

  useEffect(() => {
    Promise.all([getAdminOverview(), getAdminUsers()])
      .then(([summary, userItems]) => {
        setOverview(summary);
        setUsers(userItems);
      })
      .catch((caught) => {
        setError(caught instanceof Error ? caught.message : "管理概览加载失败");
      });
  }, []);

  useEffect(() => {
    let active = true;
    getAudit(sort, risk, selectedUser)
      .then((items) => {
        if (active) setAudit(items);
      })
      .catch((caught) => {
        if (active) {
          setError(caught instanceof Error ? caught.message : "审计数据加载失败");
        }
      })
      .finally(() => {
        if (active) setAuditLoading(false);
      });
    return () => {
      active = false;
    };
  }, [risk, selectedUser, sort]);

  const cards = [
    {
      label: "累计查询",
      value: overview.query_count ?? 0,
      detail: "全部问数任务",
    },
    {
      label: "活跃用户",
      value: overview.user_count ?? 0,
      detail: "点击查看用户列表",
      action: "users" as const,
    },
    {
      label: "风险用户",
      value: overview.risk_user_count ?? 0,
      detail: "点击查看风险用户名单",
      action: "risk_users" as const,
    },
    {
      label: "风险事件",
      value: overview.risk_event_count ?? 0,
      detail: "点击查看全部中高风险操作",
      action: "risk_events" as const,
    },
    {
      label: "有效分享",
      value: overview.active_share_count ?? 0,
      detail: "未过期且未撤销",
    },
  ];
  const displayedUsers =
    userPanel === "risk"
      ? users.filter((item) => item.risk_event_count > 0)
      : users;

  function scrollToAudit() {
    requestAnimationFrame(() => {
      auditRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function drillIntoElevatedRisk() {
    setAuditLoading(true);
    setError("");
    setRisk("elevated");
    setSort("risk_desc");
    scrollToAudit();
  }

  function handleCardAction(action: "users" | "risk_users" | "risk_events") {
    if (action === "users") {
      setUserPanel((current) => (current === "all" ? null : "all"));
      return;
    }
    if (action === "risk_users") {
      setUserPanel((current) => (current === "risk" ? null : "risk"));
      return;
    }
    drillIntoElevatedRisk();
  }

  function showUserAudit(userId: string) {
    setAuditLoading(true);
    setError("");
    setSelectedUser(userId);
    setRisk("all");
    setSort("newest");
    scrollToAudit();
  }

  function changeRisk(next: RiskFilter) {
    if (next === risk) return;
    setAuditLoading(true);
    setError("");
    setRisk(next);
  }

  function changeSort(next: AuditSort) {
    if (next === sort) return;
    setAuditLoading(true);
    setError("");
    setSort(next);
  }

  function changeUser(next: string) {
    if (next === selectedUser) return;
    setAuditLoading(true);
    setError("");
    setSelectedUser(next);
  }

  function resetAuditView() {
    if (risk === "all" && sort === "newest" && !selectedUser) return;
    setAuditLoading(true);
    setError("");
    setRisk("all");
    setSort("newest");
    setSelectedUser("");
  }

  return (
    <section>
      <div className="page-toolbar admin-toolbar">
        <p className="page-lede">
          追踪用户查询、导出、分享与安全拦截，形成完整操作闭环。
        </p>
        <span className="admin-status">
          <span className="status-dot" />
          审计服务在线
        </span>
      </div>

      {error && <p className="error-message page-error">{error}</p>}

      <div className="overview-grid">
        {cards.map((card, index) => {
          const content = (
            <>
              <div>
                <span>0{index + 1}</span>
                <small>{card.label}</small>
              </div>
              <strong>{card.value}</strong>
              <p>{card.detail}</p>
            </>
          );
          return card.action ? (
            <button
              key={card.label}
              type="button"
              className="overview-card overview-card-drill"
              onClick={() => handleCardAction(card.action)}
            >
              {content}
            </button>
          ) : (
            <article key={card.label} className="overview-card">
              {content}
            </article>
          );
        })}
      </div>

      {userPanel && (
        <section className="admin-user-panel" aria-label="用户列表">
          <div>
            <strong>{userPanel === "risk" ? "风险用户" : "活跃用户"}</strong>
            <span>点击用户可查看其全部操作记录</span>
          </div>
          <div className="admin-user-list">
            {displayedUsers.length === 0 && <p>暂无符合条件的用户。</p>}
            {displayedUsers.map((item) => (
              <button
                type="button"
                key={item.user_id}
                onClick={() => showUserAudit(item.user_id)}
              >
                <strong>{item.user_id}</strong>
                <span>{item.operation_count} 次操作</span>
                <span className={item.risk_event_count ? "has-risk" : ""}>
                  {item.risk_event_count} 条风险事件
                </span>
                <small>最后活跃：{new Date(item.last_active_at).toLocaleString("zh-CN")}</small>
              </button>
            ))}
          </div>
        </section>
      )}

      <div className="admin-grid">
        <section className="audit-card" ref={auditRef}>
          <div className="card-head audit-head">
            <div>
              <p className="section-kicker">AUDIT TRAIL</p>
              <h2>最近操作记录</h2>
            </div>
            <span>
              {selectedUser ? `${selectedUser} · ` : ""}
              {riskLabels[risk]} · {auditLoading ? "加载中" : `共 ${audit.length} 条`}
            </span>
          </div>

          <div className="audit-tools" aria-label="审计记录筛选与排序">
            <label>
              <span>用户</span>
              <select value={selectedUser} onChange={(event) => changeUser(event.target.value)}>
                <option value="">全部用户</option>
                {users.map((item) => (
                  <option key={item.user_id} value={item.user_id}>{item.user_id}</option>
                ))}
              </select>
            </label>
            <label>
              <span>风险范围</span>
              <select
                value={risk}
                onChange={(event) => changeRisk(event.target.value as RiskFilter)}
              >
                {Object.entries(riskLabels).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>
            <label>
              <span>排序方式</span>
              <select
                value={sort}
                onChange={(event) => changeSort(event.target.value as AuditSort)}
              >
                <option value="newest">时间从新到旧</option>
                <option value="risk_desc">风险从高到低</option>
                <option value="risk_asc">风险从低到高</option>
              </select>
            </label>
            <button
              type="button"
              onClick={resetAuditView}
              disabled={risk === "all" && sort === "newest" && !selectedUser}
            >
              重置
            </button>
          </div>

          <div className="audit-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>用户</th>
                  <th>操作</th>
                  <th>风险</th>
                  <th>查询编号</th>
                </tr>
              </thead>
              <tbody>
                {!auditLoading && audit.length === 0 && (
                  <tr>
                    <td colSpan={5} className="table-empty">
                      当前筛选条件下没有操作记录。
                    </td>
                  </tr>
                )}
                {audit.map((item) => (
                  <tr key={item.event_id}>
                    <td>{new Date(item.created_at).toLocaleString("zh-CN")}</td>
                    <td>
                      <button className="audit-user-link" onClick={() => showUserAudit(item.user_id)}>
                        {item.user_id}
                      </button>
                    </td>
                    <td>{actionLabels[item.action] ?? item.action}</td>
                    <td>
                      <button
                        type="button"
                        className={`risk-pill ${item.risk_level}`}
                        onClick={() => changeRisk(item.risk_level)}
                        title={`仅查看${riskLabels[item.risk_level]}`}
                      >
                        {item.risk_level === "high"
                          ? "高"
                          : item.risk_level === "medium"
                            ? "中"
                            : "低"}
                      </button>
                    </td>
                    <td>{item.query_id?.slice(0, 10) ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </section>
  );
}
