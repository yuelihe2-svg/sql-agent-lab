# Local project boundaries

- Work only in this independent project unless the user explicitly requests otherwise. Do not modify AgentOpt or agentopt-research source directories or branches.
- Keep code and records local. Do not configure remotes, push, or publish without an explicit request.
- Do not invoke model APIs, Ollama, HPC jobs, or benchmark reruns unless explicitly requested. Offline tests are allowed.
- Preserve the three-role workflow behavior when doing packaging or UI work. Keep semantic review disabled by default.
- Keep datasets, databases, credentials and run artifacts outside source control. Never print secrets.
- Public documentation should explain current behavior and usage. Keep migration history and validation artifacts in .local/.
