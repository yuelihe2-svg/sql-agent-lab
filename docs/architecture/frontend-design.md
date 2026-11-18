# 前端设计

当前界面是三个服务端直接返回的 HTML 文件，没有前端构建步骤。目标是在保留这些页面的前提下，增加可切换的多轮工作区视图。本轮不改 HTML。

## 当前页面

| 路径 | 文件 | 职责 |
| --- | --- | --- |
| `/` | `user_ui.html` | 选择数据源、提交一个问题、看结果说明和预览、最近 20 条自定义问答 |
| `/observatory` | `live_ui.html` | 角色输出、工具、trace、BIRD 模式、语义复核开关 |
| `/analysis` | `analysis.html` | 只读查看已有分析导出 |

用户页的数据流是 `startQuery()` → `POST /api/start` → 每秒 `refresh()` → `renderRun()` → `basicAnswer()` → `renderBasicAnswer()`。`basicAnswer()` 只根据已保存的执行反馈组织中文说明，不请求模型，也不重新执行 SQL。文字通过 `textContent` 进入 DOM。视图偏好存在 `localStorage` 键 `sql-agent-lab:view`。问题正文存在服务端运行目录。

简洁视图展示状态和表格，SQL 可折叠。详细视图展示调用次数、token、阶段和错误原文。侧栏“开发者观察台”链接可带当前运行 ID。用户页固定 `retrieval:true`、`semantic_review:false`。

轮询用 `epoch` 丢掉过期响应，`test_task_races.cjs` 覆盖停止与旧响应的竞争。`test_user_ui.cjs` 覆盖 0、NULL、空结果、截断、失败、类型提醒和 HTML 文本。

模型适配器是非流式的。页面上的连续更新是任务阶段变化，不是逐 token 输出。

## 目标信息架构

新增视图叫数据工作区，第一版可以放在 `/workspace`，用旗标关闭时返回 404，`/` 保持今天的文件。

工作区布局：

- 左栏：当前用户可见的 Conversation 列表，按数据源分组。
- 主栏：一个对话的消息。用户消息是问题，助手消息是结果说明、状态和表格。
- 输入区：绑定本对话的数据源。数据源在创建对话时选定，之后只读显示。
- 右栏或折叠区：SQL、阶段、耗时。详细 trace 仍跳到 `/observatory?run=`。

视觉上对齐常见对话产品：一条消息一列、输入框固定在底部、运行中显示阶段而不是空白等待。它仍然展示 SQL 和表格，因为本应用的核对对象是查询结果，不是一段无法追溯的散文。

观察台保持开发者工具的密度，不并进普通对话里。分析页保持只读。

## 客户端状态

```text
session        身份是否已建立；第一版可以仍是本机进程
catalog        /api/catalog 的 sources 与 benchmark_available
conversation   id、datasource、messages
activeRun      沿用 task.state：running、stopping、终态
view           simple 或 detail，继续可放 localStorage
```

消息列表以服务端为准。本地只乐观显示“正在提交”，提交失败则移除乐观消息并显示错误。不在浏览器保存数据库行。

切换对话只发读取请求。切换视图不发运行请求。这个规则与今天的历史查看一致。

## API 在前端的用法

现有接口继续支撑观察台和旧用户页：

- `GET /api/catalog`
- `GET /api/runs`
- `GET /api/run?id=`
- `POST /api/start`
- `POST /api/stop`

工作区旗标打开后增加：

| 接口 | 用途 |
| --- | --- |
| `GET /api/conversations` | 当前用户的对话摘要 |
| `POST /api/conversations` | `{datasource_id, title}` |
| `GET /api/conversations/{id}` | 消息和每次运行的红acted 结果 |
| `POST /api/conversations/{id}/messages` | `{question}`，服务端创建 AgentRun |
| `POST /api/runs/{id}/stop` | 与现有 stop 相同的状态机 |
| `GET /api/runs/{id}/events` | SSE |

旧 `POST /api/start` 在旗标关闭时行为不变。旗标打开且请求带 `conversation_id` 时，服务端校验对话、用户和数据源一致，再走同一条 `Observatory` 执行路径。

## SSE

事件是已有轮询对象的增量，不是新的 Agent 协议。

```text
event: snapshot
data: {"run_id":"...","task":{...},"result":null}

event: stage
data: {"run_id":"...","stage":"decomposer"}

event: finished
data: {"run_id":"...","task":{...},"result":{...红acted...}}
```

约束：

- 不发送 `call_started.messages`、schema 样本和评分样例。
- 在模型适配器仍是 `"stream": false` 时，不发送 `token` 事件。
- 断线后客户端用一次 `GET` 对齐，再决定是否重连。
- SSE 不可用时，工作区使用与今天相同的一秒轮询。
- 测试用脚本响应断言事件顺序，不打开浏览器模型。

## 结果展示

助手消息沿用 `basicAnswer()` 的规则：

- 完整的一行一列显示字段名和值，包括 0、空字符串和 NULL。
- 没有行就说明没有匹配记录。
- 多行说明行列数。
- `has_more` 或 `truncated` 时只称为预览，不把预览行数写成总数。
- 不根据 SQL 或自然语言补业务结论。
- 类型 mismatch 保留待核对提醒。
- 执行错误在普通视图用可操作的中文，详细视图保留原始 `feedback.message`。

表格继续由脚本创建单元格并用 `textContent` 填值。不使用 `innerHTML` 插入问题、SQL 或单元格。

空目录、无数据源、提交失败、停止中、中断、未评测，各自有文案。未评测显示“未评测”，不用 0 代替。token 缺失显示“未记录”，不用 0 代替。这些是现有测试已经锁定的行为。

## 可访问性与窄屏

当前用户页已有 `viewport`、按钮名和 760 像素以下的单列布局。工作区沿用同一断点：窄屏把对话列表收成顶部横向条，输入框保持在主栏底部。阶段状态使用文字，不只用颜色。

停止按钮只在 `task.state == running` 时可用。重复停止沿用后端的幂等结果。

## 验证

旧页面：

```powershell
node src/sql_agent_lab/macsql/test_user_ui.cjs
node src/sql_agent_lab/macsql/test_task_ui.cjs
node src/sql_agent_lab/macsql/test_task_races.cjs
node src/sql_agent_lab/macsql/test_source_ui.cjs
node src/sql_agent_lab/macsql/test_review_ui.cjs
```

新页面在有实现之后，用同样的无模型夹具 `tests/browser_lifecycle_fixture.py` 打开 `/workspace`，完成：选择数据源、提交、看到终态、刷新后历史仍在、停止竞争、恶意字符串只作为文本出现。没有浏览器自动化时，用脚本加载页面函数并报告未覆盖的视觉项。

前端 PR 不改 `basicAnswer()` 的判定规则，除非同时扩展 `test_user_ui.cjs` 并且产品说明确认。
