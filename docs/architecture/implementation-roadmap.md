# 实施路线

原则：每个 PR 可以独立测试，也可以单独回滚。早期 PR 不改 `reference_baseline.py` 的提示词、`dataset.score_sql()`、`evaluation_audit.py` 的比较规则、BIRD 锁文件协议，以及语义复核的默认关闭。不调用付费模型、真实业务库或 Benchmark。

事实来源在并行期仍是运行目录里的 JSON。应用库是附加投影。回滚时删除新旗标和新文件即可，旧目录还能被当前 `Observatory` 读取。

下面的测试命令假设已经按 README 装好 `.venv`。文档 PR 没有测试命令。

## PR-00 架构文档

- 内容：`docs/architecture/` 下的审计和设计。
- 不改 Python、HTML 和测试。
- 验收：文档中的“已实现”都能在引用的文件里找到；目标行为被写成尚未落地。
- 回滚：删除该目录。

## PR-01 执行路径多语句回归

- 内容：给 `execute_sql_readonly` 增加一条测试：`SELECT 1; DELETE FROM ...` 返回 `status=error`，数据库字节不变。不改 `tools.py`，除非测试证明当前实现会执行写入；若会执行，停止并先按安全设计 S3 修门禁，不顺手改提示词。
- 验收：新测试失败时不能合并实现以外的重构。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_macsql_sql_diagnostics.py tests/test_macsql_live_ui.py`
- 回滚：删除测试。

## PR-02 领域类型

- 内容：按 `data-model.md` 增加冻结数据类和序列化，放在新模块，例如 `sql_agent_lab.workspace.models`。运行时不导入它。
- 验收：往返 JSON 不包含路径、口令、参考 SQL 字段。`Question` 仍不能构造出 gold SQL。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_workspace_models.py tests/test_data_sources.py`
- 回滚：删除新模块和测试。

## PR-03 只读投影

- 内容：从现有 `manifest.json`、`task.json`、`result.json` 构造 `AgentRun` 与一条用户 Message 的纯函数。不写应用库。
- 验收：`tests/test_task_lifecycle.py` 的临时目录能投影出相同 `state`、`score` 和“无参考答案则 score 为空”。投影函数不读取 `evaluation-evidence.json` 里的样例行。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_workspace_projection.py tests/test_task_lifecycle.py`
- 回滚：删除新模块。现有服务无感知。

## PR-04 应用库迁移，默认不挂到服务

- 内容：创建 `workspace.sqlite` 的迁移函数，表如数据模型。提供升级和空库删除。`live_ui.main()` 不调用它，除非显式传入尚未默认打开的参数。
- 验收：迁移两次不重复建表；迁移文件位于临时目录；用户 SQLite 字节不变。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_workspace_migrate.py tests/test_data_sources.py`
- 回滚：不调用迁移；删除测试库。

## PR-05 启动时登记本地工作区

- 内容：旗标 `--workspace-db` 打开时，把 `SourceRegistry` 的 id 和名称写入 `DataSource`，不写路径。旗标关闭时 `catalog()` 字节级保持今天的公开字段。
- 验收：`test_standalone_http_two_sources_without_bird` 在旗标关闭时通过。旗标打开的新测试断言 API 仍无 `path`。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_data_sources.py tests/test_workspace_catalog.py`
- 回滚：去掉旗标，忽略已生成的 `workspace.sqlite`。

## PR-06 并行写入 Conversation

- 内容：旗标打开时，`POST /api/start` 在现有目录写入成功后追加 Conversation、Message、AgentRun。读取路径仍是现有 `/api/run`。失败顺序与今天的 `persistence_error` 一致：目录是事实来源。
- 验收：现有生命周期测试在旗标关闭时全绿。新测试检查一次自定义问答产生一条用户消息；BIRD 运行不把参考 SQL 写入 Message。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_task_lifecycle.py tests/test_workspace_runs.py`
- 回滚：关闭旗标。多余的表可以留下，因为服务不读它们。

## PR-07 SQLite Adapter 委托

- 内容：`SQLiteAdapter` 的方法直接调用现有 `SQLiteTools`，`ObservedTools` 仍包在外层。图继续接收现在的工具对象。
- 验收：Adapter 与直接调用 `SQLiteTools` 对同一条 `SELECT` 和 `DELETE` 返回相同状态；库字节不变。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_sqlite_adapter.py tests/test_macsql_live_ui.py tests/test_macsql_retrieval.py`
- 回滚：服务继续实例化 `SQLiteTools`。

## PR-08 检查点默认关闭

- 内容：图编译函数接受可选 checkpointer，默认 `None`。打开时使用运行目录内的 SQLite，`thread_id` 为运行 ID。启动不调用恢复。
- 验收：默认路径不创建检查点文件，`tests/test_macsql_reference_baseline.py` 通过。打开路径的脚本模型测试能读回该线程，进程重启测试的模型调用次数不增加。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_macsql_reference_baseline.py tests/test_checkpointer_optional.py tests/test_task_lifecycle.py`
- 回滚：始终传 `None`。

## PR-09 记忆摘录，默认空

- 内容：实现 `context-memory-design.md` 的纯函数。默认返回空摘录。测试用含 `GOLD_SENTINEL`、样本和参考 SQL 的假历史，断言输出里没有这些字符串。
- 验收：旗标关闭时 Decomposer 收到的问题文本与现在相同。本 PR 不改提示词常量。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_context_excerpt.py tests/test_macsql_reference_baseline.py`
- 回滚：不把函数接到 `_select()`。

## PR-10 用户结果脱敏

- 内容：用户页读取的 DTO 去掉 schema 样本、完整 messages 和评分样例。`/observatory` 使用的 `read_run()` 保持现有字段，以免破坏 `test_recorded_ui.py`。
- 验收：用户 DTO 测试找不到样本标记；观察台测试仍能看到 trace 事件名。`test_user_ui.cjs` 更新预期时只改夹具数据源，不放宽文本转义断言。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_macsql_live_ui.py src/sql_agent_lab/macsql/test_recorded_ui.py` 以及 `node src/sql_agent_lab/macsql/test_user_ui.cjs`
- 回滚：用户页改回读完整 `/api/run`。

## PR-11 策略观察模式

- 内容：在执行前调用策略函数，默认只记录。强制模式由第二个旗标打开，默认关。白名单用现有夹具里的合法 `SELECT` 拟合，缺函数时记录而不是在观察模式中失败。
- 验收：观察模式不改变 `execute_sql_readonly` 的返回。强制模式的新测试拒绝 `DELETE`、`ATTACH` 和多语句，并放行夹具中的只读查询。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_sql_policy.py tests/test_macsql_sql_diagnostics.py tests/test_macsql_reference_baseline.py`
- 回滚：跳过策略调用。

## PR-12 本地令牌

- 内容：配置了令牌才检查请求头。未配置时行为与今天相同，并在启动日志写明“本机 API 没有令牌”。日志不打印令牌。拒绝绑定非回环地址。
- 验收：未配置时现有 HTTP 测试通过。配置后的新测试覆盖缺失、错误和正确令牌；错误体不包含令牌。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_macsql_live_ui.py tests/test_local_auth.py`
- 回滚：不读取令牌配置。

## PR-13 FastAPI 兼容层

- 内容：新进程用 FastAPI 复刻现有五个 API 的状态码和 JSON 字段，内部调用 `Observatory`。旧 `python -m sql_agent_lab.macsql.live_ui` 保留到兼容测试稳定后再决定是否委托。
- 验收：把 `test_task_lifecycle.py` 和 `test_data_sources.py` 的客户端指向兼容层时断言不变。不新增依赖到默认运行，除非锁文件同时更新且离线测试仍不访问网络。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_task_lifecycle.py tests/test_data_sources.py tests/test_macsql_live_ui.py`
- 回滚：继续使用标准库服务器。

## PR-14 SSE

- 内容：`GET /api/runs/{id}/events` 推送 `frontend-design.md` 的 snapshot、stage、finished。不推 token。轮询接口保留。
- 验收：脚本客户端收到的终态与 GET 红acted 结果一致；慢模型用脚本暂停，停止后不再产生新的 stage。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_run_sse.py tests/test_task_lifecycle.py`
- 回滚：移除路由。前端继续轮询。

## PR-15 对话 API 与 `/workspace` 页面

当前产品分支已提供 `/workspace/` 和对话 API。`--workspace-db` 启用对话，页面轮询任务状态，停止操作沿用 `POST /api/stop`。

- 内容：实现 `frontend-design.md` 的对话接口和页面。`/`、`/observatory`、`/analysis` 保持原文件。新页面用 `textContent`。语义复核控件不出现。
- 验收：无模型夹具完成提交、刷新、停止和跨对话隔离；旧 Node 测试不改预期。
- 命令：上述 Node 测试，加上 `.\.venv\Scripts\python.exe -m pytest tests/test_conversations_api.py tests/test_data_sources.py`
- 回滚：关闭页面旗标。对话表可以保留。

## PR-16 MCP 只读工具与默认 Skill

- 内容：一个本地 MCP 入口，工具名等于 `ObservedTools.names`，`db_id` 由服务端注入。Registry 只包含代码内的 `execution_repair_only`，`semantic_review` 为 false。不加载外部技能文件。
- 验收：脚本调用不能通过额外参数改变数据库路径；`DELETE` 被现有工具拒绝；默认 Skill 与 `Observatory.start()` 缺省开关一致。
- 命令：`.\.venv\Scripts\python.exe -m pytest tests/test_mcp_tools.py tests/test_macsql_live_ui.py`
- 回滚：不启动 MCP 进程。网页路径不受影响。

## PR-17 可靠性套件入口

- 内容：把 `evaluation-plan.md` 的可靠性命令收成一个 pytest 标记或脚本，不改变测试逻辑。
- 验收：标记收集到生命周期、来源、只读和新增隔离测试；帮助文本写明它不衡量 SQL 准确率。
- 命令：`.\.venv\Scripts\python.exe -m pytest -m reliability`
- 回滚：去掉标记，原文件路径仍可运行。

## 明确延后

以下工作需要新的书面边界后才能开 PR：

- PostgreSQL 或 MySQL 的真实连接和方言提示词。
- 把检查点自动恢复成一次模型调用。
- 把记忆摘录写进角色提示词正文。
- 多进程队列、公网监听、账号密码登录。
- 任何会改变 BIRD 分数定义或默认开启语义复核的改动。

每个延后项单独评审对现有离线测试的影响。在那之前，SQLite 只读三角色路径应与本审计时的行为一致。
