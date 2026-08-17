"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import {
  checkCapabilities,
  clearSession,
  getCachedAuthUser,
  getHistory,
  getSessionHistory,
  renameSession,
  streamFinalAnswer,
  streamQuery,
} from "../lib/api";
import type {
  AnswerStatus,
  HistoryItem,
  Insight,
  QueryResponse,
} from "../types";
import { QueryResult } from "./QueryResult";

const examples = [
  "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
  "截至2026年3月31日，各项存款余额排名前三的是哪几家？",
  "江苏省C市农商行2026年一季度各项贷款余额趋势如何？",
  "江苏省E市农商行的不良贷款率是否达到监管要求？",
];

const stages = [
  { key: "understanding", label: "理解问题" },
  { key: "data_ready", label: "查询数据" },
  { key: "visualized", label: "智能出图" },
  { key: "insight", label: "业务解释" },
];

type Conversation = {
  key: string;
  sessionId: string | null;
  title: string;
  question: string;
  result: QueryResponse | null;
  insight: Insight | null;
  answerText: string;
  answerStatus: AnswerStatus;
  answerError: string;
  stage: string;
  message: string;
  error: string;
  latestQueryId: string | null;
  updatedAt: string;
  historyLoading: boolean;
  historyLoaded: boolean;
  turns: QueryResponse[];
  ownerUserId: string;
  expanded: boolean;
};

function createConversation(
  key: string,
  question = "",
  ownerUserId = "",
): Conversation {
  return {
    key,
    sessionId: null,
    title: "新会话",
    question,
    result: null,
    insight: null,
    answerText: "",
    answerStatus: "pending",
    answerError: "",
    stage: "idle",
    message: "等待提问",
    error: "",
    latestQueryId: null,
    updatedAt: new Date().toISOString(),
    historyLoading: false,
    historyLoaded: true,
    turns: [],
    ownerUserId,
    expanded: false,
  };
}

function createSessionKey(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function titleFromQuestion(question: string): string {
  const compact = question.replace(/\s+/g, " ").trim();
  return compact.length > 18 ? `${compact.slice(0, 18)}…` : compact;
}

function isConversationBusy(conversation: Conversation): boolean {
  return !["idle", "complete", "error"].includes(conversation.stage);
}

function historyConversation(item: HistoryItem): Conversation {
  const conversation = createConversation(
    `history-${item.user_id}-${item.session_id}`,
    item.question,
    item.user_id,
  );
  conversation.sessionId = item.session_id;
  conversation.title = item.thread_title || titleFromQuestion(item.question);
  conversation.latestQueryId = item.query_id;
  conversation.updatedAt = item.created_at;
  conversation.historyLoaded = false;
  return conversation;
}

function upsertTurn(
  turns: QueryResponse[] | undefined,
  next: QueryResponse,
): QueryResponse[] {
  const history = turns ?? [];
  const existing = history.findIndex((turn) => turn.query_id === next.query_id);
  if (existing === -1) return [...history, next];
  return history.map((turn, index) => (index === existing ? next : turn));
}

function groupLabel(updatedAt: string): "今天" | "近 7 天" | "更早" {
  const now = new Date();
  const date = new Date(updatedAt);
  if (date.toDateString() === now.toDateString()) return "今天";
  const sevenDaysAgo = new Date(now);
  sevenDaysAgo.setDate(now.getDate() - 7);
  return date >= sevenDaysAgo ? "近 7 天" : "更早";
}

export function Workbench() {
  const user = getCachedAuthUser();
  const [conversations, setConversations] = useState<Conversation[]>(() => [
    createConversation("initial", examples[0], user.user_id),
  ]);
  const [activeKey, setActiveKey] = useState("initial");
  const [conversationSearch, setConversationSearch] = useState("");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [clearingKey, setClearingKey] = useState<string | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);
  const activeKeyRef = useRef(activeKey);
  const activeQueryIdsRef = useRef<Record<string, string | null>>({});

  const activeConversation =
    conversations.find((conversation) => conversation.key === activeKey) ??
    conversations[0];
  const {
    question,
    result,
    insight,
    answerText,
    answerStatus,
    answerError,
    stage,
    message,
    error,
  } = activeConversation;

  useEffect(() => {
    activeKeyRef.current = activeKey;
  }, [activeKey]);

  useEffect(() => {
    checkCapabilities().then(setConnected);
    getHistory()
      .then((items) => {
        const latestBySession = new Map<string, HistoryItem>();
        for (const item of items) {
          const sessionKey = `${item.user_id}:${item.session_id}`;
          if (!latestBySession.has(sessionKey)) {
            latestBySession.set(sessionKey, item);
          }
        }
        const restored = Array.from(latestBySession.values()).map(historyConversation);
        setConversations((current) => {
          const knownSessions = new Set(
            current
              .filter((conversation) => conversation.sessionId)
              .map(
                (conversation) =>
                  `${conversation.ownerUserId}:${conversation.sessionId}`,
              ),
          );
          return [
            ...current,
            ...restored.filter(
              (conversation) =>
                !knownSessions.has(
                  `${conversation.ownerUserId}:${conversation.sessionId}`,
                ),
            ),
          ];
        });
      })
      .finally(() => setHistoryLoading(false));
  }, [user.user_id]);

  function updateConversation(
    key: string,
    updater: (current: Conversation) => Conversation,
  ) {
    setConversations((current) =>
      current.map((conversation) =>
        conversation.key === key ? updater(conversation) : conversation,
      ),
    );
  }

  function patchConversation(key: string, patch: Partial<Conversation>) {
    updateConversation(key, (current) => ({ ...current, ...patch }));
  }

  async function generateFinalAnswer(
    queryResult: QueryResponse,
    conversationKey: string,
  ) {
    const queryId = queryResult.query_id;
    activeQueryIdsRef.current[conversationKey] = queryId;
    patchConversation(conversationKey, {
      stage: "visualized",
      message: "数据与图表已就绪，正在生成最终回答",
      answerText: "",
      answerError: "",
      answerStatus: "streaming",
    });
    try {
      await streamFinalAnswer(queryId, {
        onStart() {
          if (activeQueryIdsRef.current[conversationKey] !== queryId) return;
          patchConversation(conversationKey, { answerStatus: "streaming" });
        },
        onDelta(payload) {
          if (activeQueryIdsRef.current[conversationKey] !== queryId) return;
          updateConversation(conversationKey, (current) => ({
            ...current,
            answerText: current.answerText + payload.content,
          }));
        },
        onDone(payload) {
          if (activeQueryIdsRef.current[conversationKey] !== queryId) return;
          updateConversation(conversationKey, (current) => ({
            ...current,
            answerText: payload.answer,
            insight: payload.insight,
            answerStatus: "completed",
            turns: (current.turns ?? []).map((turn) =>
              turn.query_id === queryId
                ? {
                    ...turn,
                    insight: payload.insight,
                    answer_status: "completed",
                  }
                : turn,
            ),
            result:
              current.result?.query_id === queryId
                ? {
                    ...current.result,
                    insight: payload.insight,
                    answer_status: "completed",
                  }
                : current.result,
          }));
        },
      });
      if (activeQueryIdsRef.current[conversationKey] !== queryId) return;
      patchConversation(conversationKey, {
        stage: "complete",
        message: "本次查询已完成并留痕",
      });
    } catch (caught) {
      if (activeQueryIdsRef.current[conversationKey] !== queryId) return;
      patchConversation(conversationKey, {
        answerStatus: "failed",
        answerError:
          caught instanceof Error
            ? caught.message
            : "最终回答生成失败，请稍后重试",
        stage: "complete",
        message: "数据与图表已完成，最终回答生成失败",
      });
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const conversationKey = activeConversation.key;
    const submittedQuestion = question.trim();
    if (!submittedQuestion || isConversationBusy(activeConversation)) return;

    patchConversation(conversationKey, {
      title:
        activeConversation.title === "新会话"
          ? titleFromQuestion(submittedQuestion)
          : activeConversation.title,
      error: "",
      answerError: "",
      result: null,
      insight: null,
      answerText: "",
      answerStatus: "pending",
      stage: "understanding",
      message: "正在理解问题",
    });
    let queryResult: QueryResponse | null = null;
    try {
      await streamQuery(submittedQuestion, activeConversation.sessionId, {
        onStatus(payload) {
          updateConversation(conversationKey, (current) => ({
            ...current,
            sessionId: payload.session_id ?? current.sessionId,
            stage: payload.stage,
            message: payload.message,
          }));
        },
        onResult(payload) {
          queryResult = payload;
          activeQueryIdsRef.current[conversationKey] = payload.query_id;
          updateConversation(conversationKey, (current) => ({
            ...current,
            result: payload,
            insight: payload.insight,
            answerText: payload.insight?.summary ?? "",
            answerStatus: payload.answer_status,
            sessionId: payload.session_id,
            latestQueryId: payload.query_id,
            updatedAt: payload.generated_at,
            historyLoaded: true,
            turns: upsertTurn(current.turns, payload),
            stage: "visualized",
            message: "数据与图表已就绪",
          }));
          if (activeKeyRef.current === conversationKey) {
            requestAnimationFrame(() =>
              resultRef.current?.scrollIntoView({
                behavior: "smooth",
                block: "start",
              }),
            );
          }
        },
        onComplete() {
          patchConversation(conversationKey, {
            message: "数据与图表已就绪",
          });
        },
      });
    } catch (caught) {
      patchConversation(conversationKey, {
        error: caught instanceof Error ? caught.message : "查询失败，请稍后重试",
        stage: "error",
        message: "查询未完成",
      });
      return;
    }

    const readyResult = queryResult as QueryResponse | null;
    if (!readyResult) {
      patchConversation(conversationKey, {
        error: "查询结果未返回，请稍后重试",
        stage: "error",
        message: "查询未完成",
      });
      return;
    }
    if (readyResult.answer_mode === "rule") {
      patchConversation(conversationKey, {
        stage: "complete",
        message: "本次查询已完成并留痕",
      });
      return;
    }
    await generateFinalAnswer(readyResult, conversationKey);
  }

  function handleNewConversation() {
    const key = createSessionKey();
    const conversation = createConversation(key, "", user.user_id);
    conversation.sessionId = key;
    conversation.expanded = true;
    setConversations((current) => [conversation, ...current]);
    setActiveKey(key);
  }

  async function handleSelectConversation(conversation: Conversation) {
    if (
      conversation.key === activeKey &&
      conversation.expanded
    ) {
      patchConversation(conversation.key, { expanded: false });
      return;
    }
    setActiveKey(conversation.key);
    setConversations((current) =>
      current.map((item) => ({
        ...item,
        expanded: item.key === conversation.key,
      })),
    );
    if (
      conversation.historyLoaded ||
      !conversation.sessionId ||
      conversation.historyLoading
    ) {
      return;
    }
    patchConversation(conversation.key, {
      historyLoading: true,
      stage: "data_ready",
      message: "正在加载历史会话",
      error: "",
    });
    try {
      const turns = await getSessionHistory(
        conversation.sessionId,
        conversation.ownerUserId,
      );
      const detail = turns.at(-1) ?? null;
      patchConversation(conversation.key, {
        turns,
        historyLoaded: true,
        result: detail,
        insight: detail?.insight ?? null,
        answerText: detail?.insight?.summary ?? "",
        answerStatus: detail?.answer_status ?? "pending",
        question: detail?.question ?? conversation.question,
        stage: "complete",
        message: "历史会话已加载",
      });
    } catch (caught) {
      patchConversation(conversation.key, {
        stage: "error",
        message: "历史会话加载失败",
        error: caught instanceof Error ? caught.message : "历史会话加载失败",
      });
    } finally {
      patchConversation(conversation.key, { historyLoading: false });
    }
  }

  async function handleRenameConversation(conversation: Conversation) {
    const nextTitle = window.prompt("请输入新的会话名称", conversation.title)?.trim();
    if (!nextTitle || nextTitle === conversation.title) return;
    try {
      if (conversation.sessionId) {
        await renameSession(
          conversation.sessionId,
          nextTitle,
          conversation.ownerUserId,
        );
      }
      patchConversation(conversation.key, { title: nextTitle.slice(0, 60) });
    } catch (caught) {
      window.alert(caught instanceof Error ? caught.message : "会话重命名失败");
    }
  }

  function handleSelectTurn(conversationKey: string, turn: QueryResponse) {
    setActiveKey(conversationKey);
    activeQueryIdsRef.current[conversationKey] = turn.query_id;
    patchConversation(conversationKey, {
      result: turn,
      insight: turn.insight,
      answerText: turn.insight?.summary ?? "",
      answerStatus: turn.answer_status,
      question: turn.question,
      stage: "complete",
      message: "已打开该轮历史结果",
    });
  }

  async function handleClearConversation(conversationKey: string) {
    const conversation = conversations.find((item) => item.key === conversationKey);
    if (!conversation) return;
    if (user.role !== "admin") {
      window.alert("权限不足：普通用户不能删除查询记录或清空会话，请联系系统管理员。");
      return;
    }
    if (
      isConversationBusy(conversation) ||
      !window.confirm(`确定删除“${conversation.title}”吗？会话内容和记忆将一并清除。`)
    ) {
      return;
    }
    const sessionId = conversation.sessionId;
    setClearingKey(conversationKey);
    try {
      if (sessionId) await clearSession(sessionId, conversation.ownerUserId);
      activeQueryIdsRef.current[conversationKey] = null;
      const remaining = conversations.filter((item) => item.key !== conversationKey);
      if (remaining.length) {
        setConversations(remaining);
        if (activeKey === conversationKey) setActiveKey(remaining[0].key);
      } else {
        const key = createSessionKey();
        const replacement = createConversation(key, "", user.user_id);
        replacement.sessionId = key;
        setConversations([replacement]);
        setActiveKey(key);
      }
    } catch (caught) {
      patchConversation(conversationKey, {
        error:
          caught instanceof Error ? caught.message : "清空会话失败，请稍后重试",
      });
    } finally {
      setClearingKey(null);
    }
  }

  const completedIndex =
    stage === "complete"
      ? stages.length
      : Math.max(
          0,
          stages.findIndex((item) => item.key === stage) + 1,
        );

  const normalizedSearch = conversationSearch.trim().toLowerCase();
  const groupedConversations = (["今天", "近 7 天", "更早"] as const)
    .map((label) => ({
      label,
      items: conversations
        .filter(
          (conversation) =>
            groupLabel(conversation.updatedAt) === label &&
            (!normalizedSearch ||
              conversation.title.toLowerCase().includes(normalizedSearch)),
        )
        .sort(
          (left, right) =>
            new Date(right.updatedAt).getTime() - new Date(left.updatedAt).getTime(),
        ),
    }))
    .filter((group) => group.items.length);

  return (
    <div className="workbench-layout">
      <aside className="conversation-sidebar" aria-label="会话列表">
        <div className="conversation-sidebar-head">
          <div>
            <strong>智能问数</strong>
            <small>会话上下文独立隔离</small>
          </div>
        </div>
        <button
          type="button"
          className="new-conversation-button"
          onClick={handleNewConversation}
        >
          <span>＋</span> 新建对话
        </button>
        <label className="conversation-search">
          <span>⌕</span>
          <input
            value={conversationSearch}
            onChange={(event) => setConversationSearch(event.target.value)}
            placeholder="搜索会话"
            aria-label="搜索会话"
          />
        </label>

        <div className="conversation-list">
          {historyLoading && conversations.length === 1 && (
            <p className="conversation-list-empty">正在加载历史会话…</p>
          )}
          {!historyLoading && !groupedConversations.length && (
            <p className="conversation-list-empty">
              {normalizedSearch ? "没有匹配的会话" : "暂无会话"}
            </p>
          )}
          {groupedConversations.map((group) => (
            <section key={group.label} className="conversation-group">
              <h3>{group.label}</h3>
              {group.items.map((conversation) => (
                <div key={conversation.key} className="conversation-entry">
                  <div
                    className={
                      conversation.key === activeConversation.key
                        ? "conversation-list-item active"
                        : "conversation-list-item"
                    }
                    role="button"
                    tabIndex={0}
                    onClick={() => void handleSelectConversation(conversation)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        void handleSelectConversation(conversation);
                      }
                    }}
                  >
                    <span
                      className={
                        isConversationBusy(conversation)
                          ? "conversation-status running"
                          : "conversation-status"
                      }
                    />
                    <span className="conversation-list-title">{conversation.title}</span>
                    <span
                      className={
                        conversation.expanded
                          ? "conversation-expand open"
                          : "conversation-expand"
                      }
                      aria-hidden="true"
                    >
                      ›
                    </span>
                    <button
                      type="button"
                      className="rename-conversation-button"
                      aria-label={`重命名会话：${conversation.title}`}
                      title="重命名会话"
                      onClick={(event) => {
                        event.stopPropagation();
                        void handleRenameConversation(conversation);
                      }}
                    >
                      ✎
                    </button>
                    <button
                      type="button"
                      className="delete-conversation-button"
                      aria-label={`删除会话：${conversation.title}`}
                      title="删除会话"
                      disabled={
                        isConversationBusy(conversation) ||
                        clearingKey === conversation.key
                      }
                      onClick={(event) => {
                        event.stopPropagation();
                        void handleClearConversation(conversation.key);
                      }}
                    >
                      {clearingKey === conversation.key ? "…" : "×"}
                    </button>
                  </div>
                  {conversation.key === activeConversation.key &&
                    conversation.expanded &&
                    conversation.historyLoading && (
                      <p className="conversation-turn-loading">正在展开历史问题…</p>
                    )}
                  {conversation.key === activeConversation.key &&
                    conversation.expanded &&
                    (conversation.turns ?? []).length > 0 && (
                      <div className="conversation-turn-list" aria-label="该会话的历史问题">
                        {(conversation.turns ?? []).map((turn, index) => (
                          <button
                            key={turn.query_id}
                            type="button"
                            className={
                              conversation.result?.query_id === turn.query_id
                                ? "active"
                                : ""
                            }
                            onClick={() => handleSelectTurn(conversation.key, turn)}
                            title={turn.question}
                          >
                            <span>{index + 1}</span>
                            <span>{turn.question}</span>
                          </button>
                        ))}
                      </div>
                    )}
                </div>
              ))}
            </section>
          ))}
        </div>
        <p className="conversation-sidebar-foot">切换会话不会带入其他线程的记忆</p>
      </aside>

      <main className="workbench-main">
      <section className="hero">
        <div className="hero-copy">
          <div className="live-label">
            <span className={connected ? "status-dot" : "status-dot muted"} />
            {connected === null
              ? "正在连接问数引擎"
              : connected
                ? "问数引擎在线"
                : "问数引擎未连接"}
          </div>
          <h1>
            从一句业务问题，到一份<span>可信结论</span>
          </h1>
          <p>
            自动完成语义理解、SQL 生成、安全执行、智能出图与业务解释，
            让经营数据真正进入决策现场。
          </p>
        </div>
      </section>

      <section className="ask-card">
        <form onSubmit={handleSubmit}>
          <label htmlFor="question">你想了解什么？</label>
          <div className="question-box">
            <textarea
              id="question"
              value={question}
              onChange={(event) =>
                patchConversation(activeConversation.key, {
                  question: event.target.value,
                })
              }
              placeholder="例如：本季度各项存款余额排名前三的是哪几家？"
              rows={3}
            />
            <button
              type="submit"
              className="ask-button"
              disabled={
                !question.trim() ||
                !connected ||
                isConversationBusy(activeConversation)
              }
            >
              <span>生成分析</span>
              <small>Enter ↗</small>
            </button>
          </div>
        </form>
      </section>

      {stage !== "idle" && (
        <section className="pipeline" aria-live="polite">
          <div className="pipeline-head">
            <strong>{message}</strong>
            {stage !== "error" && <span>{Math.min(completedIndex, 4)}/4</span>}
          </div>
          <div className="pipeline-track">
            {stages.map((item, index) => (
              <div
                key={item.key}
                className={
                  index < completedIndex
                    ? "pipeline-step done"
                    : index === completedIndex
                      ? "pipeline-step active"
                      : "pipeline-step"
                }
              >
                <span>{index < completedIndex ? "✓" : index + 1}</span>
                <small>{item.label}</small>
              </div>
            ))}
          </div>
          {error && <p className="error-message">{error}</p>}
        </section>
      )}

      <div ref={resultRef}>
        {result ? (
          <QueryResult
            key={result.query_id}
            initialResult={result}
            streamedInsight={insight}
            streamedAnswer={answerText}
            answerStatus={answerStatus}
            answerError={answerError}
            onRetryAnswer={() =>
              void generateFinalAnswer(result, activeConversation.key)
            }
            onResultChange={(next) => {
              activeQueryIdsRef.current[activeConversation.key] = next.query_id;
              updateConversation(activeConversation.key, (current) => ({
                ...current,
                result: next,
                insight: next.insight,
                answerText: next.insight?.summary ?? "",
                answerStatus: next.answer_status,
                answerError: "",
                latestQueryId: next.query_id,
                updatedAt: next.generated_at,
                turns: upsertTurn(current.turns, next),
                stage: "complete",
                message: "下钻查询已完成并留痕",
              }));
            }}
          />
        ) : (
          <section className="empty-state-grid">
            <article>
              <span>01</span>
              <h3>用业务语言提问</h3>
              <p>无需记忆表名、字段和复杂口径，多轮追问会继承上下文。</p>
            </article>
            <article>
              <span>02</span>
              <h3>自动选择最佳图表</h3>
              <p>根据时间、分类、构成和结果规模，推荐折线、柱状或表格。</p>
            </article>
            <article>
              <span>03</span>
              <h3>结论与证据同时给出</h3>
              <p>区分数据事实与业务推断，所有操作都可回溯、可审计。</p>
            </article>
          </section>
        )}
      </div>
      </main>
    </div>
  );
}
