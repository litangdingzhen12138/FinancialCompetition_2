"use client";

import { useEffect, useRef, useState } from "react";
import {
  getActiveFreezes,
  getAdminOverview,
  getAdminMetrics,
  getAdminUsers,
  getAudit,
  getSecurityAlerts,
  previewMetricDataImport,
  publishMetricDataImport,
  resolveSecurityAlert,
  unfreezeUser,
  type AdminOverview,
} from "../lib/api";
import type {
  ActiveFreeze,
  AdminUserSummary,
  AuditItem,
  MetricCatalogItem,
  MetricDataImportPreview,
  SecurityAlert,
} from "../types";

type AuditSort = "newest" | "risk_desc" | "risk_asc";
type RiskFilter = "all" | "elevated" | "low" | "medium" | "high";
type UserPanel = "all" | "risk" | null;
type AuditChainStatus = "loading" | "valid" | "invalid" | "error";

const actionLabels: Record<string, string> = {
  "query.requested": "发起查询",
  "query.completed": "查询完成",
  "query.failed": "查询失败",
  "query.time_drill": "时间下钻",
  "query.exported": "导出报告",
  "query.shared": "创建分享",
  "answer.completed": "回答完成",
  "history.batch_exported": "批量导出历史",
  "share.viewed": "查看分享",
  "user.unfrozen": "管理员解冻",
  "data_import.previewed": "校验数据文件",
  "data_import.published": "发布数据更新",
  session_clear: "清除会话",
};

const riskLabels: Record<RiskFilter, string> = {
  all: "全部风险",
  elevated: "中高风险",
  high: "高风险",
  medium: "中风险",
  low: "低风险",
};

const alertRuleLabels: Record<string, string> = {
  S3_QUERY_FREQUENCY: "敏感指标高频查询",
  REPEATED_ACCESS_DENIED: "连续越权访问",
  LARGE_EXPORT: "单次大批量导出",
  DAILY_EXPORT_VOLUME: "当日累计导出超限",
  OFF_HOURS_S3_EXPORT: "非工作时间敏感导出",
  SHARE_LINK_FREQUENCY: "短时高频创建分享",
  MULTI_METRIC_QUERY: "单次查询指标过多",
};

function auditQuestion(item: AuditItem): string {
  const question = item.details.question;
  return typeof question === "string" && question.trim() ? question : "—";
}

export function AdminView() {
  const [overview, setOverview] = useState<AdminOverview>({});
  const [users, setUsers] = useState<AdminUserSummary[]>([]);
  const [audit, setAudit] = useState<AuditItem[]>([]);
  const [alerts, setAlerts] = useState<SecurityAlert[]>([]);
  const [freezes, setFreezes] = useState<ActiveFreeze[]>([]);
  const [auditChainStatus, setAuditChainStatus] = useState<AuditChainStatus>("loading");
  const [resolvingAlertId, setResolvingAlertId] = useState("");
  const [unfreezingUserId, setUnfreezingUserId] = useState("");
  const [sort, setSort] = useState<AuditSort>("newest");
  const [risk, setRisk] = useState<RiskFilter>("all");
  const [selectedUser, setSelectedUser] = useState("");
  const [userPanel, setUserPanel] = useState<UserPanel>(null);
  const [auditLoading, setAuditLoading] = useState(true);
  const [error, setError] = useState("");
  const auditRef = useRef<HTMLElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [importPreview, setImportPreview] = useState<MetricDataImportPreview | null>(null);
  const [importBusy, setImportBusy] = useState<"preview" | "publish" | "">("");
  const [importMessage, setImportMessage] = useState("");
  const [metricCatalog, setMetricCatalog] = useState<MetricCatalogItem[]>([]);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [overwriteConfirmOpen, setOverwriteConfirmOpen] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      getAdminOverview(),
      getAdminUsers(),
      getSecurityAlerts(),
      getActiveFreezes(),
    ]).then(([summaryResult, usersResult, alertsResult, freezesResult]) => {
      if (!active) return;
      const failures: string[] = [];
      if (summaryResult.status === "fulfilled") {
        setOverview(summaryResult.value);
        setAuditChainStatus(
          summaryResult.value.audit_chain_valid === true ? "valid" : "invalid",
        );
      } else {
        setAuditChainStatus("error");
        failures.push("管理概览");
      }
      if (usersResult.status === "fulfilled") setUsers(usersResult.value);
      else failures.push("用户列表");
      if (alertsResult.status === "fulfilled") {
        setAlerts(alertsResult.value);
        setOverview((current) => ({
          ...current,
          open_alert_count: alertsResult.value.length,
        }));
      } else failures.push("安全告警");
      if (freezesResult.status === "fulfilled") setFreezes(freezesResult.value);
      else failures.push("冻结账号");
      if (failures.length) setError(`${failures.join("、")}加载失败`);
    });

    const alertTimer = window.setInterval(() => {
      getSecurityAlerts()
        .then((items) => {
          if (active) {
            setAlerts(items);
            setOverview((current) => ({
              ...current,
              open_alert_count: items.length,
            }));
          }
        })
        .catch(() => undefined);
    }, 15_000);
    return () => {
      active = false;
      window.clearInterval(alertTimer);
    };
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
    {
      label: "待处置告警",
      value: overview.open_alert_count ?? 0,
      detail: "异常规则实时命中",
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

  async function resolveAlert(alertId: string) {
    if (resolvingAlertId) return;
    setResolvingAlertId(alertId);
    try {
      await resolveSecurityAlert(alertId);
      setAlerts((items) => items.filter((item) => item.alert_id !== alertId));
      setOverview((current) => ({
        ...current,
        open_alert_count: Math.max(
          0,
          Number(current.open_alert_count ?? 0) - 1,
        ),
      }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "告警处置失败");
    } finally {
      setResolvingAlertId("");
    }
  }

  async function unfreeze(userId: string) {
    if (unfreezingUserId) return;
    setUnfreezingUserId(userId);
    setError("");
    try {
      await unfreezeUser(userId);
      setFreezes((items) => items.filter((item) => item.user_id !== userId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "账号解冻失败");
    } finally {
      setUnfreezingUserId("");
    }
  }

  async function openMetricCatalog() {
    setCatalogOpen(true);
    if (metricCatalog.length || catalogLoading) return;
    setCatalogLoading(true);
    try {
      setMetricCatalog(await getAdminMetrics());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "指标清单加载失败");
      setCatalogOpen(false);
    } finally {
      setCatalogLoading(false);
    }
  }

  function chooseImportFile(file: File | null) {
    setSelectedFile(file);
    setImportPreview(null);
    setImportMessage("");
    setOverwriteConfirmOpen(false);
    if (!file && fileInputRef.current) fileInputRef.current.value = "";
  }

  async function previewImport() {
    if (!selectedFile || importBusy) return;
    setImportBusy("preview");
    setImportMessage("");
    try {
      const preview = await previewMetricDataImport(selectedFile);
      setImportPreview(preview);
      setImportMessage(preview.valid ? "文件校验通过，请核对更新统计。" : "文件校验未通过。");
    } catch (caught) {
      setImportPreview(null);
      setImportMessage(caught instanceof Error ? caught.message : "文件校验失败");
    } finally {
      setImportBusy("");
    }
  }

  async function publishImport(confirmOverwrite: boolean) {
    if (!selectedFile || importBusy) return;
    setImportBusy("publish");
    setOverwriteConfirmOpen(false);
    setImportMessage("");
    try {
      const result = await publishMetricDataImport(selectedFile, confirmOverwrite);
      setImportMessage(
        `发布完成：新增 ${result.insert_count} 条，覆盖 ${result.overwrite_count} 条。`,
      );
      setImportPreview(null);
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (caught) {
      setImportMessage(caught instanceof Error ? caught.message : "数据发布失败");
    } finally {
      setImportBusy("");
    }
  }

  return (
    <section>
      <div className="page-toolbar admin-toolbar">
        <p className="page-lede">
          追踪用户查询、导出、分享与安全拦截，形成完整操作闭环。
        </p>
        <span className="admin-status">
          <span className="status-dot" />
          {auditChainStatus === "loading"
            ? "审计链校验中"
            : auditChainStatus === "valid"
              ? "审计链校验正常"
              : auditChainStatus === "invalid"
                ? "审计链校验异常"
                : "审计链校验失败"}
        </span>
      </div>

      {error && <p className="error-message page-error">{error}</p>}

      <section className="data-import-card" aria-label="指标数据更新">
        <div className="data-import-head">
          <div>
            <p className="section-kicker">DATA UPDATE</p>
            <h2>指标数据更新</h2>
            <p>
              上传完整 Excel，或只包含“指标数据表”的 Excel，校验后增量合并。
            </p>
          </div>
          <button type="button" className="metric-catalog-button" onClick={openMetricCatalog}>
            查看现有指标清单
          </button>
        </div>

        <div className="data-import-tip">
          当前仅支持现有指标的数据更新，指标清单发生变化的文件暂不支持直接发布，请自行扩展指标清单之后再次发布。
        </div>

        <div className="data-import-controls">
          <label className="data-file-picker">
            <span>选择 Excel 文件</span>
            <input
              ref={fileInputRef}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(event) => chooseImportFile(event.target.files?.[0] ?? null)}
            />
          </label>
          <span className="selected-file-name">
            {selectedFile ? `${selectedFile.name} · ${(selectedFile.size / 1024 / 1024).toFixed(2)} MB` : "尚未选择文件"}
          </span>
          <button
            type="button"
            className="data-import-primary"
            disabled={!selectedFile || Boolean(importBusy)}
            onClick={previewImport}
          >
            {importBusy === "preview" ? "校验中…" : "校验文件"}
          </button>
        </div>

        {importMessage && (
          <p className={importPreview?.valid === false ? "data-import-message invalid" : "data-import-message"}>
            {importMessage}
          </p>
        )}

        {importPreview && (
          <div className="data-import-preview">
            <div className="import-stat-grid">
              <div><span>新增</span><strong>{importPreview.insert_count}</strong></div>
              <div><span>无变化</span><strong>{importPreview.unchanged_count}</strong></div>
              <div><span>待覆盖</span><strong>{importPreview.overwrite_count}</strong></div>
              <div><span>文件内去重</span><strong>{importPreview.duplicate_count}</strong></div>
            </div>
            <p>
              涉及 {importPreview.metric_ids.length} 项指标、{importPreview.organization_count} 家机构
              {importPreview.date_start && importPreview.date_end
                ? `，日期范围 ${importPreview.date_start} 至 ${importPreview.date_end}`
                : ""}
            </p>
            {importPreview.errors.length > 0 && (
              <ul className="data-import-errors">
                {importPreview.errors.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
              </ul>
            )}
            {importPreview.can_publish && (
              <div className="data-import-actions">
                <button type="button" onClick={() => chooseImportFile(null)}>取消</button>
                <button
                  type="button"
                  className={importPreview.overwrite_count ? "danger" : "primary"}
                  disabled={Boolean(importBusy)}
                  onClick={() => {
                    if (importPreview.overwrite_count) setOverwriteConfirmOpen(true);
                    else void publishImport(false);
                  }}
                >
                  {importBusy === "publish"
                    ? "发布中…"
                    : importPreview.overwrite_count
                      ? "确认覆盖并发布"
                      : "发布更新"}
                </button>
              </div>
            )}
          </div>
        )}
      </section>

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
        <section className="audit-card frozen-account-card">
          <div className="card-head audit-head">
            <div>
              <p className="section-kicker">FROZEN ACCOUNTS</p>
              <h2>当前冻结账号</h2>
            </div>
            <span>共 {freezes.length} 个</span>
          </div>
          <div className="audit-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>账号</th>
                  <th>冻结原因</th>
                  <th>冻结至</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {freezes.length === 0 && (
                  <tr><td colSpan={4} className="table-empty">当前没有冻结账号。</td></tr>
                )}
                {freezes.map((item) => (
                  <tr key={item.user_id}>
                    <td>{item.user_id}</td>
                    <td>{item.reason}</td>
                    <td>{new Date(item.frozen_until).toLocaleString("zh-CN")}</td>
                    <td>
                      <button
                        type="button"
                        disabled={Boolean(unfreezingUserId)}
                        onClick={() => unfreeze(item.user_id)}
                      >
                        {unfreezingUserId === item.user_id ? "解冻中…" : "解冻"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        <section className="audit-card security-alert-card">
          <div className="card-head audit-head">
            <div>
              <p className="section-kicker">SECURITY ALERTS</p>
              <h2>待处置安全告警</h2>
            </div>
            <span>共 {alerts.length} 条</span>
          </div>
          <div className="audit-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>用户</th>
                  <th>规则</th>
                  <th>级别</th>
                  <th>处置</th>
                </tr>
              </thead>
              <tbody>
                {alerts.length === 0 && (
                  <tr><td colSpan={5} className="table-empty">暂无待处置告警。</td></tr>
                )}
                {alerts.map((item) => (
                  <tr key={item.alert_id}>
                    <td>{new Date(item.created_at).toLocaleString("zh-CN")}</td>
                    <td>{item.user_id}</td>
                    <td>{alertRuleLabels[item.rule_code] ?? item.rule_code}</td>
                    <td><span className={`risk-pill ${item.severity}`}>{item.severity}</span></td>
                    <td>
                      <button
                        type="button"
                        disabled={Boolean(resolvingAlertId)}
                        onClick={() => resolveAlert(item.alert_id)}
                      >
                        {resolvingAlertId === item.alert_id ? "处置中…" : "标记已处置"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
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
                  <th>具体问题</th>
                  <th>风险</th>
                  <th>查询编号</th>
                </tr>
              </thead>
              <tbody>
                {!auditLoading && audit.length === 0 && (
                  <tr>
                    <td colSpan={6} className="table-empty">
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
                    <td className="audit-question">{auditQuestion(item)}</td>
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

      {catalogOpen && (
        <div className="admin-modal-backdrop" role="presentation">
          <section className="admin-modal metric-catalog-modal" role="dialog" aria-modal="true" aria-labelledby="metric-catalog-title">
            <div className="admin-modal-head">
              <div>
                <p className="section-kicker">METRIC CATALOG</p>
                <h2 id="metric-catalog-title">现有指标清单</h2>
              </div>
              <button type="button" aria-label="关闭指标清单" onClick={() => setCatalogOpen(false)}>×</button>
            </div>
            <p className="metric-catalog-count">共 {metricCatalog.length} 项指标</p>
            <div className="metric-catalog-table">
              <table>
                <thead><tr><th>指标编号</th><th>指标名称</th><th>指标含义</th><th>指标单位</th></tr></thead>
                <tbody>
                  {catalogLoading && <tr><td colSpan={4}>指标清单加载中…</td></tr>}
                  {metricCatalog.map((item) => (
                    <tr key={item.metric_id}>
                      <td>{item.metric_id}</td>
                      <td>{item.metric_name}</td>
                      <td>{item.description}</td>
                      <td>{item.unit}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {overwriteConfirmOpen && importPreview && (
        <div className="admin-modal-backdrop" role="presentation">
          <section className="admin-modal overwrite-confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="overwrite-confirm-title">
            <div className="admin-modal-head">
              <h2 id="overwrite-confirm-title">确认覆盖现有数据？</h2>
              <button type="button" aria-label="关闭覆盖确认" onClick={() => setOverwriteConfirmOpen(false)}>×</button>
            </div>
            <p>
              将新增 {importPreview.insert_count} 条、覆盖 {importPreview.overwrite_count} 条、忽略 {importPreview.unchanged_count} 条无变化数据。
            </p>
            <p>覆盖后，后续查询、排名和图表将使用新值，历史结果不会自动更新。</p>
            <div className="admin-modal-actions">
              <button type="button" onClick={() => setOverwriteConfirmOpen(false)}>取消</button>
              <button type="button" className="danger" disabled={Boolean(importBusy)} onClick={() => void publishImport(true)}>
                确认覆盖 {importPreview.overwrite_count} 条并发布
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
