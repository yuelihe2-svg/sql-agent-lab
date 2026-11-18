# 评测计划

系统里已经有两类证据，只是还没有分成两个入口。Agent 质量回答“SQL 结果对不对”。系统可靠性回答“任务、安全边界和页面是否按状态机工作”。二者的失败不能互相代替：任务 `completed` 可以同时 `score = 0`，可靠性测试通过也不表示模型更准确。

本轮不跑 Benchmark，不调用模型。下面的命令都是离线命令；质量数字只来自以后单独授权的运行。

## Agent Quality Eval

### 协议

保持 `dataset.score_sql()` 和 `EvaluationSession`：

- 比较完整结果集合。
- 忽略顺序和重复行。
- 数字按有理数等价，文本按原值，包括前导零。
- 预测 SQL 执行失败且属于候选错误时得 0。
- 参考 SQL 失败、数据库变化、超时、行数或字节上限，得分是空，状态是评测错误，不是 0。
- 预览不能用来打分。
- 参考 SQL、完整行和样例行不进入模型输入和 workflow trace。trace 里只有哈希、分数、原因和耗时。

实现证据：`evaluation_audit.py`、`test_trace_fixes.py`、`test_role_comparison.py` 的 `GOLD_SENTINEL` 断言、`test_data_sources.py` 中无参考答案时 `score is None` 且不写 `evaluation-evidence.json`。

语义复核、输出类型检查和 `result_risks` 是诊断。它们可以让结果变成 `needs_review` 或保留原 SQL，但不是 BIRD 分数。质量报告若列出这些状态，必须单独成列，不能折进准确率。

### 离线门槛

每次改动评分器以外的执行路径时，至少跑：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_trace_fixes.py tests/test_role_comparison.py tests/test_data_sources.py tests/test_macsql_reference_baseline.py src/sql_agent_lab/macsql/test_execution_repair_only.py
```

改动三角色提示词不在本路线的早期 PR 中。一旦有授权的提示词实验，使用已有的复现和角色比较入口，并把协议哈希写入该次实验记录。不要把复现模式的上游提示词测试混进产品默认路径。

### 以后的有模型评测

需要明确授权才运行。报告至少包含：

- 题目集合与过滤条件。
- 模型名、是否模拟。
- 每题 `score` 空/0/1 的数量。准确率的分母只含 0 和 1，空分母单独列出。
- 调用次数、token、耗时。
- `semantic_review` 是否关闭。
- 与任务失败数分开。

没有新的授权运行时，文档只引用已有实验记录，不新增准确率数字。

## System Reliability Eval

可靠性评测使用临时 SQLite、脚本模型和测试进程内的 HTTP。它断言状态、文件、拒绝和隔离，不断言自然语言能力。

### 已有覆盖

| 主题 | 测试 |
| --- | --- |
| 完成、错答、类型复核、失败、停止、中断、写盘失败 | `tests/test_task_lifecycle.py` |
| 写操作被拒绝、跨源 POST、错误 Host | `tests/test_macsql_live_ui.py` |
| 双数据源、无 BIRD、路径不泄露、校验和不匹配 | `tests/test_data_sources.py` |
| 只读历史与记录 ID 不能逃出目录 | `src/sql_agent_lab/macsql/test_recorded_ui.py` |
| 页面状态、轮询竞争、用户页文本安全 | `test_task_ui.cjs`、`test_task_races.cjs`、`test_user_ui.cjs` |
| 离线网络隔离 | `conftest.py` |
| 本地模型适配器拒绝远程主机 | `tests/test_macsql_local.py` |
| 付费账本不记录秘密 | `src/sql_agent_lab/macsql/test_deepseek_api.py` |

### 建议的新套件

新测试放在 `tests/reliability/`，只在对应 PR 加入，避免一次改完全部。

| 用例 | 断言 |
| --- | --- |
| 多语句执行 | `execute_sql_readonly` 对 `SELECT 1; DELETE` 返回错误，文件字节不变 |
| 用户 DTO | 响应不含 `observed_samples`、参考 SQL 和 ledger 式秘密；观察台夹具仍能读 trace |
| 对话隔离 | 用户 A 的 `conversation_id` 不能被用户 B 读取或继续提问 |
| 数据源错配 | 对话绑定库 A 时提交库 B 的运行被拒绝，且没有运行目录 |
| 检查点默认关闭 | 跑完脚本模型后不产生检查点文件 |
| 中断不续跑 | 旗标打开时，重启只把运行标成 `interrupted`，脚本模型调用次数不增加 |
| SSE 回放 | 终态事件与一次 GET 的红acted 结果一致 |

可靠性通过的定义是这些断言全绿，并且现有 `pytest` 与 Node 页面测试全绿。它不产生准确率。

### 命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_lifecycle.py tests/test_macsql_live_ui.py tests/test_data_sources.py src/sql_agent_lab/macsql/test_recorded_ui.py tests/test_macsql_local.py
node src/sql_agent_lab/macsql/test_user_ui.cjs
node src/sql_agent_lab/macsql/test_task_ui.cjs
node src/sql_agent_lab/macsql/test_task_races.cjs
node src/sql_agent_lab/macsql/test_source_ui.cjs
```

全量回归仍是：

```powershell
.\.venv\Scripts\python.exe -m pytest
node src/sql_agent_lab/macsql/test_review_ui.cjs
node src/sql_agent_lab/macsql/test_task_ui.cjs
node src/sql_agent_lab/macsql/test_task_races.cjs
node src/sql_agent_lab/macsql/test_source_ui.cjs
node src/sql_agent_lab/macsql/test_user_ui.cjs
```

## 报告格式

每次架构 PR 在说明里写两行，而不是一个笼统的“测试通过”：

```text
Agent quality: 未跑有模型评测。离线评分/协议测试：<命令> <结果>
Reliability: <命令> <结果>
```

若只跑了可靠性，必须写明质量数字没有更新。若评分测试失败，不能用页面测试通过来合并。

## 不纳入这两类的内容

- 语义复核的真实模型效果。它默认关闭，实验记录留在 `docs/experiments/`。
- 上游 MAC-SQL 逐字复现是否达到论文分数。复现入口独立，不作为 Workspace 的质量门。
- 性能基准。在有并发设计之前，只记录单任务超时和现有限额。
