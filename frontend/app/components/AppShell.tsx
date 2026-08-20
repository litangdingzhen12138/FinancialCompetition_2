"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import {
  getCurrentUser,
  getRememberedAuthUser,
  logout,
  type AuthUser,
} from "../lib/api";
import { LoginView } from "./LoginView";

const navigation = [
  { href: "/", label: "智能问数", mark: "问" },
  { href: "/history", label: "历史报告", mark: "历" },
  { href: "/admin", label: "管理审计", mark: "管" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [user, setUser] = useState<AuthUser | null>(getRememberedAuthUser);
  const [checking, setChecking] = useState(true);
  const [loggingOut, setLoggingOut] = useState(false);
  const [railCollapsed, setRailCollapsed] = useState(false);

  useEffect(() => {
    getCurrentUser()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
  }, []);

  if (!user) {
    return <LoginView checking={checking} onLoggedIn={setUser} />;
  }

  const avatar = Array.from(user.display_name)[0] ?? "用";
  const visibleNavigation =
    user.role === "admin"
      ? navigation
      : navigation.filter((item) => item.href !== "/admin");

  async function handleLogout() {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await logout();
    } catch {
      // The local token is cleared even if the server is temporarily unavailable.
    } finally {
      setUser(null);
      setLoggingOut(false);
    }
  }

  return (
    <div className={railCollapsed ? "app-shell rail-collapsed" : "app-shell"}>
      <aside className="side-rail">
        <Link href="/" className="brand" aria-label="数衡智能问数首页">
          <span className="brand-mark">衡</span>
          <span>
            <strong>数衡</strong>
            <small>BankInsight</small>
          </span>
        </Link>

        <nav className="side-nav" aria-label="主导航">
          <p className="nav-caption">工作空间</p>
          {visibleNavigation.map((item) => {
            const active =
              item.href === "/"
                ? pathname === "/"
                : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={active ? "nav-item active" : "nav-item"}
              >
                <span className="nav-mark">{item.mark}</span>
                <span>{item.label}</span>
              </Link>
            );
          })}
        </nav>

        <button
          type="button"
          className="rail-toggle"
          onClick={() => setRailCollapsed((current) => !current)}
          aria-label={railCollapsed ? "展开侧边栏" : "收起侧边栏"}
          title={railCollapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          <span aria-hidden="true">{railCollapsed ? "›" : "‹"}</span>
          <span>{railCollapsed ? "展开" : "收起侧栏"}</span>
        </button>

        <div className="rail-foot">
          <div className="security-badge">
            <span className="status-dot" />
            <span>
              <strong>安全查询链路</strong>
              <small>只读执行 · 全程留痕</small>
            </span>
          </div>
        </div>
      </aside>

      <main className="app-main">
        <header className="topbar">
          <div>
            <p className="eyebrow">经营分析</p>
          </div>
          <div className="topbar-actions">
            <div className="profile">
              <span className="avatar">{avatar}</span>
              <span>
                <strong>{user.display_name}</strong>
                <small>{user.username}</small>
              </span>
            </div>
            <button
              type="button"
              className="sign-out-button"
              onClick={() => void handleLogout()}
              disabled={loggingOut}
            >
              {loggingOut ? "正在退出…" : "退出登录"}
            </button>
          </div>
        </header>
        <div className="page-content">{children}</div>
      </main>

      <nav className="mobile-nav" aria-label="移动端主导航">
        {visibleNavigation.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={
              (item.href === "/" ? pathname === "/" : pathname.startsWith(item.href))
                ? "active"
                : ""
            }
          >
            <span>{item.mark}</span>
            {item.label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
