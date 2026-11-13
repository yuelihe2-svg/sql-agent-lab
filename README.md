# SQL Agent Lab

SQL Agent Lab 是一个三角色、只读 SQLite 问答应用。LangGraph 组织 Selector（筛选表与字段）、Decomposer（分解问题并生成 SQL）、Refiner（根据执行与类型检查反馈修复 SQL），中文网页展示角色输出、工具调用、运行记录和独立评测结果。

三个角色按条件运行：完整 schema 能放进上下文时跳过 Selector；没有执行或类型错误时不调用执行修复 Refiner。语义复核是默认关闭的实验选项。SQL 可以执行不代表答案正确；BIRD 评分通过单独执行参考 SQL 比较完整结果集合，忽略顺序和重复行。

## 安装与离线检查

在项目根目录使用 Python 3.13：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m pytest
node src/sql_agent_lab/macsql/test_review_ui.cjs
```

测试使用临时 SQLite 数据库、保存的模型回复和模拟传输。测试网络防护仅允许连接测试自行创建的本地 HTTP 服务，不允许访问云端 API 或本地模型服务。回归测试通过不代表真实模型准确率提升。

## 网页

本机已配置路径时，在项目根目录运行 `./start-web.ps1`。该脚本读取被 Git 忽略的 `.local/web-config.json`；其他机器可复制 `web-config.example.json` 后填写自己的目录。

```powershell
.\.venv\Scripts\python.exe -m sql_agent_lab.macsql.live_ui --data-dir <BIRD目录> --metadata <字段说明JSON> --output-dir <仓库外的新运行目录> --port 8780
```

打开 `http://127.0.0.1:8780`。可通过 `--analysis-dir <分析目录>` 读取历史分析表，通过重复指定 `--recorded-batch <批次目录>` 读取历史模型输出与 trace。读取记录不会重新调用模型或评测。点击运行按钮会调用已配置的本地 Ollama；网页启动本身不调用模型。DeepSeek 的命令行运行器位于 `sql_agent_lab.macsql.run_deepseek`，只有主动运行该命令才会使用 API。

数据、数据库、模型权重、API Key 和实验记录保存在仓库外。`.local` 用于本机迁移证明与配置，已被 Git 忽略。API Key 通过环境变量提供，不写进源码。

## 代码入口

- `src/sql_agent_lab/macsql/reference_core.py`：LangGraph、模型接口、状态与调用记账。
- `reference_baseline.py`：三个角色的提示词与工作流行为。
- `tools.py`：schema 检索、取值查询、SQL 编译与只读执行。
- `live_ui.py`、`live_ui.html`、`analysis.html`：本地服务与中文网页。
- `tests/`、模块内 `test_*.py` 和 `fixtures/`：离线回归测试。

运行不依赖 AgentOpt。`make_agent_class(config)` 保留 `__init__(models)` / `run(input_data)` 接口，便于后续通过适配器接入模型组合搜索。当前项目不包含 AgentOpt 优化器与实时 Pareto 界面。

本实现参考 MAC-SQL 的角色设计，不是作者仓库的逐字复刻，也不宣称复现论文分数。来源与许可说明见 `NOTICE.md`。
