# 目标架构

本文描述从当前单机原型走向多用户、多数据源、可持久化会话的 AI Data Workspace。它是设计，不是已落地行为。当前行为以 [current-state-audit.md](current-state-audit.md) 为准。

第一批实现只增加可关闭的边界和并行存储。Selector、Decomposer、Refiner 的提示词、路由条件、SQL 评分器和 BIRD 协议保持不动。语义复核继续默认关闭。

## 目标

一个 Workspace 容纳多个用户和多个只读数据源。用户在某个数据源上拥有多轮 Conversation。每一轮用户消息启动一个 AgentRun。AgentRun 仍然跑现有三角色图。系统把运行过程持久化，并能在进程重启后查询历史；自动续跑仍默认关闭，与今天的 `interrupted` 语义一致。

PostgreSQL 和 MySQL 通过新的 Adapter 接入。第一阶段 Adapter 只有 SQLite，内部委托给现有 `SQLiteTools`。

## 逻辑组件

```text
Browser / future MCP client
        |
        v
API 边界（先兼容现有 HTTP，再加 FastAPI）
        |
        +-- 身份与 Workspace 授权
        +-- Conversation 服务
        +-- 任务生命周期（现有 state machine）
        |
        v
Agent 应用服务
        |
        +-- Context Builder（预算、历史摘录、schema 文本）
        +-- ReferenceMacSQLAgent（现有图，checkpointer 默认关闭）
        |
        v
Tool Gateway
        |
        +-- SQL Policy
        +-- Database Adapter（SQLite 已有；PostgreSQL/MySQL 以后）
        |
        v
审计与工件
        +-- 现有 trace/result 文件
        +-- 新的 AuditEvent 与应用元数据
```

模型仍然不持有数据库连接。`db_id` 继续由服务端从已授权数据源填入，工具函数不接受路径、URI 或连接串。

## 与现有代码的对应

| 目标组件 | 现在放在哪里 | 迁移方式 |
| --- | --- | --- |
| 三角色图 | `reference_core.py`、`reference_baseline.py` | 继续作为唯一 SQL 生成图。新代码通过构造 `WorkflowConfig` 调用它 |
| 工具 | `SQLiteTools`、`ObservedTools` | `SQLiteAdapter` 委托现有方法；`ObservedTools` 仍负责 trace |
| 数据源 | `SourceRegistry` | 启动时继续读 JSON。应用库只保存 id、名称、种类和秘密引用，不保存路径到 API |
| 任务 | `task_lifecycle.py` | 状态名和转移表保持不变。新表是同一状态的另一个投影 |
| 上下文 | `ContextPolicy`、`_fit()` | 继续负责单次提示是否放得下。跨轮摘录在进入 `_fit()` 之前组装 |
| 检索 | `search_schema()`、`lookup_values()` | 作为 Schema RAG 的第一版检索器 |
| 评分 | `EvaluationSession` | 只在有参考答案的评测运行中调用 |
| HTTP | `live_ui.py` | 保持现有路径和状态码，直到兼容层测试通过 |

研究复现入口 `OriginalMacSQLAgent` 不接入 Workspace 运行时。

## 一次对话请求

1. API 解析调用方，确认该用户对 Workspace 和 DataSource 有 `can_query`。
2. 写入 Message，创建 AgentRun，状态沿用 `new_task()` 的 `running`。
3. Context Builder 读取本对话已完成轮次的短摘录、当前问题，以及 Adapter 的 schema。摘录默认关闭，关闭时提示词与今天相同。
4. 现有图执行。Tool Gateway 在调用 `compile_sql_readonly` 和 `execute_sql_readonly` 前做策略检查；策略默认先观察，不改变今天已经允许的语句，直到安全 PR 单独打开强制模式。
5. 结果写入现有 `result.json`，同时写 Message 和 AuditEvent。
6. 客户端继续可用轮询。SSE 只转发已经产生的任务、阶段和结果事件，不伪造 token 流。

## Checkpointer

`langgraph-checkpoint` 已在锁文件中，因为 `langgraph==1.2.19` 依赖它。应用现在调用无检查点的 `graph.compile()`。

目标是可选检查点：

- 默认 `checkpointer=None`，`invoke` 行为与现在一致。
- 打开时，一个 AgentRun 使用自己的 `thread_id`，检查点写在应用库或运行目录，不写进业务 Message。
- 检查点用于崩溃后的人工排查和将来的显式恢复。
- 服务启动仍然不自动调用模型、不自动重放中断任务。这是 `task_lifecycle.py` 已经保证的产品语义。

## 多数据源

Adapter 对图只暴露今天 `SQLiteTools` 已有的操作：加载 schema、检索、取值、编译、只读执行、诊断、以及仅供评分器使用的完整结果。图和提示词继续按 SQLite 方言生成，直到有单独授权的方言实验。因此 PostgreSQL/MySQL 设计先落在接口、连接安全和只读账号上，不在本阶段改 Decomposer 提示词。

SQLite 继续使用文件注册表和 `mode=ro`。网络数据库使用只读账号、TLS、语句超时和单语句限制。连接秘密放在仓库外的秘密引用里。

## MCP 与 Skills

MCP 是工具的另一种调用面，不是第二套 SQL 引擎。Skills 是一组命名的 `WorkflowConfig` 开关，不是另一套提示词。

内部工具名保持 `ObservedTools.names`：

- `load_schema`
- `search_schema`
- `lookup_values`
- `compile_sql_readonly`
- `execute_sql_readonly`
- `diagnose_sql_readonly`

MCP server 以后可以暴露同名只读工具。调用时服务端注入已授权的 `db_id`。模型或外部客户端若传入路径，服务器丢弃并记 AuditEvent。MCP 不暴露 `full_result_set`，避免把评分用的完整结果交给模型。

默认 Skill 名为 `execution_repair_only`：`schema_retrieval=true`、`semantic_review=false`、`max_repairs=1`，与当前用户页和 `Observatory.start()` 缺省一致。`semantic_review` Skill 只在开发者观察台、且调用方有 developer 角色时可见。用户页继续不提供这个开关。

Skills Registry 第一版可以是代码内的冻结表，加上测试断言默认项。它不从任意目录加载可执行文件。

## 进程与部署边界

近期仍是一个本地进程、一个写者、一个输出目录。应用元数据使用单独的 SQLite 文件，放在输出目录或 `.local/`，不进入 Git。

以下事项需要单独决定后才能做，不包含在早期 PR 中：

- 监听 `127.0.0.1` 以外的地址。
- 多个进程同时写一个输出目录。
- 自动恢复中断的 AgentRun。
- 改变角色提示词、评分器或 BIRD 协议。
- 调用真实模型或重新跑 Benchmark。

## 兼容承诺

现有离线测试继续以今天的模块路径运行。新接口在旗标关闭时不改变 `POST /api/start` 的请求、任务状态和结果文件。旗标打开时，旧文件仍然写入，新表只是附加投影。关闭旗标或回滚迁移即可回到当前行为。
