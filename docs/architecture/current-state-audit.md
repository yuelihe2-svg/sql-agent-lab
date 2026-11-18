# 当前实现审计

审计日期：2026-11-18。范围是本仓库 `sql-agent-lab` 的源码与离线测试。本轮没有改 Agent，没有调用模型、数据库或 Benchmark，也没有重新执行 `pytest`。下文“已实现”表示源码行为与已有测试断言一致，不表示本轮重新跑通了整套测试。

产品工作流与研究复现是两条路径。网页和 `ReferenceMacSQLAgent` 使用 `reference_baseline.py` 的角色提示词。`experiments/macsql_reproduction.py` 的 `OriginalMacSQLAgent` 固定上游提示词，不进入默认网页。

## 运行形态

当前应用是单进程、单活动任务、只绑定回环地址的本地问答台。

| 事实 | 证据 |
| --- | --- |
| HTTP 使用标准库 `ThreadingHTTPServer`，监听 `127.0.0.1` | `live_ui.py` `main()` |
| 同一进程同时只允许一个 `active` 任务 | `Observatory.start()`，`tests/test_task_lifecycle.py` `test_http_stop_is_targeted_idempotent_and_waits_for_worker` |
| 一次运行落成 `manifest.json`、`task.json`、`trace.jsonl`、`result.json`；有参考答案时另写 `evaluation-evidence.json` | `Observatory.start()`、`Observatory._work()` |
| 输出目录必须在仓库外 | `Observatory.__init__` |
| 服务重启不续跑；未完成的本地任务只读显示为 `interrupted` | `task_lifecycle.task_view()`，`test_restart_projects_interruption_without_rewriting_history` |
| 依赖锁定为 `langgraph==1.2.19`、`httpx`、`sqlglot`；锁文件带有 `langgraph-checkpoint`，应用没有构造 checkpointer | `pyproject.toml`、`requirements-lock.txt`、`reference_core.py` `graph.compile()` |

## 功能矩阵

状态只使用三种：已实现、部分实现、未实现。

| 能力 | 状态 | 源码 | 测试 |
| --- | --- | --- | --- |
| Selector / Decomposer / Refiner | 已实现 | `reference_core.py` 节点 `select_schema` → `decompose` → `execute`，条件边进入 `refine`；行为在 `reference_baseline.py` `_select`、`_decompose`、`_next`、`_refine` | `tests/test_macsql_reference_baseline.py`：小 schema 跳过 Selector，执行错误进入 Refiner；`test_execution_repair_only.py`：最多修复一次，坏候选保留原 SQL |
| 完整 schema 可放入上下文时跳过 Selector | 已实现 | `_select()` 用 `count_tokens` 与 `input_budget` 比较 | `test_last_subsql_is_answer_and_small_schema_skips_selector` |
| 无执行或类型错误时不调用执行修复 Refiner | 已实现 | `_next()`：`bad` 为类型 mismatch 或 `feedback.status != ok` | `test_execution_repair_only.py` `test_valid_empty_and_null_need_only_generation`；`test_role_comparison.py` `test_empty_result_does_not_trigger_model_recheck` |
| 语义复核默认关闭 | 已实现 | `WorkflowConfig.semantic_review = False`；`Observatory.start()` 读取请求，缺省 `False`；用户页写死 `semantic_review:false` | `test_execution_repair_only.py` `test_deepseek_worker_defaults_to_no_review_and_three_call_cap`；`test_role_comparison.py` `test_empty_result_does_not_trigger_model_recheck` 断言 `semantic_status == disabled` |
| 观察台仍可打开语义复核 | 已实现 | `live_ui.html` 复选框写入 `semantic_review` | 语义复核行为由 `tests/test_macsql_semantic_review.py` 覆盖；开关本身是页面行为 |
| SQLite 只读执行 | 已实现 | `tools.py` `mode=ro`、`PRAGMA query_only=ON`、authorizer、`conn.execute` 单语句 | `tests/test_macsql_live_ui.py` `test_trace_reports_actual_sql_results_and_denied_writes`；`tests/test_macsql_reproduction.py` `test_read_only_rejects_write_and_database_remains_unchanged` |
| 参考 SQL 不进入 Agent | 已实现 | `Question` 只有 `question_id, db_id, question, evidence`；`Reference` 只给评分器 | `test_data_sources.py` 断言 `asdict(question)` 不含 SQL；`test_role_comparison.py` `test_reference_object_is_not_an_agent_input`；`test_trace_fixes.py` `test_session_reuses_same_sql_and_gold_and_keeps_evidence_out_of_trace` |
| BIRD 集合评分，忽略顺序和重复行 | 已实现 | `dataset.score_sql()`、`evaluation_audit.EvaluationSession.score()`，`SCORER = complete_row_set_v1_ignore_order_and_duplicates` | `test_trace_fixes.py` `test_set_scoring_and_fingerprints_preserve_numeric_equality_and_text_ids`、`test_limits_and_reference_failures_never_become_zero` |
| 任务完成与答案正确分开 | 已实现 | `result_task_state()`：`needs_review` 仍是任务 `completed`；`score` 独立 | `test_task_lifecycle.py` `test_completed_task_is_independent_of_answer_score`、`test_type_review_does_not_become_task_failure` |
| 开发者观察台 | 已实现 | `GET /` 用户页，`GET /observatory`，`GET /analysis` | `test_macsql_live_ui.py` 检查两个页面；`test_recorded_ui.py` 检查只读历史 |
| 多 SQLite 数据源注册 | 已实现 | `data_sources.SourceRegistry`，只接受 `kind=sqlite`，目录不返回路径 | `test_data_sources.py` `test_standalone_http_two_sources_without_bird` |
| PostgreSQL / MySQL | 未实现 | `SourceRegistry.load()` 对非 sqlite 直接报错 | `test_reject_bad_source_config_without_modifying_files` 覆盖非法配置，不含其他引擎 |
| 多用户、身份与权限 | 未实现 | 无 User、会话令牌或数据源授权；任意能访问本机端口的进程可调用 API | `test_browser_write_endpoints_reject_foreign_origin_and_invalid_body` 只检查 Host/Origin |
| 持久化多轮会话 | 未实现 | 每次 `Question('custom', db, text)`；`question_id` 固定为 `custom` | 用户页用 `question_id === 'custom'` 过滤最近 20 条运行，见 `user_ui.html` `renderHistory` |
| LangGraph Checkpointer | 未实现 | `graph.compile()` 无 checkpointer；`run()` 调用 `graph.invoke` | 中断测试证明进程退出后不恢复图状态 |
| 跨轮 Conversation Memory | 未实现 | 图状态只活在一次 `invoke`；修复轮能看到本题的 SQL 与 feedback | 无跨题记忆测试 |
| Context 预算与 schema 压缩 | 已实现 | `context.ContextPolicy`，`reference_baseline._fit()` 四级压缩，标识符保留 | `test_trace_budget_schema.py` `test_larger_output_still_respects_context_limit`；`test_review_fit_does_not_trade_types_for_budget` |
| Schema 检索 | 部分实现 | `tools.search_schema()` 用内存 FTS5 + BM25；`lookup_values()` 最多两次字面量 | `tests/test_macsql_retrieval.py`。这是词法检索，没有向量索引，也没有独立 RAG 服务 |
| SQL Policy Engine | 部分实现 | 执行门禁是 SQLite authorizer、长度、超时和预览上限；`sqlglot` 用于诊断和结果风险，不作为执行前的统一策略层 | 写操作与诊断多语句有测试；执行路径的函数白名单没有测试 |
| 查询预算 | 部分实现 | 工具行数/字节、生成 token、`max_repairs`、网页单任务。DeepSeek `Ledger` 只属于付费命令行 | `test_deepseek_api.py` 覆盖 Ledger；本地网页没有按用户的费用账本 |
| Prompt injection 防护 | 部分实现 | 系统句声明数据库内容是任务数据；工具不接受模型传入的路径。问题原文拼进提示词 | 无注入隔离或策略拒绝测试 |
| 敏感数据保护 | 部分实现 | 参考 SQL 以哈希进入评分证据；预览截断。trace 和 `/api/run` 仍返回提示词、schema 样本和结果预览 | `test_json_contract_usage_and_secret_not_logged` 覆盖 API key；样本脱敏没有测试 |
| FastAPI | 未实现 | `http.server.BaseHTTPRequestHandler` | HTTP 测试通过 `live_ui` 测试客户端 |
| SSE / token 流 | 未实现 | 前端 `refresh()` 每秒轮询；`LocalOllamaModel` 请求 `"stream": False` | `test_task_races.cjs` 覆盖轮询乱序，不覆盖 SSE |
| ChatGPT 式多轮界面 | 部分实现 | `user_ui.html` 有数据源、单问题输入、最近查询、简洁/详细视图 | `test_user_ui.cjs`、`test_source_ui.cjs`。一次运行对应一个问题，没有消息线程 |
| MCP Server / Skills Registry | 未实现 | 工具是 `SQLiteTools` 方法，经 `ObservedTools` 记 trace。仓库没有 MCP 入口或技能注册表 | 无 |
| Agent 质量评测 | 已实现 | BIRD/自定义参考答案走 `EvaluationSession`，与任务状态分开 | `test_trace_fixes.py`、`test_data_sources.py` 无参考答案时 `score is None` |
| 系统可靠性评测 | 部分实现 | 生命周期、停止、崩溃投影、来源校验、离线网络钩子分散在测试里，没有独立的可靠性套件入口 | `test_task_lifecycle.py`、`conftest.py`、`test_data_sources.py` |

## 三角色数据流

1. `user_ui.html` `startQuery()` 发送 `POST /api/start`，体为 `db_id`、`question`、`retrieval:true`、`semantic_review:false`。浏览器不提交文件路径或参考 SQL。
2. `Observatory.start()` 校验已注册 `db_id` 和 1–4000 字问题，创建运行目录和 `manifest.json`，启动守护线程，立即返回运行 ID。
3. `_work()` 绑定该题的 SQLite，包一层 `ObservedTools`，构造 `LocalOllamaModel` 与 `WorkflowConfig`，调用 `ReferenceMacSQLAgent.run()`。
4. `_select()` 读取并抽样 schema。网页默认 `schema_retrieval=True`。完整提示放得下则跳过 Selector 模型。
5. `_decompose()` 取最后一个 fenced SQL。`_execute()` 先 `compile_sql_readonly`，通过后再 `execute_sql_readonly`，并做类型与结果风险检查。
6. `_next()` 只在执行失败或类型 mismatch、且修复次数未用尽时进入 Refiner。语义复核仅在配置开启、执行成功且类型通过时进入。
7. 返回的 SQL 再执行一次写入 `result.json`。没有 `Reference` 时不评分。

`graph.compile()` 的图没有持久化状态。trace 是观察日志，不能作为可恢复的 checkpoint。

## 数据与上下文

`SourceRegistry` 在启动时读取 JSON。`id` 限制为 1–100 位字母、数字、下划线和连字符。相对路径相对配置文件解析。加载时检查文件头 `SQLite format 3`，并用 `mode=ro` 读一条 schema。修改注册表需要重启。

`ContextPolicy` 用 UTF-8 字节上界估计输入，默认窗口 32,000；网页覆盖为 `input_budget=14000`、`context_window=16384`。`_fit()` 在四级 schema 文本中选择能放下的一级，放不下则抛 `ContextBudgetExceeded`。样本和描述会先被丢掉，标识符保留。

`search_schema()` 把表列名、说明和有界样本放进临时 FTS5。查询词来自问题的词法切分，并转义后作为 FTS 字面量。注释写明结果只是提示，调用方仍要用完整 schema 校验标识符。`lookup_values()` 要求表列真实存在，对 `%`、`_`、`\` 做 LIKE 转义，最多返回 5 个截断值。

## HTTP 表面

| 方法与路径 | 行为 |
| --- | --- |
| `GET /` | `user_ui.html` |
| `GET /observatory` | `live_ui.html` |
| `GET /analysis` | `analysis.html`；分析文件来自启动参数，查询串不能选文件 |
| `GET /api/catalog` | 数据源公开字段、是否有题库、题目与 evidence；测试断言不含路径和参考 SQL |
| `GET /api/runs`、`GET /api/run?id=` | 任务、manifest、事件、结果、评分证据 |
| `POST /api/start`、`POST /api/stop` | 启动与协作停止 |

GET 要求 `Host` 精确等于 `127.0.0.1:<port>`。POST 还要求 `Origin` 缺省或等于该源，请求体最大 20,000 字节。响应带 `nosniff` 和较紧的 CSP。没有 `Access-Control-Allow-Origin`。运行 ID 只在服务自己扫描到的目录映射里查找；记录批次还有 `path.parent != root` 检查，见 `test_recorded_ui.py` `test_recorded_ids_cannot_escape_registered_root`。

停止是协作式的。`ObservedTools` 在下一次工具调用前观察 `stop`；模型请求开始前也会观察。已经发出的 Ollama 请求没有取消接口，`httpx` 超时为 600 秒。

## 现有安全控制

这些控制已经在源码中，并有对应测试：

- 数据库路径只来自服务端注册表，`db_id` 是允许列表的键。`tools.connection()` 注释明确模型不能提供路径。
- 查询连接使用 URI `mode=ro`、`query_only`、SQL 长度限制、进度回调超时。`query=True` 时 authorizer 只允许 `SQLITE_SELECT`、`SQLITE_READ`、`SQLITE_FUNCTION`、`SQLITE_RECURSIVE`，并拒绝名为 `load_extension`、`readfile`、`writefile` 的函数。
- `DELETE` 被拒绝且文件字节不变：`test_trace_reports_actual_sql_results_and_denied_writes`。
- 诊断器用 `sqlglot` 要求单条 `Query`，因此 `SELECT 1; DELETE` 在诊断路径被拒绝：`test_multiple_statements_and_writes_are_rejected`。
- 预览默认 5 行、12 列、单元格 160 字。类型剖析最多 1,000 行。评分完整结果超过 100,000 行或 32,000,000 字节时失败，不允许用预览冒充分数。
- 本地模型适配器拒绝非回环 Ollama、非 GGUF 和远程模型：`test_local_adapter_rejects_external_server_before_request`。
- 测试进程用 `sys.addaudithook` 阻断除本进程回环端口以外的连接：`conftest.py`。
- DeepSeek key 不写入 ledger：`test_json_contract_usage_and_secret_not_logged`。
- 用户页用 `textContent` 渲染问题、错误和结果说明。

## 安全问题

严重程度按当前部署判断：服务只监听回环地址，使用者也是本机操作者。一旦绑定地址或部署方式改变，前两项会升为高。

### S1 回环 API 没有调用方身份

- 严重程度：中。
- 影响：同一台机器上的任意进程可以列出全部运行、读取 SQL、提示词、schema 样本、结果预览和评分样例行，并启动或停止任务。
- 证据：`Handler` 没有身份字段；`read_run()` 返回 manifest、events、result、`evaluation_evidence`。
- 最小修复：保持只绑定 `127.0.0.1`。在被 Git 忽略的本地配置中增加服务端持有的本地令牌，所有 `/api/*` 要求该令牌，日志不记录它。多用户模型落地前不要把监听地址改出回环。

### S2 跟踪与接口携带抽样数据

- 严重程度：中。
- 影响：`call_started.messages`、`schema_loaded.schema` 里的 `observed_samples`、执行预览和 `evaluation-evidence.json` 的最多 3 行样例会进入磁盘和浏览器。参考 SQL 本体不在 `Question` 中，评分工件保存的是 `reference_sql_sha256`。
- 证据：`reference_baseline._ask_reference()`、`tools._enrich()`、`evaluation_audit.examples()`、`live_ui._read_run()`。
- 最小修复：用户接口返回红acted 视图，只保留状态、有界预览和错误类别。完整 trace 留在开发者观察台。先改投影，不改 Agent 写入的研究 trace 格式，避免破坏现有夹具。

### S3 执行门禁是动作允许列表加三个函数名，不是统一策略

- 严重程度：中。
- 影响：通过 authorizer 的 `SELECT` 仍可调用除三个名字以外的 SQLite 函数。某些 SQLite 构建历史中存在危险函数；本仓库没有按当前 `sqlite3.sqlite_version` 做函数白名单。`sqlglot` 只约束诊断和风险检查。
- 证据：`SQLiteTools.connection()` 的 `authorize()`；`execute_sql_readonly()` 在 authorizer 连接上直接 `conn.execute`。
- 测试缺口：`execute_sql_readonly('SELECT 1; DELETE ...')` 没有直接断言。CPython `execute()` 对多语句抛 `ProgrammingError`，而该方法捕获 `sqlite3.Error` 后返回 `status=error`，因此预期不会执行第二条；这仍应补一条回归。
- 最小修复：单独的安全 PR，在现有执行函数之前增加只读语句分类和函数白名单，默认仍走现有 `SQLiteTools`。用现有 `SELECT`、聚合、`WITH`、`DELETE`、`ATTACH` 和多语句夹具证明行为。不改提示词和评分器。

### S4 超时是协作式的，模型请求不能中途取消

- 严重程度：低。
- 影响：进度回调每 100 条虚拟机指令检查一次。网页把工具超时设为 30 秒，诊断路径仍可要求 2 秒。Ollama 超时 600 秒。唯一工作线程在此期间不接受新任务。
- 证据：`ToolLimits`、`Observatory._work()` 的 `ToolLimits(timeout_seconds=30)` 与 `httpx` `timeout=600`；`request_stop()` 只设置事件。
- 最小修复：停止时关闭当前 `httpx` 客户端，并为一次运行增加墙钟预算。保持“已越过检查点的任务可以正常完成”这一现有语义，用 `test_task_lifecycle.py` 锁定。

### S5 问题文本直接进入角色提示词

- 严重程度：低，属于 Text-to-SQL 的既有残余风险。
- 影响：问题可以要求模型选择敏感列或忽略原说明。当前能限制的是只读执行、预览和“路径不由模型提供”。没有独立的注入分类器。
- 证据：`_decomposer_prompt()` 把 Question、Evidence、Schema 连成一个用户消息。
- 最小修复：后续上下文构造把不可信问题放进固定分隔区，并继续禁止工具参数携带连接信息。第一批工程 PR 不改现有提示词正文。

### S6 运行 ID 熵不足以支撑未来的远程访问

- 严重程度：信息。当前配合回环和无 CORS，不构成远程读取。
- 证据：`datetime` 秒级时间加 `uuid4().hex[:6]`。
- 最小修复：多用户 PR 使用完整 UUID，并按所有权授权，而不能只靠难猜的 ID。

观察台可以显式打开语义复核，用户页写死关闭，服务端缺省也是关闭。这符合“默认关闭”，不是缺陷。若产品决定用户工作区永不开启，应在用户入口忽略该字段；那是后续策略 PR，不是本轮改动。

## 明确不在本轮改动的边界

- `reference_baseline.py` 的三角色提示词。
- `dataset.score_sql()` 与 `evaluation_audit.py` 的集合比较协议。
- BIRD 校验、`source-lock.json` 和复现模式上游提示词。
- 语义复核默认值。
- 只读 SQLite 工具的现有限额和 authorizer，直到单独的安全 PR 用回归测试锁定差异。
