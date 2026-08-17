"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  downloadHistoryBatch,
  getCachedAuthUser,
  getHistory,
  getHistoryDetail,
} from "../lib/api";
import type { HistoryItem, QueryResponse } from "../types";
import { QueryResult } from "./QueryResult";

type ExportScope = "all" | "recent" | "session";

type ThreadGroup = {
  key: string;
  userId: string;
  sessionId: string;
  title: string;
  updatedAt: string;
  items: HistoryItem[];
};

function fallbackTitle(item: HistoryItem): string {
  const compact = item.question.replace(/\s+/g, " ").trim();
  return compact.length > 26 ? `${compact.slice(0, 26)}…` : compact;
}

function buildThreads(items: HistoryItem[]): ThreadGroup[] {
  const groups = new Map<string, ThreadGroup>();
  for (const item of items) {
    const key = `${item.user_id}:${item.session_id}`;
    const existing = groups.get(key);
    if (existing) {
      existing.items.push(item);
      continue;
    }
    groups.set(key, {
      key,
      userId: item.user_id,
      sessionId: item.session_id,
      title: item.thread_title || fallbackTitle(item),
      updatedAt: item.created_at,
      items: [item],
    });
  }
  return Array.from(groups.values());
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function HistoryView() {
  const user = getCachedAuthUser();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [selected, setSelected] = useState<QueryResponse | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [keyword, setKeyword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [exportScope, setExportScope] = useState<ExportScope>("all");
  const [recentCount, setRecentCount] = useState(20);
  const [exportUser, setExportUser] = useState("");
  const [exportThread, setExportThread] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportNotice, setExportNotice] = useState("");

  const threads = useMemo(() => buildThreads(items), [items]);
  const userIds = useMemo(
    () => Array.from(new Set(threads.map((thread) => thread.userId))).sort(),
    [threads],
  );
  const userSections = useMemo(() => {
    if (user.role !== "admin") {
      return [{ userId: user.user_id, threads }];
    }
    return userIds.map((userId) => ({
      userId,
      threads: threads.filter((thread) => thread.userId === userId),
    }));
  }, [threads, user.role, user.user_id, userIds]);
  const availableExportThreads = exportUser
    ? threads.filter((thread) => thread.userId === exportUser)
    : threads;

  async function load(search = "") {
    setLoading(true);
    setError("");
    try {
      setItems(await getHistory(search));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "历史记录加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let active = true;
    getHistory()
      .then((history) => {
        if (active) setItems(history);
      })
      .catch((caught) => {
        if (active) {
          setError(caught instanceof Error ? caught.message : "历史记录加载失败");
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleSearch(event: FormEvent) {
    event.preventDefault();
    await load(keyword.trim());
  }

  async function openDetail(queryId: string) {
    setError("");
    try {
      setSelected(await getHistoryDetail(queryId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "详情加载失败");
    }
  }

  function toggleThread(key: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function handleBatchExport() {
    const selectedThread =
      availableExportThreads.find((thread) => thread.key === exportThread) ??
      availableExportThreads[0];
    if (exportScope === "session" && !selectedThread) {
      setExportNotice("当前没有可导出的 thread");
      return;
    }
    setExporting(true);
    setExportNotice("");
    try {
      await downloadHistoryBatch({
        scope: exportScope,
        recentCount: exportScope === "recent" ? recentCount : undefined,
        sessionId: exportScope === "session" ? selectedThread?.sessionId : undefined,
        ownerUserId:
          exportScope === "session" ? selectedThread?.userId : exportUser || undefined,
      });
      setExportNotice("批量报告已开始下载");
    } catch (caught) {
      setExportNotice(caught instanceof Error ? caught.message : "批量导出失败");
    } finally {
      setExporting(false);
    }
  }

  return (
    <section>
      <div className="page-toolbar history-toolbar">
        <p className="page-lede">
          按会话保留每一次问题、查询计划、结果、图表与业务结论。
        </p>
        <form className="history-search" onSubmit={handleSearch}>
          <label htmlFor="history-keyword">搜索历史问题</label>
          <div>
            <input
              id="history-keyword"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="输入机构、指标或问题关键词"
            />
            <button>搜索</button>
          </div>
        </form>
      </div>

      {error && <p className="error-message page-error">{error}</p>}
      {selected ? (
        <div>
          <button className="back-button" onClick={() => setSelected(null)}>
            ← 返回历史列表
          </button>
          <QueryResult
            key={selected.query_id}
            initialResult={selected}
            streamedInsight={selected.insight}
          />
        </div>
      ) : (
        <>
          <section className="batch-export-card" aria-label="批量导出历史报告">
            <div className="batch-export-intro">
              <strong>批量导出</strong>
              <span>导出汇总与每次查询的完整结果数据</span>
            </div>
            {user.role === "admin" && (
              <label>
                <span>用户</span>
                <select
                  value={exportUser}
                  onChange={(event) => {
                    setExportUser(event.target.value);
                    setExportThread("");
                  }}
                >
                  <option value="">全部用户</option>
                  {userIds.map((userId) => (
                    <option key={userId} value={userId}>{userId}</option>
                  ))}
                </select>
              </label>
            )}
            <label>
              <span>导出范围</span>
              <select
                value={exportScope}
                onChange={(event) => setExportScope(event.target.value as ExportScope)}
              >
                <option value="all">全部数据</option>
                <option value="recent">最近若干条</option>
                <option value="session">指定 thread_id</option>
              </select>
            </label>
            {exportScope === "recent" && (
              <label>
                <span>最近条数</span>
                <input
                  type="number"
                  min={1}
                  max={5000}
                  value={recentCount}
                  onChange={(event) => setRecentCount(Number(event.target.value) || 1)}
                />
              </label>
            )}
            {exportScope === "session" && (
              <label className="export-thread-select">
                <span>会话 thread_id</span>
                <select
                  value={exportThread}
                  onChange={(event) => setExportThread(event.target.value)}
                >
                  {availableExportThreads.map((thread) => (
                    <option key={thread.key} value={thread.key}>
                      {thread.title} · {thread.sessionId}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <button
              type="button"
              className="batch-export-button"
              onClick={() => void handleBatchExport()}
              disabled={exporting || items.length === 0}
            >
              {exporting ? "正在导出…" : "导出 XLSX"}
            </button>
            {exportNotice && <p className="batch-export-notice">{exportNotice}</p>}
          </section>

          <div className="thread-history-list">
            {loading && <div className="loading-card">正在读取历史记录…</div>}
            {!loading && items.length === 0 && (
              <div className="loading-card">暂时没有符合条件的查询记录。</div>
            )}
            {userSections.map((section) => (
              <section className="history-user-section" key={section.userId}>
                {user.role === "admin" && (
                  <div className="history-user-head">
                    <span>用户</span>
                    <strong>{section.userId}</strong>
                    <small>{section.threads.length} 个 thread</small>
                  </div>
                )}
                {section.threads.map((thread) => {
                  const isOpen = expanded.has(thread.key);
                  return (
                    <article className="history-thread" key={thread.key}>
                      <button
                        type="button"
                        className="history-thread-head"
                        onClick={() => toggleThread(thread.key)}
                        aria-expanded={isOpen}
                      >
                        <span className={isOpen ? "thread-chevron open" : "thread-chevron"}>›</span>
                        <span className="history-thread-title">
                          <strong>{thread.title}</strong>
                          <small>thread_id：{thread.sessionId}</small>
                        </span>
                        <span className="history-thread-meta">
                          {thread.items.length} 次提问 · {formatDate(thread.updatedAt)}
                        </span>
                      </button>
                      {isOpen && (
                        <div className="history-thread-turns">
                          {thread.items
                            .slice()
                            .reverse()
                            .map((item, index) => (
                              <article className="history-turn" key={item.query_id}>
                                <span className="history-turn-index">{index + 1}</span>
                                <div>
                                  <p>{item.question}</p>
                                  <small>
                                    {formatDate(item.created_at)} · {item.row_count} 条结果 · {item.duration_ms} ms
                                  </small>
                                  <span>{item.answer}</span>
                                </div>
                                <button onClick={() => void openDetail(item.query_id)}>
                                  查看报告 →
                                </button>
                              </article>
                            ))}
                        </div>
                      )}
                    </article>
                  );
                })}
              </section>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
