"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getSharedQuery } from "../lib/api";
import type { QueryResponse } from "../types";
import { QueryResult } from "./QueryResult";

export function ShareView({ token }: { token: string }) {
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getSharedQuery(token)
      .then(setResult)
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : "共享报告加载失败"),
      );
  }, [token]);

  return (
    <main className="share-page">
      <header>
        <Link href="/" className="brand">
          <span className="brand-mark">衡</span>
          <span>
            <strong>数衡</strong>
            <small>安全共享报告</small>
          </span>
        </Link>
        <span>访问时已按当前查看者角色重新校验与脱敏</span>
      </header>
      <div className="share-content">
        {error && <p className="error-message page-error">{error}</p>}
        {!error && !result && <div className="loading-card">正在验证分享权限…</div>}
        {result && (
          <>
            <div className="page-heading">
              <div>
                <p className="eyebrow">SHARED REPORT</p>
                <h1>{result.question}</h1>
                <p>报告编号 {result.query_id.slice(0, 12)}</p>
              </div>
            </div>
            <QueryResult initialResult={result} streamedInsight={result.insight} />
          </>
        )}
      </div>
    </main>
  );
}
