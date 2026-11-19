# SQL Agent Studio

The workspace page is served locally at `/workspace/`. The layout follows the static prototype. Query results come from the Conversation API allowlist, and the page does not use built-in demo numbers.

Example start command (keep data files outside the repository):

```powershell
.\.venv\Scripts\python.exe -m sql_agent_lab.macsql.live_ui --sources D:\data\sources.json --workspace-db D:\data\workspace.sqlite --output-dir D:\data\runs --port 8765
```

Open http://127.0.0.1:8765/workspace/ .

Without `--workspace-db`, the page explains that conversations are unavailable. `/` and `/observatory` stay available. When there is no reference answer, the page shows “Not evaluated” and does not treat a successful execution as an accuracy score.

Check:

```powershell
node prototypes/workspace-ui/test_workspace_ui.cjs
```
