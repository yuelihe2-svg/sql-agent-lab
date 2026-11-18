# 上下文、记忆与 Schema RAG

当前一次问答的上下文由 `ContextPolicy` 和 `ReferenceMacSQLAgent._fit()` 完成。跨题记忆、LangGraph 检查点和独立 RAG 服务都还没有。本设计把三者加在图的外面，避免改角色提示词。

## 现在已经发生的事

`ContextPolicy` 用 UTF-8 字节上界估计输入，并保留输出位和安全边距。网页运行使用 16,384 的窗口和 14,000 的输入预算。

`_fit()` 对同一份提示词尝试四级 schema 文本：

1. 列类型、最多 120 字说明、观察到的样本。
2. 去掉样本。
3. 去掉说明。
4. 非语义复核角色再去掉类型。

任何一级都必须保留表和列标识符。放不下就失败，而不是删掉问题或标识符。语义复核保留类型。对应测试包括 `test_larger_output_still_respects_context_limit` 和 `test_review_fit_does_not_trade_types_for_budget`。

Selector 只在“完整 schema 的 Decomposer 提示”超过输入预算时调用。检索提示来自 FTS5，最多 1,600 字，并且声明自己不是完整 schema。

图状态 `WorkflowState` 只在一次 `invoke` 中存在。Refiner 能看到本题的旧 SQL 和 SQLite feedback。上一题的 SQL 不会进入下一题。

## Context Builder

Context Builder 是调用图之前的纯函数。它产出今天 `_decompose()` 已经接受的材料：问题、evidence、schema 字典、检索提示。默认模式产出与今天相同的材料，不读取历史。

输入：

- 当前用户问题。
- 数据源 schema。
- 可选的本对话摘录。
- `ContextPolicy`。

输出：

- `history_excerpt`：默认空字符串。
- `retrieval_hints`：继续由现有 `retrieval_hints()` 生成。
- 预算报告：使用了哪一级压缩、估计单位、是否包含历史。

历史摘录一旦打开，也必须经过同一个字节预算。预算不够时先丢掉最早的轮次，再丢掉摘录中的 SQL，最后才允许空摘录。不允许为了历史去删除当前问题或 schema 标识符。

摘录的一条轮次只包含：

- 用户问题的前 200 字。
- 已采纳 SQL 的前 500 字。
- 结果形状：行数是否被截断、列数、执行状态。

摘录不包含模型原始回复、schema 样本、评分样例、参考 SQL、trace 或其他对话。摘录生成函数要有测试证明这些字符串不会出现。

在旗标关闭的测试里，Builder 的输出与直接调用现有 `_select()` 时使用的问题文本相同。

## Conversation Memory

Memory 是 Conversation 上已完成 AgentRun 的有序列表，存在应用库，不存在模型供应商处。

写入时机是运行到达终态之后。`running` 的部分输出不进入下一轮，避免读到半成品 SQL。失败轮次只保留“这一问题失败”和错误类别，不保留失败 SQL，减少把坏语句反复送回模型的机会。

读取范围是同一 `conversation_id`、同一 `datasource_id`、同一用户。换库必须为空。

记忆不是检查点。它不能用来恢复一张跑到一半的图。

## LangGraph Checkpointer

目标配置：

```text
compile(checkpointer=None)          # 默认，等于今天
compile(checkpointer=run_local_db)  # 显式打开
```

打开时的规则：

- `thread_id` 等于 `AgentRun.id`，不复用 Conversation ID。同一对话的两轮是两次图执行。
- 检查点可以包含 `WorkflowState` 里的 SQL 和 schema，因此检查点文件与 trace 一样视为敏感工件，放在运行目录，不进 Git，API 默认不返回。
- 不在启动时调用 `invoke` 续跑。
- 若未来增加“显式恢复”，必须是新的 API，并重新做只读策略检查。恢复设计不在早期 PR 中。

测试要求：默认配置下，现有 `ReferenceMacSQLAgent.run()` 测试不需要检查点表。打开配置的新测试只验证写入和读取线程，使用脚本模型，不调用 Ollama。

## Schema RAG

第一版检索器就是 `SQLiteTools.search_schema()` 和 `lookup_values()`：

- 索引内容是标识符、说明、文档示例和有界观测样本。
- 索引建在内存临时库，不写回用户数据库。
- 排序是 FTS5 BM25。
- 词法切分和 FTS 引号转义保持不变。
- 返回值仍标记 `samples_incomplete` 和 `sample_only`。
- Selector 或 Decomposer 若采用某个名字，后续仍由 `validate_schema_selection()` 和 SQLite 编译检查真实存在性。

以后可以加向量检索，但必须满足同一条约束：检索器只返回候选，schema 权威来自数据库目录。向量索引保存列标识和说明，不保存业务表的全量单元格。样本进入索引前继续使用现有 `cell_chars` 和 `sample_values` 上限。

RAG 不读取 gold SQL、评测证据或其他用户的对话。BIRD evidence 只有在该次运行本来就会把 evidence 放进 `Question` 时才可见；检索器不额外把参考查询插进文档。

## 和提示词的边界

Builder 可以在现有 hints 槽位放入检索提示。历史摘录如果启用，也放在这个槽位之前的独立段落，由新代码拼接。`_selector_prompt`、`_decomposer_prompt`、`_refiner_prompt` 的正文在记忆 PR 中不改。

若实验表明必须把历史写进角色说明，那是单独的 Agent 行为变更，需要自己的离线夹具和明确授权。它不属于本架构的第一阶段。

## 失败行为

| 情况 | 行为 |
| --- | --- |
| schema 超过预算 | 沿用 `ContextBudgetExceeded`，任务失败，不调用模型 |
| FTS5 不可用 | 沿用 `RuntimeError('FTS5 schema search is unavailable')` |
| 摘录超过预算 | 缩短摘录；当前问题仍要放进预算 |
| 检查点写失败 | 旗标打开时任务失败并保留错误；旗标关闭时没有检查点可写 |
| 对话与数据源不一致 | 拒绝启动，不调用图 |
