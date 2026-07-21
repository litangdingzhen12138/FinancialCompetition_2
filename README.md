# 银行业 Text2SQL

这是一个面向当前银行智能问数赛题的最小可运行实现：

```text
自然语言问题
  → session上下文补全
  → 高置信规则QueryPlan
  → 确定性SQL编译
  → 多层统一校验
  → DuckDB只读执行
  → 结果校验与答案格式化

规则未完整命中或失败
  → LLM生成QueryPlan + SQL
  → 同一套校验、执行与结果验证
```

当前版本不使用 LangGraph。多轮对话通过 `session_id + 结构化SessionState` 实现；默认进程内存储，后续可以将 `InMemorySessionStore` 替换为 Redis。

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
python -m text2sql.cli "江苏省A市农商行在2025年6月15日，各项存款余额是多少？"
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
uvicorn text2sql.api:app --host 0.0.0.0 --port 8000
```

请求：

```json
POST /query
{
  "question": "它们的不良率呢？",
  "session_id": "session-1"
}
```

## LLM兜底配置

只有规则无法完整覆盖时才需要LLM：

```env
TEXT2SQL_LLM_URL=https://api.deepseek.com/chat/completions
TEXT2SQL_LLM_API_KEY=your-deepseek-api-key
TEXT2SQL_LLM_MODEL=deepseek-v4-flash
```

LLM必须返回结构化QueryPlan和SQL。生成结果不会直接执行，仍需经过与规则SQL相同的完整校验链路。默认允许一次初始生成和一次带脱敏反馈的修复。

## 测试

```powershell
pytest -q
```

测试覆盖真实数据点查、排名方向、期间变化、派生比率、省均值、机构全省排名、多轮继承、SQL安全和LLM兜底契约。
