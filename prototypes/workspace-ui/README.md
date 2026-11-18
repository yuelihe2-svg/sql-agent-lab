# SQL Agent Studio 原型

独立静态页面，演示多轮数据分析工作区。不替换 `user_ui.html` 或 `live_ui.html`，不调用 API 或模型。

## 设计

对象是要核对 SQL 的分析员，所以页面是一段左对齐的对话，右侧是这一问的 SQL 和三个角色的轨迹。净收款那个衬线数字是页面里唯一的展示字形，其余保持石色底和墨色字。青绿只用在当前数据源、当前对话、发送按钮和这条下划线。

表、SQL 和数字都标成演示数据。空值显示为 `NULL`，没有 token 记录时显示“未记录”。

## 预览

在仓库根目录：

```powershell
py -3 -m http.server 8765 --bind 127.0.0.1 --directory prototypes/workspace-ui
```

打开 http://127.0.0.1:8765/ 。默认对话“10 月净收款”里依次是库简介、10 月净收款和按城市拆分。

检查脚本：

```powershell
node prototypes/workspace-ui/test_workspace_ui.cjs
```

## 交互

- 切换数据源会换一组对话，已有对话的数据源不会被改写。
- 新建会话进入空对话。查询进行中时，切换和新建会停住并说明要先停止。
- 右侧详情可以收起。执行详情用页面里的折叠控件关闭。
- 推荐问题如果已经在当前对话里，会滚到那一条，而不是再追加一遍。
- 问题文本用 `textContent` 写入。带标签的字符串只作为文字出现。

刷新后，地址栏里的内置对话可以恢复。新建会话只留在当前页面内存里。

## 以后接 Conversation API

原型不发请求。接入时按现有工作区模型读取，并沿用 `frontend-design.md` 里的旗标接口。

| 原型动作 | 接口 | 请求 |
| --- | --- | --- |
| 数据源列表 | `GET /api/catalog` | 只用 `id`、`name`、`kind`、`read_only` |
| 对话列表 | `GET /api/conversations` | 可按 `datasource_id` 过滤 |
| 新建会话 | `POST /api/conversations` | `{datasource_id, title}` |
| 打开对话 | `GET /api/conversations/{id}` | 消息加每次运行的脱敏结果 |
| 发送问题 | `POST /api/conversations/{id}/messages` | `{question}` |
| 停止 | `POST /api/runs/{id}/stop` | 仅 `running` 时可点 |
| 进度 | `GET /api/runs/{id}/events` | SSE 的 `snapshot`、`stage`、`finished`；没有 token 流 |

页面还需要运行结果里的这些展示字段，且不能带路径、参考 SQL 或 schema 样本：

```json
{
  "summary": "由服务端按现有结果说明规则生成",
  "state": "running | completed | failed | stopped",
  "stage": "selector | decomposer | refiner | execute",
  "sql": "SELECT …",
  "columns": [{"key": "amount", "label": "净收款额", "type": "money"}],
  "rows": [],
  "has_more": false,
  "truncated": false,
  "duration_ms": 4800,
  "tokens": {"input": null, "output": null},
  "trace": [{"role": "Selector", "state": "已完成", "detail": "不含提示词和样本"}]
}
```

`tokens` 或 `duration_ms` 缺失时继续显示“未记录”。语义复核开关不出现在这个页面。
