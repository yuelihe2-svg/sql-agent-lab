# SQL Agent Studio

Codex 风格工作区页面，由本地服务在 `/workspace/` 提供。布局沿用静态原型。查询结果来自 Conversation API 的白名单结果视图，不使用页面内的演示数字。

启动示例（数据文件放在仓库外）：

```powershell
.\.venv\Scripts\python.exe -m sql_agent_lab.macsql.live_ui --sources D:\data\sources.json --workspace-db D:\data\workspace.sqlite --output-dir D:\data\runs --port 8765
```

打开 http://127.0.0.1:8765/workspace/ 。

未加 `--workspace-db` 时页面会说明会话不可用，原有 `/` 与 `/observatory` 不受影响。没有参考答案时页面显示“未评测”，不把执行成功写成准确率。

检查脚本：

```powershell
node prototypes/workspace-ui/test_workspace_ui.cjs
```
