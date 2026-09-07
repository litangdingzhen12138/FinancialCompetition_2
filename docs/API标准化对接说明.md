# BankInsight API 标准化对接说明

## 1. 适用范围

本文档用于数据中台、风控中台、营销中台、报表系统和 BankInsight 前端调用
智能问数服务。四类系统复用同一套 `/api/v1` 接口，不需要分别建设专用查数
接口。

默认本地地址：

```text
http://127.0.0.1:8000
```

启动后端后可查看：

- Swagger UI：`/docs`
- ReDoc：`/redoc`
- OpenAPI 3.1：`/openapi.json`

生产环境应通过银行 API 网关访问。当前 Caddy 配置只代理 `/api/*` 和
`/health`；如需在内网开放文档，应由网关单独配置 `/docs`、`/redoc` 和
`/openapi.json`，不建议向公网开放。

## 2. 认证

除登录和能力发现外，`/api/v1` 接口（包括分享访问）均使用 Bearer Token：

```http
Authorization: Bearer <access_token>
```

登录：

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "analyst",
  "password": "analyst123"
}
```

Header 认证默认关闭；生产部署应明确保留：

```env
TEXT2SQL_ALLOW_HEADER_AUTH=false
```

`X-User-Id`、`X-User-Role` 和 `X-Org-Scope` 仅用于本地演示或受信任网关注入，
不能直接信任浏览器自行传递的角色。
文档中的六组默认账号和口令也只供本地比赛演示，联网部署前必须全部替换；
当前内存访问令牌不替代银行统一身份认证。

## 3. 公共请求追踪

调用方可以通过请求头提供唯一请求编号：

```http
X-Request-ID: 20260823-risk-000001
```

也可以在智能问数请求体中传入 `request_id`。请求体值优先；未提供时后端自动
生成。所有 HTTP 响应都会返回：

```http
X-Request-ID: 20260823-risk-000001
```

`request_id` 最长 128 字符，可使用英文字母、数字、点、下划线、冒号和连字符。

## 4. 同步智能问数

系统间调用优先使用同步 JSON 接口：

```http
POST /api/v1/queries
Authorization: Bearer <access_token>
Content-Type: application/json
X-Request-ID: 20260823-risk-000001
```

请求：

```json
{
  "request_id": "20260823-risk-000001",
  "caller_system": "risk-platform",
  "question": "查询2025年四季度末不良率最低的五家机构",
  "session_id": "risk-platform-session-001"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `question` | 是 | 自然语言查询，1～2000 字符 |
| `session_id` | 否 | 多轮对话会话编号，最长 128 字符 |
| `request_id` | 否 | 调用链请求编号；未传时自动生成 |
| `caller_system` | 否 | 调用系统编码，如 `risk-platform` |

典型响应：

```json
{
  "api_version": "v1",
  "query_id": "7ea6c2f78ce84de195dfd43c07ebf205",
  "session_id": "risk-platform-session-001",
  "status": "completed",
  "question": "查询2025年四季度末不良率最低的五家机构",
  "route": "rule",
  "answer_mode": "rule",
  "answer_status": "completed",
  "generated_at": "2026-08-23T10:30:00+00:00",
  "duration_ms": 286,
  "plan": {},
  "sql": "SELECT ...",
  "columns": [],
  "records": [],
  "row_count": 5,
  "truncated": false,
  "visualization": {},
  "alternatives": [],
  "insight": {},
  "permissions": {},
  "warnings": [],
  "request_id": "20260823-risk-000001",
  "caller_system": "risk-platform"
}
```

完整字段和嵌套结构以 `/openapi.json` 中的 `ProductQueryResponse` 为准。

## 5. 流式智能问数

PC/H5 页面可继续使用：

```http
POST /api/v1/queries/stream
```

响应类型为 `text/event-stream`，事件顺序为：

| 事件 | 说明 |
|---|---|
| `status` | 理解问题、数据就绪等阶段信息 |
| `result` | 完整 `ProductQueryResponse` |
| `complete` | 查询完成，返回查询编号和回答状态 |
| `error` | 查询失败，返回错误码、说明和请求编号 |

当 `answer_mode=llm` 且 `answer_status=pending` 时，再调用：

```http
GET /api/v1/queries/{query_id}/answer/stream
```

最终回答事件包括 `answer_start`、`answer_delta`、`answer_done` 和
`answer_error`。

## 6. 查询、导出、分享与管理接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/v1/capabilities` | 服务能力发现 |
| POST | `/api/v1/auth/login` | 登录 |
| GET | `/api/v1/auth/me` | 当前用户 |
| POST | `/api/v1/auth/logout` | 注销 |
| POST | `/api/v1/queries` | 同步智能问数 |
| POST | `/api/v1/queries/stream` | 流式智能问数 |
| GET | `/api/v1/queries/{query_id}/answer/stream` | 流式最终回答 |
| POST | `/api/v1/queries/{query_id}/drill` | 时间下钻 |
| GET | `/api/v1/history` | 查询历史 |
| GET | `/api/v1/history/{query_id}` | 历史详情 |
| GET | `/api/v1/queries/{query_id}/export?format=xlsx` | Excel 导出 |
| GET | `/api/v1/queries/{query_id}/export?format=csv` | CSV 导出 |
| POST | `/api/v1/queries/{query_id}/share` | 创建限时分享 |
| GET | `/api/v1/shares/{token}` | 访问分享结果 |
| GET | `/api/v1/admin/overview` | 管理概览 |
| GET | `/api/v1/admin/audit` | 操作审计 |
| GET | `/api/v1/admin/alerts` | 安全告警列表 |
| PATCH | `/api/v1/admin/alerts/{alert_id}` | 确认或关闭告警 |
| GET | `/api/v1/admin/users` | 审计用户列表 |
| GET | `/api/v1/admin/metrics` | 当前完整指标清单 |
| POST | `/api/v1/admin/data-imports/preview?filename=...` | 校验并预览指标数据更新，正文为原始 `.xlsx` 字节 |
| POST | `/api/v1/admin/data-imports/publish?filename=...` | 发布指标数据更新；覆盖时增加 `confirm_overwrite=true` |

旧 `/query` 和 `/sessions/{session_id}` 固定返回410，避免形成匿名、无权限范围和无审计的旁路；本地批量回归请直接调用 Python/CLI 入口。

单次或批量导出超过200行时先返回409；调用方确认后追加 `confirm_large_export=true` 重试。连续越权触发冻结后，业务接口返回423及解冻时间。

## 7. 统一错误响应

JSON 接口错误保持原有 `detail` 字段，同时增加机器可读错误码和请求编号：

```json
{
  "code": "AUTH_FORBIDDEN",
  "detail": "无权访问该查询",
  "request_id": "20260823-risk-000001"
}
```

| HTTP 状态 | 默认错误码 | 说明 |
|---:|---|---|
| 400 | `REQUEST_INVALID` | 请求或查询问题不合法 |
| 401 | `AUTH_UNAUTHORIZED` | 未登录或令牌失效 |
| 403 | `AUTH_FORBIDDEN` | 权限不足 |
| 404 | `RESOURCE_NOT_FOUND` | 资源不存在 |
| 409 | `CONFIRMATION_REQUIRED` | 大批量导出或指标数据覆盖需要二次确认 |
| 410 | `LEGACY_ENDPOINT_DISABLED` | 旧版旁路接口已关闭 |
| 422 | `REQUEST_VALIDATION_ERROR` | 请求字段校验失败 |
| 423 | `ACCOUNT_FROZEN` | 异常访问触发临时冻结 |
| 429 | `RATE_LIMITED` | 请求过于频繁，由网关执行 |
| 500 | `SYSTEM_ERROR` | 系统内部错误 |
| 504 | `QUERY_TIMEOUT` | 查询超时，由网关或执行器执行 |

## 8. 四类系统调用示例

四类平台推荐使用以下 `caller_system`：

| 平台类型 | 推荐的 `caller_system` |
|---|---|
| 数据中台 | `data-platform` |
| 风控中台 | `risk-platform` |
| 营销中台 | `marketing-platform` |
| 报表系统 | `report-system` |

以上取值用于标识请求来源，便于日志检索和调用链排查，目前不是接口强制枚举。
正式对接时如果联合银行已有统一的系统编码，也可以改用其正式编码。四类系统
调用同一套查询接口，只需根据调用方修改 `caller_system` 和问题内容：

```json
{"caller_system":"data-platform","question":"查询2025年末各机构存款余额"}
```

```json
{"caller_system":"risk-platform","question":"查询不良率最高的五家机构"}
```

```json
{"caller_system":"marketing-platform","question":"查询存款增幅排名前十的机构"}
```

```json
{"caller_system":"report-system","question":"查询年末各机构资产质量指标明细"}
```

## 9. 更换赛方 LLM

当前默认插件为 `openai_compatible`。赛方给出完整 Chat Completions 地址时：

```env
TEXT2SQL_LLM_PROVIDER=openai_compatible
TEXT2SQL_LLM_URL=https://contest.example.com/v1/chat/completions
TEXT2SQL_LLM_API_KEY=sk-contest-xxx
TEXT2SQL_LLM_MODEL=contest-model
```

赛方只给 Base URL 时，不设置 `TEXT2SQL_LLM_URL`，改用：

```env
TEXT2SQL_LLM_BASE_URL=https://contest.example.com/v1
TEXT2SQL_LLM_API_KEY=sk-contest-xxx
TEXT2SQL_LLM_MODEL=contest-model
```

后端会自动拼接 `/chat/completions`。修改配置后需要重启服务。API Key 只能通过
服务端环境变量、容器 Secret 或银行密钥管理系统注入，不能放在查询请求体中。

非 OpenAI 兼容供应商应实现 `ChatModelClient`，无需修改 QueryPlan、SQL 模板、
安全校验和结果校验。

## 10. 数据源扩展边界

当前运行时仍使用 `DuckDBDataSourceAdapter`，数据库结构、规则 SQL 和校验逻辑
没有变化。`DataSourceAdapter` 仅提供后续替换点。银行物理表不同时，应先通过
视图、ETL 或数据中台映射成当前统一逻辑模型，再实现新的适配器；不能只替换
连接字符串后直接执行现有 SQL。
