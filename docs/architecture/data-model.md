# 数据模型

这些类型是目标工作区的应用元数据。它们现在不存在于代码中。落地时先做不接入运行时的数据类和迁移，现有 `manifest.json`、`task.json`、`trace.jsonl`、`result.json` 继续作为一次运行的工件。

参考 SQL 只允许出现在评测工件和 `EvaluationSession` 中，不进入 User、Conversation、Message 或模型可见字段。连接秘密只保存引用，不保存秘密值。

## 标识与时间

主键使用 UUID 字符串。时间使用带时区的 ISO-8601 UTC，与 `task_lifecycle.utc_now()` 一致。外部稳定 ID，例如数据源 `id` 和 BIRD `question_id`，保留原有格式，不替换成 UUID。

## Workspace

一个工作区是授权和数据源的边界。当前进程相当于一个隐式的本地工作区。

| 字段 | 含义 |
| --- | --- |
| `id` | UUID |
| `name` | 显示名 |
| `created_at` | 创建时间 |
| `local_only` | 第一版恒为 true，表示不对外网提供服务 |

## User

| 字段 | 含义 |
| --- | --- |
| `id` | UUID |
| `display_name` | 显示名 |
| `created_at` | 创建时间 |

身份凭证不放在这张表。本地第一版使用一个进程级令牌，令牌值只在被忽略的配置文件中。以后如果有多用户登录，表中只保存 `credential_ref`。

## Membership

| 字段 | 含义 |
| --- | --- |
| `workspace_id` | Workspace |
| `user_id` | User |
| `role` | `owner`、`analyst`、`developer` |

`analyst` 可以提问和查看自己的对话。`developer` 额外可以打开观察台中的原始 trace 和实验语义复核。`owner` 管理数据源注册。

## DataSource

| 字段 | 含义 |
| --- | --- |
| `id` | 沿用现有 `[A-Za-z0-9_-]{1,100}` |
| `workspace_id` | 所属工作区 |
| `name` | 页面名称 |
| `kind` | `sqlite`、`postgres`、`mysql`。后两者在 Adapter 落地前不能被运行时选中 |
| `read_only` | 第一版恒为 true |
| `status` | `ready` 或 `unavailable` |
| `secret_ref` | 指向仓库外配置项的名字，例如 `sources:shop` |
| `schema_fingerprint` | 最近一次成功读取的 schema 哈希，可空 |
| `created_at` | 注册时间 |

SQLite 的文件路径留在现有 `sources.json`，由 `SourceRegistry` 解析。应用库不复制路径，API 继续只返回 `id`、`name`、`kind`、`read_only`。网络数据库的主机、端口、库名和用户名也放在秘密引用指向的文件中，不进入目录接口。

`DataSourceGrant` 把用户连到数据源：`user_id`、`datasource_id`、`can_query`。表级允许列表是以后的字段，第一版用整库只读授权，因为当前工具也是整库只读。

BIRD 数据源仍由 `BirdBenchmark` 注册，并带校验和。与用户数据源 ID 冲突时继续失败，对应 `test_duplicate_between_bird_and_user_sources_is_rejected`。

## Conversation

| 字段 | 含义 |
| --- | --- |
| `id` | UUID |
| `workspace_id` | Workspace |
| `user_id` | 创建者 |
| `datasource_id` | 这个对话绑定的数据源 |
| `title` | 可由首个问题截断生成 |
| `created_at`、`updated_at` | 时间 |

一个对话固定一个数据源。换数据源即新对话，避免把 A 库的 SQL 历史送进 B 库的提示词。当前“最近查询”是运行列表，不是 Conversation。

## Message

| 字段 | 含义 |
| --- | --- |
| `id` | UUID |
| `conversation_id` | Conversation |
| `seq` | 从 1 递增 |
| `role` | `user`、`assistant`、`tool_summary` |
| `body` | 用户问题，或给用户看的结果说明 |
| `created_at` | 时间 |

`assistant.body` 使用今天 `basicAnswer()` 已经能从执行反馈得出的事实：状态、有界预览形状、错误类别。它不保存完整模型回复。完整回复留在该次 AgentRun 的 trace 里。`tool_summary` 只记录工具名、状态和截断标记，不记录无限结果。

## AgentRun

一次图执行。它对应今天的一个运行目录。

| 字段 | 含义 |
| --- | --- |
| `id` | 第一版继续使用现有运行目录名，保证旧测试和页面能寻址；新代码可另存 UUID |
| `conversation_id` | 可空。旗标关闭时为空，行为与今天的单次问答相同 |
| `user_message_id` | 触发这次运行的用户消息，可空 |
| `datasource_id` | 问题使用的数据源 |
| `state` | `running`、`stopping`、`completed`、`failed`、`stopped`、`interrupted`、`unknown` |
| `stage` | 现有 `event_stage()` 的阶段名 |
| `started_at`、`updated_at`、`ended_at`、`stop_requested_at` | 与 `task.json` 相同 |
| `error` | 任务错误，不是答案错误 |
| `result_status` | `completed`、`needs_review`、`failed`、`evaluation_failed`、`stopped` 或空 |
| `score` | 仅评测运行有 0、1 或空。空表示未评测 |
| `semantic_review` | 实际使用的开关，默认 false |
| `artifact_dir` | 现有运行目录的服务器端路径，API 不返回 |

状态转移继续调用 `change_task()`。应用表是投影：写文件成功后再写表；表写入失败时沿用今天的 `persistence_error` 行为，不把任务假装成已持久化。

`interrupted` 仍是读时投影。启动扫描发现 `running` 或 `stopping` 且没有工作线程时，API 显示中断，不改历史文件，也不自动建新运行。

## AuditEvent

| 字段 | 含义 |
| --- | --- |
| `id` | UUID |
| `at` | 时间 |
| `actor_user_id` | 可空，表示进程启动等系统动作 |
| `workspace_id` | 可空 |
| `action` | 见下表 |
| `object_type`、`object_id` | 作用对象 |
| `outcome` | `ok` 或 `denied` |
| `detail` | 短 JSON。禁止秘密、完整 SQL 结果、参考 SQL 和提示词全文 |

建议的 `action`：`auth.check`、`source.register`、`conversation.create`、`run.start`、`run.stop`、`sql.compile`、`sql.execute`、`policy.deny`、`run.finish`。

trace 里的 `tool_started` 仍然是开发者观察记录。AuditEvent 是给权限和安全排查的更短记录。两者不要互相替代。

## 评测工件

保持现有文件：

- `evaluation-evidence.json` 含 `model_input: false`、库哈希、参考 SQL 哈希、比较摘要和最多三行截断样例。
- 它不进入 Message，也不进入模型上下文。
- 只有 developer 角色的运行详情可以读取它。用户结果视图只看到 `score` 空、0 或 1。

## 逻辑关系

```text
Workspace 1---* Membership *---1 User
Workspace 1---* DataSource 1---* DataSourceGrant *---1 User
User 1---* Conversation *---1 DataSource
Conversation 1---* Message
Conversation 1---* AgentRun *---1 DataSource
AgentRun 1---* AuditEvent
AgentRun 1---1 运行目录（manifest/task/trace/result）
```

## 从现有文件投影

只读投影，供迁移核对，不替代原文件：

| 现有字段 | 新字段 |
| --- | --- |
| `manifest.question` | 一条 `user` Message，加上 AgentRun 的 `datasource_id` |
| `manifest.semantic_review` | `AgentRun.semantic_review` |
| `task.json` | `AgentRun.state` 与时间字段 |
| `result.status`、`result.score` | `result_status`、`score` |
| 目录名 | `AgentRun.id` 与 `artifact_dir` |

自定义问题的 `question_id == "custom"` 只说明它不是 BIRD 题，不能当作 Conversation ID。

## 存储

应用元数据使用输出目录下单独的 SQLite，例如 `workspace.sqlite`。该文件由应用拥有，可以用迁移创建表。用户的业务库仍然只读，应用不在其中建表。

迁移版本写入 `schema_migrations`。每一步可向前应用、可整库删除回滚，因为旧运行目录仍是事实来源。在并行写入被证明与 `tests/test_task_lifecycle.py` 一致之前，不把应用库切换成唯一事实来源。
