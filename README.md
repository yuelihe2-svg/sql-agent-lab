# SQL Agent Lab

SQL Agent Lab 是一个三角色、只读 SQLite 问答应用。LangGraph 组织 Selector（筛选表与字段）、Decomposer（分解问题并生成 SQL）、Refiner（根据执行与类型检查反馈修复 SQL），中文网页展示角色输出、工具调用、运行记录和独立评测结果。

三个角色按条件运行：完整 schema 能放进上下文时跳过 Selector；没有执行或类型错误时不调用执行修复 Refiner。语义复核是默认关闭的实验选项。SQL 可以执行不代表答案正确；BIRD 评分通过单独执行参考 SQL 比较完整结果集合，忽略顺序和重复行。

## 安装与离线检查

协作者先克隆仓库并进入项目目录，再创建自己的 Python 环境；不要复制他人的 `.venv` 或本机配置。前端离线检查还需要安装 Node.js。数据库和模型不随仓库提供；可以先运行下面的离线测试或无模型页面预览，再配置自己的 SQLite 数据源。

在项目根目录使用 Python 3.13：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m pytest
node src/sql_agent_lab/macsql/test_review_ui.cjs
node src/sql_agent_lab/macsql/test_task_ui.cjs
node src/sql_agent_lab/macsql/test_task_races.cjs
node src/sql_agent_lab/macsql/test_source_ui.cjs
node src/sql_agent_lab/macsql/test_user_ui.cjs
```

测试使用临时 SQLite 数据库、保存的模型回复和模拟传输。测试网络防护仅允许连接测试自行创建的本地 HTTP 服务，不允许访问云端 API 或本地模型服务。回归测试通过不代表真实模型准确率提升。

## 网页

本机已配置路径时，在项目根目录运行 `./start-web.ps1`。该脚本读取被 Git 忽略的 `.local/web-config.json`；其他机器可复制 `web-config.example.json` 后填写自己的目录。

```powershell
.\.venv\Scripts\python.exe -m sql_agent_lab.macsql.live_ui --data-dir <BIRD目录> --metadata <字段说明JSON> --output-dir <仓库外的新运行目录> --port 8780
```

打开 `http://127.0.0.1:8780`。可通过 `--analysis-dir <分析目录>` 读取历史分析表，通过重复指定 `--recorded-batch <批次目录>` 读取历史模型输出与 trace。读取记录不会重新调用模型或评测。点击运行按钮会调用已配置的本地 Ollama；网页启动本身不调用模型。DeepSeek 的命令行运行器位于 `sql_agent_lab.macsql.run_deepseek`，只有主动运行该命令才会使用 API。

数据、数据库、模型权重、API Key 和实验记录保存在仓库外。`.local` 用于本机迁移证明与配置，已被 Git 忽略。API Key 通过环境变量提供，不写进源码。

首页 `/` 是用户数据问答页：选择数据源、输入问题、查看结果。默认简洁视图展示执行状态和结果表格，SQL 可折叠查看；右上角切换详细视图后展开 SQL、调用次数、Token 和阶段事件。切换视图或查看历史只读取已有结果，不发起推理或重新执行 SQL。视图偏好保存在当前浏览器，查询内容仍由后端保存；侧栏显示最近 20 条普通问答。结果只展示工具返回的预览，更多行会标注，空结果不会伪装成失败。

原工作流观察台保留在 `/observatory`，可查看角色原始输出、工具调用、BIRD 评测与历史实验。用户页的“开发者观察台”链接可携带当前运行 ID；`/analysis` 仍用于逐题分析。用户页沿用相同的 `POST /api/start`、`POST /api/stop` 和任务查询接口，固定开启原有 schema 检索、关闭实验语义复核，不改变 Agent 工作流。详情中的正确性评测与任务结束状态分别显示，缺失 Token 不显示为零。

结果表格上方的“结果说明”由前端 `basicAnswer(run)` 根据已有执行反馈生成，不调用模型或重新执行 SQL。明确完整的一行一列结果展示字段名和值；0、NULL、空字符串和没有返回行分别说明。多行结果说明行列数；截断或完整性未知的结果只描述预览，不把预览行数当作总数，也不根据自然语言或 SQL 文本推断业务含义。原有类型检查待复核时保留提醒。执行错误优先读取 SQLite 工具的 `feedback.message`，普通页面展示可操作的中文说明，详细视图保留原始错误。SQL 和表格始终作为核对依据。

代码阅读顺序为 `refresh()` → `renderRun()` → `basicAnswer()` → `renderBasicAnswer()`：轮询拿到状态与结果，纯函数判断结果形状，再通过 `textContent` 显示文字。这一层只负责展示，不改变任务状态、Agent 流程或评测分数。`test_user_ui.cjs` 覆盖零值、空值、空结果、截断、失败、类型提醒及 HTML 文本安全等边界。

无需模型的五种结果示例：`.\.venv\Scripts\python.exe -B tests/browser_lifecycle_fixture.py --standalone --answer-cases --root <仓库外的全新目录>`。打开打印的地址，从查询历史选择 `answer-scalar`、`answer-zero`、`answer-empty`、`answer-null` 或 `answer-failure` 对应的中文示例。它们由临时 SQLite 执行固定只读 SQL 后保存，查看示例不调用 Agent。失败示例故意引用不存在的字段；真实 Agent 有修复能力，不能保证用自然语言提出坏字段就一定得到失败结果。

无模型页面预览：`.\.venv\Scripts\python.exe -B tests/browser_lifecycle_fixture.py --standalone --showcase --root <仓库外的全新目录>`。这个测试夹具使用合成商店数据和固定 SQL 回复，并阻断所有出站连接。它只能验证界面和执行链路，不能验证自然语言理解能力。普通应用中的“开始查询”仍会调用真实本地模型。

### 一次问题怎样传到本地模型

1. `user_ui.html` 的 `startQuery()` 读取数据源 ID 和问题，通过 `fetch` 发送 `POST /api/start`。浏览器不提交数据库路径或参考 SQL。
2. `live_ui.py` 的 `Handler.do_POST()` 解析 JSON，交给 `Observatory.start()` 验证数据源与问题，构造 `Question('custom', db_id, question)`，保存任务并启动后台线程，立即返回运行 ID。
3. 后台 `work()` 管理任务生命周期，`_work()` 按 ID 找到 SQLite 文件，组装只读工具、上下文配置及 `LocalOllamaModel`，调用 `ReferenceMacSQLAgent.run(question)`。
4. Agent 读取 schema，按既有条件执行角色节点；`reference_baseline.py` 组织角色提示词，`run_local.py` 的适配器向本机 `127.0.0.1:11434/api/chat` 发送请求。网页当前固定使用已下载的 `qwen3:4b-instruct`、16,384 上下文、8 个 CPU 线程、禁用 GPU offload 和 thinking；适配器检查本地 GGUF，拒绝远程模型，不下载模型或回退到云端。
5. 模型返回文本，Agent 提取 SQL，由 Python 的 SQLite 工具检查和执行；模型不持有数据库连接。连接使用 `mode=ro`、`query_only` 和原有 SQL 限制。输出经过既有执行与类型检查，必要时才调用修复节点。
6. 后端写入事件与结果；浏览器的 `refresh()` 每秒查询 `/api/runs` 和 `/api/run?id=...`，`renderRun()`、`renderTable()` 更新状态与表格。模型适配器当前是非流式调用，完整回复到达后才记录本次 Token；页面轮询不等于逐 Token 流式输出。

真实接入使用正常的 `start-web.ps1` 或模块启动命令，不使用浏览器测试夹具。先确认 Ollama 已运行且已有指定模型；启动网页本身不推理，点击“开始查询”才开始。第一次加载模型和处理 schema 可能耗时。普通问答没有参考答案，不会触发 BIRD 评分；一次简单查询成功只能证明该次链路与结果，不能代表通用准确率。

## 独立 SQLite 接入与可选 BIRD 评测

数据库问答不要求 BIRD 文件。将 `sources.example.json` 复制到本机数据目录或被忽略的 `.local/` 中，填写已有 SQLite 文件路径：

```json
{"sources": [{"id": "shop", "name": "商店数据", "kind": "sqlite", "path": "D:/sql-agent-data/shop.sqlite"}]}
```

`id` 是稳定且唯一的标识，使用英文字母、数字、下划线或连字符；`name` 是页面名称，省略时使用 ID。相对路径以这份配置文件所在目录为基准。接入时只读检查文件和 schema，不复制、建表或修改数据库，也不调用模型。配置目前在启动时加载，修改后需重启自己启动的服务；暂不提供上传文件、在线编辑注册表或其他数据库类型。

```powershell
.\.venv\Scripts\python.exe -B -m sql_agent_lab.macsql.live_ui --sources D:/sql-agent-data/sources.json --output-dir D:/sql-agent-lab-runs/standalone --port 8781
```

这个命令用于启动服务，启动前请确认端口未被占用。打开网页后选择“数据库问答”和数据源，输入问题。点击运行仍会调用本地真实模型；离线验收请使用下面的测试夹具。没有参考答案时不启动评测器，`score` 为 `null`，页面显示“未评测（无参考答案）”。执行成功不能证明语义正确。结果表格是执行工具返回的预览，受原有行数限制。

需要题库时额外传 `--data-dir <BIRD目录>`，在 `/observatory` 选择“BIRD 题库评测”。也可只传旧的 `--data-dir`，原配置继续可用。BIRD 适配器注册自己的数据库、加载题目和参考答案，并保留原有注释文件及数据库哈希校验。独立数据源与 BIRD 的 ID 冲突会报错，不会静默覆盖。BIRD 数据源也能接受普通问题，但普通问题不评分。字段说明仍可用 `--metadata` 传入，按数据库 ID 匹配；没有字段说明也可读取数据库自身的表、列和外键。

`start-web.ps1` 的配置新增可选 `sources`（数据源配置文件路径），`data_dir` 变为可选。现有 `.local/web-config.json` 不会被自动改写。

| 接口 | 输入或输出 | 责任 |
| --- | --- | --- |
| `GET /api/catalog` | `sources: [{id, name, kind, read_only}]`、`benchmark_available`、`questions` | 前端展示数据源与可用模式，不返回路径或参考 SQL；保留旧 `databases` ID 数组 |
| `POST /api/start` 普通问答 | `{"db_id":"shop","question":"有多少条记录？"}` | 只允许已注册 ID，构造普通问题，不执行参考 SQL |
| `POST /api/start` BIRD 评测 | `{"question_id":"1"}` | 从服务端题库取问题与参考答案；未启用 BIRD 时拒绝 |
| `GET /api/runs`、`GET /api/run?id=...` | 原有任务状态、事件、SQL、结果预览 | 继续复用任务生命周期管理 |

两类启动请求继续支持原有 `retrieval`、`semantic_review` 选项。为兼容旧页面，以是否提供 `question_id` 区分模式；提供题号时由服务端确定问题内容和数据库。若同时提供 `db_id`，它必须与题目所属数据库一致。参考 SQL 不进入 `Question` 或模型上下文。

阅读顺序：`data_sources.py` 管理 SQLite 绑定，`bird_benchmark.py` 管理可选评测输入，`live_ui.py` 在应用边界组装二者，`live_ui.html` 的 `configureSources()` 根据目录接口更新选择框。Agent 编排、角色行为、只读执行工具和评分算法没有改变。这一步仍是单机应用接入，不代表已经支持任意数据库或生产环境的权限与并发隔离。

无模型浏览器验收：运行 `.\.venv\Scripts\python.exe -B tests/browser_lifecycle_fixture.py --standalone --root <仓库外的全新临时目录>`，打开打印出的随机端口。该模式不创建 BIRD 文件，注册两份合成 SQLite，选择不同数据源会分别返回 42 和 99；Agent 已替换且所有出站连接被阻断。创建该目录中的 `shutdown` 文件可退出测试服务。`tests/test_data_sources.py` 检查同一 HTTP/执行链路、失败边界和 BIRD 兼容性；`test_source_ui.cjs` 检查模式切换。

## 任务生命周期：前后端如何连接

当前网页服务是单进程、单活动任务的本地应用；同一个输出目录应只由一个服务写入，没有跨进程任务队列或自动续跑。

一次运行保存四类文件：`manifest.json` 是启动配置，`task.json` 是应用任务状态，`trace.jsonl` 是逐步事件，`result.json` 是 SQL 执行与评测结果。基准题还会保存独立评测证据。任务完成不代表答案正确，例如 `task.state = completed` 可以同时对应 `result.score = 0`；`needs_review` 也属于已结束的任务，具体检查问题仍保留在结果中。

| 任务状态 | 页面含义 |
| --- | --- |
| `running` | 后台线程正在处理 |
| `stopping` | 已请求停止，等待当前操作返回 |
| `completed` | 任务正常结束；检查与评分另看结果 |
| `failed` | 执行、评测或保存过程失败，查看结束原因 |
| `stopped` | 已在协作停止检查点结束 |
| `interrupted` | 新版本地任务遗留未完成状态，当前服务没有对应工作线程 |
| `unknown` | 旧记录缺少可确定的任务状态 |

按以下顺序阅读代码，就能追踪一次网页操作：

1. `live_ui.html` 的运行按钮通过 `fetch` 发送 `POST /api/start`。
2. `live_ui.py` 的 `Handler.do_POST()` 把请求交给 `Observatory.start()`，它保存初始任务状态、启动后台线程并返回运行 ID。HTTP 请求不需要等模型完成。
3. 后台 `work()` 管理任务的开始与结束，`_work()` 保留原有模型、SQL 和评测执行过程。`_observe_stage()` 只观察既有事件，更新页面阶段，不改变 LangGraph 路由。
4. 网页每秒调用 `GET /api/runs` 和 `GET /api/run?id=...`。两个接口都返回 `task`，包含 `state`、`stage`、`started_at`、`updated_at`、`ended_at`、`stop_requested_at` 和 `error`。前端 `renderTask()` 把状态翻译成中文并更新按钮。
5. 停止按钮发送 `POST /api/stop`，请求体为 `{"id":"运行 ID"}`。`request_stop()` 先核对 ID，再记录停止请求并设置线程事件。重复停止不会重置请求时间，旧 ID 不会停止新任务。当前模型请求无法立即取消；如果任务已越过最后停止检查点，也可能正常完成。

`task_lifecycle.py` 中的 `change_task()` 限制状态变化，防止已完成的任务重新变成运行中。`Lock` 协调 HTTP 线程与后台线程；保存时先写临时文件再替换正式文件，避免接口读到半份 JSON。这个线程锁只在一个服务进程内有效。

服务重启后，读取新版本地遗留任务会返回 `interrupted`，不修改旧文件、不自动调用模型，也不伪造准确结束时间。如果最终结果已保存、任务状态还没来得及保存，则根据结果恢复展示状态。旧记录缺少 `task.json` 时只做兼容展示。状态文件保存失败时，当前服务会展示内存中的状态及保存错误；这不等于数据已经成功持久化。

离线学习入口为 `tests/test_task_lifecycle.py`：用临时 SQLite 数据库和模拟 Agent，通过真实的本地 HTTP 接口验证运行、停止和查询。`test_task_ui.cjs` 检查真实页面函数的中文状态、按钮、计时和轮询行为。二者都不连接真实模型。

`test_task_races.cjs` 专门控制请求返回顺序，检查旧轮询不能覆盖已接受的停止操作。前端用操作版本号丢弃过期响应；状态文件保存成功时也会清除之前的保存失败提示。

手动浏览器验收可运行 `.\.venv\Scripts\python.exe -B tests/browser_lifecycle_fixture.py --root <仓库外的全新临时目录>`，打开它打印的随机端口地址。这个测试服务器使用模拟 Agent 和合成数据库，并阻断所有出站连接，不使用本机研究数据或 8780 服务。题目 2/3/4 分别模拟正常完成、答案错误和异常；题目 5 等待目录中的 `release` 文件，方便观察“运行中”和“停止中”。创建 `shutdown` 文件会让该测试服务器自行退出；移走该标记后，用同一目录重新启动可检查中断恢复。测试夹具不作为应用运行入口。

## 代码入口

- `src/sql_agent_lab/macsql/reference_core.py`：LangGraph、模型接口、状态与调用记账。
- `reference_baseline.py`：三个角色的提示词与工作流行为。
- `tools.py`：schema 检索、取值查询、SQL 编译与只读执行。
- `live_ui.py`、`live_ui.html`、`analysis.html`：本地服务与中文网页。
- `task_lifecycle.py`：应用任务状态与只读恢复展示；独立于 Agent 工作流状态。
- `tests/`、模块内 `test_*.py` 和 `fixtures/`：离线回归测试。

运行不依赖 AgentOpt。`make_agent_class(config)` 保留 `__init__(models)` / `run(input_data)` 接口，便于后续通过适配器接入模型组合搜索。当前项目不包含 AgentOpt 优化器与实时 Pareto 界面。

本实现参考 MAC-SQL 的角色设计，不是作者仓库的逐字复刻，也不宣称复现论文分数。来源与许可说明见 `NOTICE.md`。

## 协作开发

`main` 保存已验证的版本。每次开发从最新的 `main` 新建功能分支，小步提交，通过 Pull Request 说明问题、改动和离线验证结果，再审查合并。工程改造与 Agent 行为修改分开提交，便于追踪结果变化。

每位开发者使用自己的 `.local/` 配置和仓库外运行目录。不要提交密钥、真实数据库、模型权重或运行日志；固定的 `fixtures/` 是离线测试输入，使用范围见 `NOTICE.md`。运行普通测试不需要模型；真实模型验证和 BIRD 批量评测应单独约定。
