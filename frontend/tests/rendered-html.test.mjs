import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const templateRoot = new URL("../", import.meta.url);

async function render(pathname = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${pathname}`, {
      headers: { accept: "text/html", host: "localhost" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the product login", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>智能问数 · 数衡 BankInsight<\/title>/i);
  assert.match(html, /登录数衡/);
  assert.match(html, /请输入账号/);
  assert.match(html, /请输入密码/);
  assert.match(html, /analyst123/);
  assert.match(html, /og\.png/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/);
});

test("keeps the workbench, conversation sidebar and product metadata", async () => {
  const [
    page,
    layout,
    packageJson,
    workbench,
    appShell,
    queryResult,
    adminView,
    historyView,
  ] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../app/components/Workbench.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/AppShell.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/QueryResult.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/AdminView.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/HistoryView.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(page, /<Workbench \/>/);
  assert.match(layout, /数衡 BankInsight/);
  assert.match(layout, /\/og\.png/);
  assert.match(workbench, /conversation-sidebar/);
  assert.match(workbench, /新建对话/);
  assert.match(workbench, /今天/);
  assert.match(workbench, /getHistory/);
  assert.match(workbench, /getSessionHistory/);
  assert.match(workbench, /conversation-turn-list/);
  assert.doesNotMatch(workbench, /hero-metrics|example-row/);
  assert.match(workbench, /从一句业务问题，到一份/);
  assert.match(appShell, /handleLogout/);
  assert.match(appShell, /LoginView/);
  assert.ok(
    queryResult.indexOf("最终回答") < queryResult.indexOf("chart-card"),
    "最终回答应显示在图表分析之前",
  );
  assert.match(queryResult, /查看生成 SQL 语句/);
  assert.match(queryResult, /answer-trust-strip/);
  assert.match(queryResult, /useState<"chart" \| "table">\("table"\)/);
  assert.doesNotMatch(queryResult, /规则回答|查看查询逻辑|trust-card/);
  assert.match(adminView, /risk_desc/);
  assert.match(adminView, /中高风险/);
  assert.doesNotMatch(adminView, /permission-card|管理与审计|CONTROL CENTER/);
  assert.doesNotMatch(historyView, /QUERY ARCHIVE|<h1>历史报告/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  await assert.rejects(access(new URL("../app/_sites-preview", templateRoot)));
});
