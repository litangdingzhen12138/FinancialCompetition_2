# 银行业 Text2SQL

这是一个面向当前银行智能问数赛题的最小可运行实现：

```text
自然语言问题
  → session上下文补全
  → 高置信规则QueryPlan
  → 确定性SQL编译
  → 业务角色、指标与机构范围执行前鉴权
  → 多层统一校验
  → DuckDB权限视图内只读执行
  → 结果校验与答案格式化

规则未完整命中或失败
  → LLM生成QueryPlan + SQL
  → 同一套校验、执行与结果验证
```

当前版本不使用 LangGraph。多轮对话通过 `session_id + 结构化SessionState` 实现；默认进程内存储，后续可以将 `InMemorySessionStore` 替换为 Redis。

## 账户密码

| 业务身份 | 默认账号 | 默认密码 | 数据范围 |
|---|---|---|---|
| 总行管理层 | `analyst` | `analyst123` | 21项指标、全部机构 |
| 分支行管理层 | `analyst2` | `analyst2123` | 21项指标、配置机构 |
| 业务条线人员 | `analyst3` | `analyst3123` | ZB003～ZB006、ZB018～ZB021，配置机构 |
| 风险合规人员 | `risk` | `risk123` | ZB013～ZB017、全部机构 |
| 财务人员 | `finance` | `finance123` | ZB001～ZB002、ZB007～ZB012、全部机构 |
| 系统管理员 | `admin` | `admin123` | 仅系统管理和审计，不可查询业务数值 |

以上均为比赛演示账号。任何联网或生产部署必须通过环境变量更换全部默认口令，
并保持 `TEXT2SQL_ALLOW_HEADER_AUTH=false`；当前内存令牌服务不替代银行统一身份认证。

## 主要能力

- Excel首次启动自动构建 DuckDB，后续按源文件大小和修改时间复用；
- 不导入“问题答案清单”，防止标准答案泄漏到查询路径；
- 支持单值、排名、Top-N、较年初/上月/上季/同期、增量、增幅、全省均值、派生比率、日均和季度趋势等规则计划；
- 支持机构、指标、日期和上一轮结果的结构化继承；
- 规则和 LLM SQL统一经过QueryPlan校验、AST只读校验、表白名单、计划—SQL一致性、DuckDB `EXPLAIN`预检、结果形状校验；
- LLM使用OpenAI兼容接口，仅在规则未覆盖或失败时调用；
- 提供Python入口、CLI和FastAPI接口。

## 安装

```powershell
python -m pip install -r requirements.txt
或者
uv sync
```

复制根目录的 `.env.example` 为 `.env` 后填写密钥。配置加载顺序为：根目录 `.env` 优先，未在 `.env` 中设置的项目再读取系统环境变量。

## 构建数据库

```powershell
python -m text2sql.cli --build-db
```

默认数据集是：

```text
比赛数据/22-多模态技术与数据治理赛道-江苏农商联合银行-基于大模型与NL2SQL的银行业智能问数系统构建与应用/基于大模型与NL2SQL的银行业智能问数系统构建与应用_数据集.xlsx
```

可在根目录 `.env` 中配置，系统环境变量作为缺失项的后备：

```env
TEXT2SQL_XLSX_PATH=D:\path\dataset.xlsx
TEXT2SQL_DB_PATH=D:\path\bank_metrics.duckdb
```

## CLI

```powershell
python -m text2sql.cli "把江苏省E市农商行2025年10月31日的不良率、拨备覆盖率、逾期率和资本充足率都列出来，并告诉我各自在全省排第几"
python -m text2sql.cli "2026年3月末，哪家农商行的不良贷款率最低？" --json
```

## Python与多轮对话

```python
from text2sql import Text2SQLService

service = Text2SQLService()
print(service.ask("截至2026-03-31，各项存款余额排名前三的是哪几家？", "session-1").answer)
print(service.ask("它们的不良率呢？", "session-1").answer)
```

兼容入口：

```python
import run

answer = run.run("江苏省A市农商行在2025年6月15日，各项存款余额是多少？")
```

## FastAPI

```powershell
python -m uvicorn text2sql.api:app --host 127.0.0.1 --port 8000
```

完整的认证、请求字段、响应字段、错误码、SSE 和中台调用示例见
[`docs/API标准化对接说明.md`](docs/API标准化对接说明.md)。启动后端后也可直接访问
`/docs`、`/redoc` 或 `/openapi.json`。

登录后的请求：

```json
POST /api/v1/queries
{
  "question": "它们的不良率呢？",
  "session_id": "session-1"
}
```

## 产品化工作台

项目现已增加版本化产品接口和响应式 PC/H5 工作台。旧 `/query` 已关闭，
`/api/v1` 能力包括：

- 问数全流程编排，查询数据和图表优先返回；
- 最终回答采用规则优先、LLM 兜底，未命中规则时通过独立 SSE 接口流式返回；
- 确定性图表智能推荐；
- 年、季度、月、日时间下钻，当前统一采用期末值口径；
- 查询历史、详情回溯、Excel/CSV 导出和限时分享；
- 六类业务身份的指标级、机构级权限，以及数据库执行范围强制约束；
- 分享查看者按当前登录身份重新鉴权；无明文权限者和系统管理员的结果字段动态脱敏，导出沿用同一权限结果；
- 查询、下钻、导出、分享和越权操作的哈希链审计；
- 七类异常规则告警、连续越权冻结和管理员处置闭环。

### 本地启动

前后端需要分别占用一个终端，并且都从项目根目录
`D:\PycharmProject\FinancialCompetition_2` 开始执行。

终端一，启动后端（端口 `8000`）：

```powershell
python -m uvicorn text2sql.api:app --host 127.0.0.1 --port 8000
```

后端接口地址：`http://127.0.0.1:8000`。

终端二，启动前端（端口 `3101`）：

```powershell
cd frontend
npm install  # 仅首次运行或依赖发生变化时需要执行
npm run dev -- --port 3101
```

浏览器访问：`http://localhost:3101`。

前端默认连接 `http://127.0.0.1:8000`，可通过
`frontend/.env.local` 中的 `NEXT_PUBLIC_API_BASE_URL` 修改。停止服务时，
在对应终端按 `Ctrl+C`。

产品历史和审计数据默认存储在 `data/product.sqlite3`，可通过
`TEXT2SQL_PRODUCT_DB_PATH` 修改。该 SQLite 存储用于比赛和单机演示；
正式银行部署时应通过持久化仓储适配器替换为目标国产数据库。
当前告警去重锁只保证单进程实例内的一致性，多 worker 或多实例部署应把规则
计数、告警写入和冻结改造成数据库事务或集中式风控服务。
生产环境前端来源通过 `TEXT2SQL_CORS_ORIGINS` 配置，多个来源用英文逗号分隔。

产品工作台的查询与最终回答采用两个独立链路：

```text
POST /api/v1/queries/stream
GET  /api/v1/queries/{query_id}/answer/stream
```

第一个接口完成 Text2SQL、SQL 执行和图表推荐。若已有确定性回答规则，
响应中的 `answer_mode` 为 `rule`、`answer_status` 为 `completed`，不会再次调用
LLM。未命中回答规则时，`answer_mode` 为 `llm`、`answer_status` 为 `pending`；
前端先渲染查询数据和图表，再使用第二个接口流式填充“最终回答”。最终回答
调用失败不会影响已返回的数据和图表，成功后会写回历史记录，供导出、分享和
历史回溯使用。

## LLM兜底配置

只有规则无法完整覆盖时才需要LLM：

```env
TEXT2SQL_LLM_URL=https://api.deepseek.com/chat/completions
TEXT2SQL_LLM_API_KEY=your-deepseek-api-key
TEXT2SQL_LLM_MODEL=deepseek-v4-flash
```

SQL规划阶段的LLM必须返回结构化QueryPlan和SQL。生成结果不会直接执行，
仍需经过与规则SQL相同的完整校验链路。默认允许一次初始生成和一次带脱敏
反馈的修复。最终回答阶段复用同一套模型连接配置，但只接收原始问题和已经
执行完成的SQL结果，并关闭深度思考；提示词要求仅依据结果、使用简洁银行
业务语言回答，不得编造原因或扩展无关结论。

## 测试

```powershell
pytest -q
```

测试覆盖真实数据点查、排名方向、期间变化、派生比率、省均值、机构全省排名、多轮继承、SQL安全和LLM兜底契约。
