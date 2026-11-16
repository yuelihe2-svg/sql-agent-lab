# MAC-SQL 原方法复现模式

本模式独立于网页使用的 `ReferenceMacSQLAgent`，用于研究对照。目标是尽量对齐官方 BIRD 实现，不是宣称复现了论文准确率；这次只做无模型验收。

## 固定哪个版本

- 论文：[MAC-SQL，COLING 2025](https://aclanthology.org/2025.coling-main.36/)。
- 官方代码：[wbbeyourself/MAC-SQL，31a9df5](https://github.com/wbbeyourself/MAC-SQL/tree/31a9df5e0d520be4769be57a4b9022e5e34a14f4)。完整 revision 与五个核心文件的 SHA-256 固定在 `macsql_upstream.py`。
- 冲突时以该代码版本的 BIRD 默认路径为实现目标，并记录与论文表述的区别。例如论文描述按 schema 长度决定筛选，固定代码实际使用列数阈值。
- 官方核心目录没有明确的许可文件；`training_scripts/LICENSE` 不能直接视为整个仓库的许可。原始代码和完整提示词仅保存在忽略的本地目录中，不随本仓库分发。项目自身的 Apache-2.0 文件不对第三方内容重新授权。

## 本次对齐的行为

| 部分 | 当前应用模式 | 新复现模式 |
|---|---|---|
| Selector 触发 | 根据生成提示词的上下文预算 | 平均列数向下取整大于 6，或总列数大于 30 |
| Selector 提示词 | 本地适配提示词 | 固定版本的完整原版提示词 |
| Selector 输出 | 本地 JSON 协议与字段校验 | 读取第一个小写 `json` 围栏；解析无效回退完整 schema |
| `drop_all` | 本地筛选策略 | 保留该表前 6 列，未提及的表完整保留 |
| 列列表后处理 | 本地覆盖策略 | 保留主键、出向外键；原表超过 6 列时补足至 6 列；不按照模型列表重排原列 |
| schema 描述 | 本地字段说明、样本、检索提示 | BIRD `tables.json` 的名称、主外键与原版列表格式；取值按频率排序 |
| Decomposer | 本地缩短提示词和简单示例 | 原版两个完整 few-shot 示例；一次回复包含子问题与中间 SQL，取最后一个小写 `sql` 围栏 |
| 模型消息 | 本地 system + user | 原版单个 user 消息，不追加应用指令 |
| Refiner 触发 | 执行错误、类型检查等本地规则 | 执行错误、空结果、前 5 行任一列含 NULL |
| Refiner 循环 | 修复候选验收、失败回退等 | 原版至多 3 个执行/修复轮次；候选直接替换，不添加类型或语义验收 |
| 上下文 | 自动压缩、检索 | 不压缩原版提示词；超过显式上限直接停止 |

Decomposer 并没有变成“多次调用并执行每个子问题”。原版也是一次模型回复；中间 SQL 是推理示例和生成过程，只有最后的 SQL 交给 Refiner。

## 保留的原版细节

1. 原版 Selector 的自然语言指令要求丢弃无关表，但代码中的 `drop_all` 只压缩到前 6 列。复现按实际代码执行。
2. 三个轮次是“执行当前 SQL，必要时生成一个修复”。第三轮仍失败时会生成第三个修复，然后退出；这个最终候选尚未在工作流中执行。结果中的 `final_sql_checked=false`、`checked_sql` 和 `last_execution` 分开保存，不能把前一个 SQL 的反馈当成最终候选的反馈。后续独立评测仍应执行最终 SQL。
3. 回复没有小写 `sql` 代码块时，原版返回错误标记。Refiner 对包含小写 `error` 的 SQL 直接退出，甚至可能误伤字符串常量。复现保留并记录为 `upstream_error_guard`。
4. 原版修复提示词会给不是以 `SELECT` 或 `select` 开头的 SQL 直接添加 `SELECT`，包括 `WITH` 查询。此细节也保留用于对照。
5. 空结果和 NULL 可以是正确答案。原版规则会尝试修复它们；反过来，能运行且结果非空的语义错误通常不会触发修复。`execution_accepted` 只代表通过该启发式检查，不代表回答正确。

## 明确保留的工程差异

- 所有 SQL 通过现有 `SQLiteTools.connection(query=True)` 只读连接与 authorizer 执行。禁止写入、附加数据库、扩展和文件访问；不复制原版可写连接。
- 设置完整结果的行数、字节数、SQL 长度、单值大小和执行时间上限。超过资源上限停止，不把不完整结果当成空结果或成功。正常路径完整读取后只把前 5 行交给原版判据，不使用网页截断到 12 列的预览。
- 原版超时分支可能访问未赋值的 `error_info`。本模式明确标记 `execution_timeout` 并退出，不重现这个崩溃。
- 不复制启动时的模型连通性测试，也不自动重试 API。API/预算异常直接停止；不会像原版 Selector 的宽泛捕获一样吞掉网络失败继续付费。
- 每次最多输出 3200 tokens、提示词最多 100000 UTF-8 字节，超限不静默裁剪。它们是本地实验预算，不是论文参数；未来实验必须记录并检查截断。DeepSeek 仍使用非 thinking 模式。
- `ReproductionDeepSeekModel` 复用现有记账与异常处理，温度设为官方调用中的 0.1，不强制 Selector 为 JSON response format。三个角色仍有独立 trace；API ledger 角色名带 `macsql_` 前缀以区分应用运行。
- 使用 DeepSeek 是模型替换，不是原论文 GPT-4/GPT-3.5 的同配置复现。独立评分继续使用本项目完整结果集合比较，忽略行顺序和重复行、保留列顺序；不宣称等同于官方评分脚本。
- 仅接受 `Question`，没有 gold schema 或参考 SQL 输入接口。原版固定 few-shot 示例与当前题目的参考答案是不同来源；正式评测前仍须审查样本与 few-shot 的重叠，并公开排除规则。

## 代码阅读顺序

1. `src/sql_agent_lab/experiments/macsql_upstream.py`：固定版本、校验哈希，用 AST 读取字符串常量，不导入上游 Python 模块。
2. `macsql_schema.py`：从一条 BIRD `tables.json` 记录准备 schema；读取数据库中的频率排序取值，构造 Selector 和 Decomposer 看到的文本。
3. `macsql_reproduction.py` 中的 `OriginalMacSQLAgent`：独立 LangGraph `selector → decomposer → refiner_round`。`_refiner_round()` 同时管理一次执行和一次可能的模型修复；`rounds`、`repairs`、`checked_sql` 是不同状态。
4. `ReproductionResult`：把最终 SQL、最后执行过的 SQL、执行反馈和停止原因分别交给调用方。网页尚未接入此模式，现有服务无须重启。
5. `tests/test_macsql_reproduction.py`：合成数据、固定回复、模拟 HTTP，验证边界和实际循环。`test_macsql_upstream_parity.py` 对比已核对的上游纯函数；没有本地源码时跳过，绝不自动下载。

## 无模型使用方式

先显式下载固定的公开源码到忽略目录。这只是 HTTP 文件下载，不访问模型：

```powershell
.\.venv\Scripts\python.exe -B -m sql_agent_lab.experiments.macsql_upstream fetch --directory .local/macsql-upstream/31a9df5e0d520be4769be57a4b9022e5e34a14f4
```

已下载后可把 `fetch` 改成 `verify`，全程离线检查哈希。`load_prompts()` 本身不会联网。

回放需要：匹配 SQLite 文件的 BIRD `tables.json`、按调用顺序保存的字符串数组 `responses.json`，以及新的仓库外目录。这里的回复为预先保存或人工编写的模拟输出，不进行自然语言推理：

```powershell
.\.venv\Scripts\python.exe -B -m sql_agent_lab.experiments.macsql_reproduction --upstream-dir .local/macsql-upstream/31a9df5e0d520be4769be57a4b9022e5e34a14f4 --tables-json <tables.json> --database <database.sqlite> --db-id <db_id> --question "问题" --responses <responses.json> --output-dir <仓库外的新目录>
```

CLI 只有离线回放，没有付费运行子命令。测试也受仓库的网络防护约束。API 适配器仅通过模拟传输验证，不需要读取环境中的密钥。

2026-11-16 验收：完整离线套件 385 项通过，其中新增复现测试 45 项；本机已安装固定源码，4 项上游纯函数/提示词校验实际执行，没有跳过。另以原版提示词、合成 SQLite 和三条人工回复完成 CLI 回放：Selector 选择 schema、Decomposer 生成不存在字段、Refiner 修复成功。回放启用网络阻断，所有模型回复标记 `simulated=true`，产物保存在仓库外。

## 真实实验前还缺什么

本机已有的 `upstream-schema.json` 是按数据库组织的 DDL 字符串，不是官方 `tables.json`。新增代码不把它偷偷当成同一份元数据。需要补齐与现有 SQLite 匹配的 BIRD 表名、列名描述和主外键记录，固定来源与哈希，完成 schema 准备检查。频率采样比原应用的少量样本扫描更昂贵，也需提前记录耗时和资源限制。

之后再建立新的实验配置，冻结题目、样本排除规则、提示词、模型参数、预算、评分器与源码哈希。完整路径至多调用 Selector 1 次、Decomposer 1 次、Refiner 3 次，API ledger 的调用上限应至少容纳这 5 次；所有条件使用相同输入与预算策略。不要直接沿用上一轮 A/B/C 运行器的 3 次调用上限，也不要把新结果追加到旧报告。

本次没有重新跑 50 题、没有调用真实模型、没有修改共享数据、没有修改网页 Agent 或 main。离线通过只说明控制流和协议对齐，不说明准确率提高。
