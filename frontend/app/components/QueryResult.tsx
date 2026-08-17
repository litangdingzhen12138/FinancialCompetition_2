"use client";

import { useState } from "react";
import {
  createShare,
  downloadExport,
  drillQuery,
} from "../lib/api";
import type {
  AnswerStatus,
  Insight,
  QueryResponse,
  TimeGranularity,
} from "../types";
import { SmartChart } from "./SmartChart";

const drillLabels: Record<TimeGranularity, string> = {
  year: "按年",
  quarter: "下钻到季度",
  month: "下钻到月",
  day: "下钻到日",
};

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: 4,
    }).format(value);
  }
  return String(value);
}

function hasVisualChart(result: QueryResponse): boolean {
  return ["line", "bar", "donut"].includes(result.visualization.chart_type);
}

function periodRange(
  level: TimeGranularity,
  value: unknown,
): { start: string; end: string } | null {
  if (typeof value !== "string") return null;
  const start = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(start.getTime())) return null;
  const end = new Date(start);
  if (level === "year") {
    end.setUTCFullYear(end.getUTCFullYear() + 1);
  } else if (level === "quarter") {
    end.setUTCMonth(end.getUTCMonth() + 3);
  } else if (level === "month") {
    end.setUTCMonth(end.getUTCMonth() + 1);
  } else {
    end.setUTCDate(end.getUTCDate() + 1);
  }
  end.setUTCDate(end.getUTCDate() - 1);
  return {
    start: start.toISOString().slice(0, 10),
    end: end.toISOString().slice(0, 10),
  };
}

export function QueryResult({
  initialResult,
  streamedInsight,
  streamedAnswer,
  answerStatus,
  answerError,
  onRetryAnswer,
  onResultChange,
}: {
  initialResult: QueryResponse;
  streamedInsight: Insight | null;
  streamedAnswer?: string;
  answerStatus?: AnswerStatus;
  answerError?: string;
  onRetryAnswer?: () => void;
  onResultChange?: (result: QueryResponse) => void;
}) {
  const [result, setResult] = useState(initialResult);
  const [view, setView] = useState<"chart" | "table">("table");
  const [sqlOpen, setSqlOpen] = useState(false);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const insight = streamedInsight ?? result.insight;
  const finalAnswer = streamedAnswer || insight?.summary || "";
  const finalAnswerStatus = answerStatus ?? result.answer_status;
  const chartAvailable = hasVisualChart(result);

  async function handleDrill(
    target: TimeGranularity,
    periodStart?: string,
    periodEnd?: string,
  ) {
    setBusy("drill");
    setNotice("");
    try {
      const next = await drillQuery(
        result.query_id,
        target,
        periodStart,
        periodEnd,
      );
      setResult(next);
      onResultChange?.(next);
      setView("table");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "下钻失败");
    } finally {
      setBusy("");
    }
  }

  function handlePointSelect(record: Record<string, unknown>) {
    const target = result.available_time_drill[0];
    const level = result.current_time_level;
    if (!target || !level) return;
    const range = periodRange(level, record.period_start);
    if (!range) return;
    void handleDrill(target, range.start, range.end);
  }

  async function handleExport(format: "xlsx" | "csv") {
    setBusy(format);
    setNotice("");
    try {
      await downloadExport(result.query_id, format);
      setNotice(`${format.toUpperCase()} 报告已开始下载`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "导出失败");
    } finally {
      setBusy("");
    }
  }

  async function handleShare() {
    setBusy("share");
    setNotice("");
    try {
      const share = await createShare(result.query_id);
      const url = `${window.location.origin}/share/${share.token}`;
      await navigator.clipboard.writeText(url);
      setNotice("24 小时有效的分享链接已复制；访问时仍会重新校验权限");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "分享失败");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="result-stack" aria-label="查询结果">
      <article className="result-card insight-card">
          <div className="insight-title">
            <span className="insight-mark">答</span>
            <div>
              <p>最终回答</p>
              <h3>
                {finalAnswerStatus === "completed"
                  ? "已回答"
                  : finalAnswerStatus === "failed"
                    ? "回答生成失败"
                    : "正在组织回答…"}
              </h3>
            </div>
            {result.sql && (
              <button
                type="button"
                className="sql-trigger"
                onClick={() => setSqlOpen((value) => !value)}
              >
                {sqlOpen ? "收起生成 SQL 语句" : "查看生成 SQL 语句"}
              </button>
            )}
          </div>
          {finalAnswerStatus === "failed" ? (
            <div className="answer-error">
              <p className="caveat">
                {answerError || "最终回答暂时生成失败，图表和查询数据不受影响。"}
              </p>
              {onRetryAnswer && (
                <button className="ghost-button" onClick={onRetryAnswer}>
                  重新生成最终回答
                </button>
              )}
            </div>
          ) : finalAnswer ? (
            <p
              className={`insight-summary ${
                finalAnswerStatus === "streaming" ? "answer-streaming" : ""
              }`}
              aria-live="polite"
            >
              {finalAnswer}
            </p>
          ) : (
            <>
              <div className="insight-loading" aria-label="正在生成最终回答">
                <span />
                <span />
                <span />
              </div>
              <p className="answer-waiting">查询数据已就绪，请稍候。</p>
            </>
          )}
          {sqlOpen && result.sql && (
            <pre className="answer-sql-panel">{result.sql}</pre>
          )}
          <div className="answer-trust-strip" aria-label="可信查询链路">
            <strong>语义计划已确认</strong>
            <strong>SQL 安全校验通过</strong>
            <strong>操作已留痕</strong>
          </div>
      </article>

      <div className="result-card chart-card">
        <div className="chart-toolbar">
          <div className="result-actions">
            <div className="view-switch" aria-label="结果显示方式">
              <button
                className={view === "table" ? "active" : ""}
                onClick={() => setView("table")}
              >
                表格
              </button>
              {chartAvailable && (
                <button
                  className={view === "chart" ? "active" : ""}
                  onClick={() => setView("chart")}
                >
                  图表
                </button>
              )}
            </div>
            {result.permissions.can_export && (
              <button
                className="ghost-button"
                onClick={() => handleExport("xlsx")}
                disabled={Boolean(busy) || finalAnswerStatus !== "completed"}
                title={
                  finalAnswerStatus === "completed"
                    ? "导出完整报告"
                    : "最终回答生成完成后可导出完整报告"
                }
              >
                {busy === "xlsx" ? "导出中…" : "导出"}
              </button>
            )}
            {result.permissions.can_share && (
              <button
                className="ghost-button"
                onClick={handleShare}
                disabled={Boolean(busy) || finalAnswerStatus !== "completed"}
                title={
                  finalAnswerStatus === "completed"
                    ? "创建分享链接"
                    : "最终回答生成完成后可分享完整报告"
                }
              >
                {busy === "share" ? "生成中…" : "分享"}
              </button>
            )}
          </div>
        </div>

        {notice && <div className="inline-notice">{notice}</div>}

        {result.available_time_drill.length > 0 && (
          <div className="drill-strip">
            <span>点击按钮下钻</span>
            {result.available_time_drill.map((target) => (
              <button
                key={target}
                onClick={() => handleDrill(target)}
                disabled={busy === "drill"}
              >
                {busy === "drill" ? "查询中…" : drillLabels[target]}
              </button>
            ))}
            <small>口径：周期最后一个有数据日期的期末值</small>
          </div>
        )}

        {result.current_time_level &&
          result.available_time_drill.length > 0 &&
          result.visualization.chart_type === "line" && (
            <p className="point-hint">
              点击折线数据点或周期标签，可进入所选周期的下一层
            </p>
          )}

        {view === "chart" && chartAvailable ? (
          <div className="visualization-canvas">
            <SmartChart
              spec={result.visualization}
              records={result.records}
              columns={result.columns}
              onPointSelect={
                result.current_time_level &&
                result.available_time_drill.length > 0
                  ? handlePointSelect
                  : undefined
              }
            />
          </div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  {result.columns.map((column) => (
                    <th key={column.key}>
                      {column.label}
                      {column.unit ? `（${column.unit}）` : ""}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.records.map((record, rowIndex) => (
                  <tr key={rowIndex}>
                    {result.columns.map((column) => (
                      <td key={column.key}>{formatCell(record[column.key])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

    </section>
  );
}
