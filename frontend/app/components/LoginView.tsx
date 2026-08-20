"use client";

import { FormEvent, useState } from "react";
import { login, type AuthUser } from "../lib/api";

export function LoginView({
  checking,
  onLoggedIn,
}: {
  checking: boolean;
  onLoggedIn: (user: AuthUser) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!username.trim() || !password || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      onLoggedIn(await login(username.trim(), password));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "登录失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  if (checking) {
    return (
      <main className="auth-restore-screen" role="status" aria-live="polite">
        <span className="auth-restore-mark" aria-hidden="true">衡</span>
        <p>正在进入工作空间…</p>
      </main>
    );
  }

  return (
    <main className="login-page">
      <section className="login-visual" aria-label="数衡智能问数">
        <div className="login-brand">
          <span className="brand-mark">衡</span>
          <span>
            <strong>数衡</strong>
            <small>BankInsight</small>
          </span>
        </div>
        <div className="login-visual-copy">
          <p>联合银行 · 智能经营分析</p>
          <h1>让每一次提问，<br />都有可信的数据回答</h1>
          <span>自然语言问数 · 安全 SQL · 智能图表 · 业务洞察</span>
        </div>
      </section>

      <section className="login-panel">
        <form className="login-card" onSubmit={handleSubmit}>
          <div>
            <p className="login-eyebrow">WELCOME BACK</p>
            <h2>登录数衡</h2>
            <span className="login-subtitle">使用系统账号进入智能问数工作台</span>
          </div>

          <label htmlFor="login-username">账号</label>
          <input
            id="login-username"
            name="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="请输入账号"
            autoComplete="username"
            disabled={checking || submitting}
          />

          <label htmlFor="login-password">密码</label>
          <input
            id="login-password"
            name="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="请输入密码"
            type="password"
            autoComplete="current-password"
            disabled={checking || submitting}
          />

          {error && <p className="login-error">{error}</p>}

          <button
            type="submit"
            disabled={
              checking || submitting || !username.trim() || !password
            }
          >
            {checking
              ? "正在恢复登录状态…"
              : submitting
                ? "正在登录…"
                : "登录"}
          </button>
          <small className="demo-account-tip">
            默认分析员：analyst / analyst123；analyst2 / analyst2123；
            analyst3 / analyst3123　管理员：admin / admin123
          </small>
        </form>
      </section>
    </main>
  );
}
