'use strict';
/* SQL Agent Studio client. Text is assigned with textContent. No mock query results. */
(function (root) {
  function readPref(key) {
    try { return root.localStorage?.getItem('sql-studio-' + key); } catch { return null; }
  }
  function writePref(key, value) {
    try { root.localStorage?.setItem('sql-studio-' + key, value); } catch { /* local storage unavailable */ }
  }
  const locale = readPref('language') === 'en' ? 'en' : 'zh-CN';
  const english = {
    "正在准备查询": "Preparing query",
    "正在读取数据库结构": "Reading database schema",
    "正在筛选相关字段": "Selecting relevant fields",
    "正在生成 SQL": "Generating SQL",
    "正在检查并执行 SQL": "Checking and running SQL",
    "正在修复 SQL": "Repairing SQL",
    "正在分析返回要求": "Checking result requirements",
    "正在复核查询": "Reviewing query",
    "正在独立评测": "Evaluating independently",
    "正在整理结果": "Preparing result",
    "未记录": "Unavailable",
    " 秒": " s",
    "正在处理查询": "Working on query",
    "已完成": "Completed",
    "查询失败": "Query failed",
    "正在处理": "Running",
    "正在停止": "Stopping",
    "已停止": "Stopped",
    "已中断": "Interrupted",
    "状态未记录": "Status unavailable",
    "已收到停止请求，正在等待当前操作返回。": "Stop requested. Waiting for the current operation to finish.",
    "查询已停止。可以修改问题后再次发起。": "Query stopped. Edit your question and try again.",
    "服务曾中断，本次查询未自动重跑。": "The service was interrupted. Run the query again when ready.",
    "本次查询未完成。": "The query did not finish. Review the question and try again.",
    "此记录没有可确认的执行结果。": "No confirmed result was saved for this run.",
    "没有可展示的查询结果。": "No query results to display.",
    "查询结果。下列是全部返回行。": "Query result. All returned rows are shown.",
    "结果预览。行数不是总数，也不能当作完整结果。": "Result preview. The displayed rows are a sample.",
    "查询结果-": "query-result-",
    "结果预览-": "result-preview-",
    "你的问题": "Your question",
    "回答": "Answer",
    "全部返回行": "All returned rows",
    "结果预览": "Result preview",
    "无结果表": "No result table",
    "查询结果表格": "Query results table",
    "当前预览行数可能与总行数不同。": "The preview may show fewer rows than the full result.",
    "下载结果 CSV": "Download CSV",
    "下载预览 CSV": "Download preview CSV",
    "显示这条 SQL": "Show SQL",
    "停止": "Stop",
    "收起详情": "Close details",
    "展开 SQL 详情": "SQL details",
    "未命名对话": "Untitled conversation",
    "新对话": "New conversation",
    "返回对话": "Back to conversation",
    "数据库介绍": "Database overview",
    "数据库介绍 · SQL Agent Studio": "Database overview · SQL Agent Studio",
    "正在读取数据库介绍。": "Loading database overview…",
    "正在发送…": "Sending…",
    "查询运行中": "Query running",
    "发送": "Send",
    "其他页面中有查询正在运行。": "A query is running on another page.",
    "点击左侧「添加数据库」，选择 SQLite 文件后即可开始提问。": "Choose Add database and select a SQLite file to get started.",
    "工作区还不能保存会话。": "The workspace cannot save conversations yet.",
    "开始对话": "Ask your database",
    "直接输入问题，发送后自动保存为新对话。": "Send a question to start a new conversation.",
    "这段对话还没有问题。": "No questions yet.",
    "展开或收起 ": "Expand or collapse ",
    "在 ": "New conversation in ",
    " 中新建对话": "",
    "选择一条回答以查看对应问题。": "Select an answer to see its question.",
    "本次问题暂无 SQL。": "No SQL for this question.",
    " · 已评测": " · Evaluated",
    " · 未评测": " · Not evaluated",
    "尚未选择查询": "No query selected",
    "尚未评估答案正确性。": "Answer correctness has not been evaluated.",
    "已启用数据库依据。检索策略为 ": "Database context enabled. Search method: ",
    "。命中的表为 ": ". Matched tables: ",
    "无": "None",
    "。知识文档为 ": ". Knowledge documents: ",
    "。这不是 SQL 正确性的证明。": ". These matches do not verify SQL correctness.",
    "未使用数据库依据。": "Database context was not used.",
    "使用数据库依据": "Use database context",
    "未启用工作区，不能使用数据库依据。": "Database context requires an enabled workspace.",
    "关键词": "Keyword",
    "混合检索": "Hybrid",
    "未启用工作区。请带 --workspace-db 重新启动后刷新。": "Enable the workspace and reload this page.",
    "无法读取这个数据源的介绍。": "Could not load the overview for this data source.",
    "无法读取数据库介绍。": "Could not load the database overview.",
    "数据库摘要": "Database summary",
    " 张表。结构信息来自数据库目录，业务含义需由知识文档补充。": " tables. Schema comes from the catalog. Knowledge documents describe business meaning.",
    "指纹 ": "Fingerprint ",
    "抽取时间 ": "Extracted ",
    "表结构": "Schema",
    "这个数据库没有用户表。": "This database has no user tables.",
    "列": "Column",
    "类型": "Type",
    "主键": "Primary key",
    "未声明": "Not declared",
    "是": "Yes",
    "没有列。": "No columns.",
    "声明的主键：": "Declared primary key: ",
    "没有声明主键。": "No declared primary key.",
    "声明的外键：": "Declared foreign keys: ",
    "没有声明外键。": "No declared foreign keys.",
    "业务知识": "Business knowledge",
    "用户提供，未经核验。这些说明不是数据库已经核对过的事实。": "User-provided and unverified.",
    "还没有业务知识。写下来的内容只属于当前数据源。": "No knowledge yet. Notes belong to this data source.",
    "版本 ": "Version ",
    " · 来源：用户提供": " · Source: user",
    "用户提供／未经核实": "User-provided / unverified",
    "编辑": "Edit",
    "删除": "Delete",
    "编辑知识": "Edit knowledge",
    "添加知识": "Add knowledge",
    "知识标题": "Knowledge title",
    "知识格式": "Knowledge format",
    "纯文本": "Plain text",
    "知识正文": "Knowledge body",
    "保存修改": "Save changes",
    "填入指标口径模板": "Insert metric template",
    "检索预览": "Retrieval preview",
    "按当前数据源的目录和业务知识做匹配。不会执行 SQL，也不会把结果交给 Agent。": "Preview schema and knowledge matches for this data source. This does not run SQL.",
    "检索问题": "Search question",
    "预览检索策略": "Preview search method",
    "查看匹配": "Show matches",
    "（数值越小越相关，与结构得分分别展示）": " (lower is more relevant; separate from schema scores)",
    "关键词得分 ": "Keyword score ",
    "这次问题匹配到的内容": "Matches for this question",
    "检索策略 ": "Search method ",
    "没有匹配到表、字段或知识。": "No tables, fields or knowledge matched.",
    "表 ": "Table ",
    "数据库结构事实 · ": "Database catalog · ",
    "字段 ": "Fields ",
    "没有单独命中的字段": "No individual fields matched.",
    "用户提供的定义 · 版本 ": "User definition · Version ",
    "这段文字仍只是用户说明，不会当作系统指令。": "This excerpt is user-provided context.",
    "表和字段来自数据库目录。知识不能覆盖系统规则。": "Schema comes from the database catalog. Knowledge cannot override system rules.",
    "工作区": "Workspace",
    "SQL 智能问答工作区": "SQL Agent Studio",
    "本地只读工作区": "Local read-only workspace",
    "数据源": "Data sources",
    "新建会话": "New conversation",
    "对话": "Conversations",
    "开发工具": "Developer tools",
    "单次问答": "Single question",
    "观察台": "Observatory",
    "+ 添加数据库": "+ Add database",
    "添加数据库": "Add database",
    "添加并开始提问": "Add and start asking",
    "取消": "Cancel",
    "上传完整、未加密的 SQLite 文件，最大20 MB。数据库以只读方式使用。": "Choose a complete, unencrypted SQLite file, up to 20 MB. The database is read-only.",
    "数据库文件": "Database file",
    "名称": "Name",
    "业务说明（可选，例如金额单位、付款状态与统计口径）": "Business notes (optional, e.g. currency or payment status)",
    "请选择不超过20 MB的 SQLite 文件": "Choose a SQLite file no larger than 20 MB.",
    "正在校验并添加…": "Validating and adding…",
    "添加失败，请重试": "Could not add the database. Try again.",
    " 参考业务说明": " Use business knowledge",
    " 关联前文": " Use conversation context",
    "数据库依据检索策略": "Database context search method",
    "检索方式": "Search method",
    "参考这个数据库的结构和业务说明，帮助理解字段与统计口径。": "Use this database's schema and notes to interpret fields and metrics.",
    "追问时参考当前对话，例如“那杭州呢？”；新话题可新建会话。": "Use earlier turns for follow-ups. Start a new conversation to switch topics.",
    "查询设置": "Query settings",
    "用自然语言描述要查询的内容": "Ask a question about this database…",
    "输入问题": "Question",
    "Ctrl + Enter 发送": "Ctrl + Enter to send",
    "查询详情": "Query details",
    "复制 SQL": "Copy SQL",
    "已复制": "Copied",
    "请选中上方 SQL 手动复制": "Select the SQL above to copy it manually",
    "执行状态": "Execution status",
    "Token 和耗时": "Tokens and duration",
    "输入 token": "Input tokens",
    "输出 token": "Output tokens",
    "耗时": "Duration",
    "数据库依据": "Database context",
    "当前阶段": "Current stage",
    "请求未完成": "Request could not be completed",
    "连接中断，正在重试…": "Connection lost. Retrying…",
    "未启用工作区。请带 --workspace-db 重新启动后刷新。当前不会把演示数据当成查询结果。": "Enable the workspace and reload this page.",
    "无法连接本地服务。确认服务已启动后刷新此页。": "Could not reach the local service. Check that it is running, then reload.",
    "数据源不一致": "Data source does not match",
    "数据源与会话": "Sources and history",
    "关闭导航": "Close navigation",
    "界面语言": "Interface language",
    "主题": "Theme",
    "跟随系统": "System",
    "浅色": "Light",
    "深色": "Dark",
    "跳转到对话": "Skip to conversation",
    "需要补充条件": "Clarification needed",
    "暂无匹配记录": "No matching rows",
    "暂无可展示结果": "No result to display",
    "数据库操作：": "Database actions for ",
    "运行详情": "Run details",
    "查看统计口径": "Query basis",
    "需核对": "Needs review",
    "正在排队": "Queued",
    "等待前面的任务完成。": "Waiting for earlier queries to finish.",
    "取消排队": "Cancel queued query",
    "加入队列": "Queue query",
    "当前有查询在执行，新问题将排队。": "A query is running. New questions will be queued.",
    "本次处理超时，可以缩小查询范围后重试。": "Query timed out. Narrow the query and try again.",
    "查询超时": "Timed out",
    "管理对话": "Manage conversations",
    "删除选中": "Delete selected",
    "选择对话": "Select conversation",
    "删除选中的对话？对话将从工作区隐藏。": "Delete the selected conversations? They will be hidden from the workspace.",
    "退出登录": "Sign out",
    "你的账号暂时没有获授权的数据源，请联系维护者。": "Your account has no data sources. Contact the workspace operator."
  };
  function ui(text) { return locale === 'en' ? (english[text] ?? text) : text; }

  const count = new Intl.NumberFormat(locale);
  const cellNumber = new Intl.NumberFormat(locale, { maximumSignificantDigits: 21 });
  const when = new Intl.DateTimeFormat(locale, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const seconds = new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const stageNames = {
    preparing: ui('正在准备查询'),
    schema: ui('正在读取数据库结构'),
    selector: ui('正在筛选相关字段'),
    decomposer: ui('正在生成 SQL'),
    execute: ui('正在检查并执行 SQL'),
    refiner: ui('正在修复 SQL'),
    requirements: ui('正在分析返回要求'),
    semantic_review: ui('正在复核查询'),
    evaluation: ui('正在独立评测'),
    finalizing: ui('正在整理结果'),
    finished: ui('正在整理结果')
  };

  function formatWhen(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : when.format(date);
  }
  function formatElapsed(value) {
    if (value == null || !Number.isFinite(Number(value))) return ui('未记录');
    return seconds.format(Number(value)) + ui(' 秒');
  }
  function formatTokens(value) {
    if (value == null) return ui('未记录');
    return count.format(value);
  }
  function stageLabel(stage) {
    return stageNames[stage] || ui('正在处理查询');
  }
  function statusLabel(status) {
    return {
      queued: ui('正在排队'),
      completed: ui('已完成'),
      failed: ui('查询失败'),
      running: ui('正在处理'),
      stopping: ui('正在停止'),
      stopped: ui('已停止'),
      interrupted: ui('已中断')
    }[status] || ui('状态未记录');
  }
  function publicSources(catalog) {
    return (catalog && catalog.sources || []).filter(function (source) {
      return source && typeof source.id === 'string';
    }).map(function (source) {
      return {
        id: source.id,
        name: typeof source.name === 'string' ? source.name : source.id,
        kind: source.kind || '',
        read_only: source.read_only === true
      };
    });
  }
  function displaySummary(run) {
    const view = run && run.result || {};
    if (view.summary) return ui(view.summary);
    const state = run && run.state;
    if (state === 'queued') return ui('等待前面的任务完成。');
    if (state === 'running') return stageLabel(run.stage);
    if (state === 'stopping') return ui('已收到停止请求，正在等待当前操作返回。');
    if (state === 'stopped') return ui('查询已停止。可以修改问题后再次发起。');
    if (state === 'interrupted') return ui('服务曾中断，本次查询未自动重跑。');
    if (state === 'failed') return view.error || ui('本次查询未完成。');
    return ui('此记录没有可确认的执行结果。');
  }
  function pairTurns(detail) {
    const runs = {};
    (detail.runs || []).forEach(function (run) {
      if (run && run.user_message_id) runs[run.user_message_id] = run;
    });
    const items = [];
    (detail.messages || []).slice().sort(function (a, b) { return a.seq - b.seq; }).forEach(function (message) {
      items.push({ role: 'user', id: message.id, body: message.body, createdAt: message.created_at });
      const run = runs[message.id];
      if (!run) return;
      const view = run.result || {};
      items.push({
        role: 'assistant',
        id: run.id,
        body: displaySummary(run),
        clarification: view.needs_clarification === true,
        needsReview: view.execution_status === 'needs_review',
        timedOut: view.error === '本次处理超时，可以缩小查询范围后重试。',
        state: run.state,
        stage: run.stage || view.stage || null,
        sql: view.sql || null,
        columns: Array.isArray(view.columns) ? view.columns : [],
        rows: Array.isArray(view.rows) ? view.rows : [],
        preview: view.preview === true,
        hasMore: view.has_more === true,
        truncated: view.truncated === true,
        complete: view.complete === true,
        error: view.error || null,
        elapsed: view.elapsed_seconds,
        tokens: view.tokens || { input: null, output: null },
        evaluated: view.evaluated === true,
        grounding: view.grounding && view.grounding.enabled === true ? view.grounding : { enabled: false }
      });
    });
    return items;
  }
  function caption(turn) {
    if (!turn || turn.preview !== true) return ui('没有可展示的查询结果。');
    if (turn.complete === true) return ui('查询结果。下列是全部返回行。');
    return ui('结果预览。行数不是总数，也不能当作完整结果。');
  }
  function cellText(value) {
    if (value == null) return { text: 'NULL', nullish: true };
    if (typeof value === 'number' && Number.isFinite(value)) return { text: cellNumber.format(value), nullish: false };
    if (typeof value === 'boolean') return { text: value ? 'true' : 'false', nullish: false };
    return { text: String(value), nullish: false };
  }

  function resultCsv(columns, rows) {
    function field(value) {
      let text = value == null ? 'NULL' : String(value);
      if (typeof value === 'string' && (/^\s*[=+@-]/u.test(text) || /^[\t\r\n]/u.test(text))) {
        text = "'" + text;
      }
      return '"' + text.replace(/"/g, '""') + '"';
    }
    return '\uFEFF' + [columns, ...rows].map(row => row.map(field).join(',')).join('\r\n') + '\r\n';
  }

  function downloadResult(doc, item) {
    const blob = new Blob([resultCsv(item.columns, item.rows)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = el(doc, 'a');
    link.href = url;
    link.download = (item.complete ? ui('查询结果-') : ui('结果预览-')) + item.id + '.csv';
    link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function sourceById(sources, id) {
    return (sources || []).find(function (source) { return source.id === id; }) || null;
  }
  function talksFor(state) {
    return state.conversations.filter(function (item) { return item.datasource_id === state.datasourceId; });
  }

  function el(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function renderMessage(doc, item, handlers) {
    const article = el(doc, 'article', 'message' + (item.role === 'user' ? ' user' : ''));
    if (item.clarification) article.classList.add('clarification');
    article.dataset.messageId = item.id;
    if (item.role === 'user') {
      article.append(el(doc, 'p', 'prose', item.body));
      article.setAttribute('aria-label', ui('你的问题'));
      return article;
    }
    article.setAttribute('aria-label', ui('回答'));
    const meta = el(doc, 'div', 'meta');
    meta.append(el(doc, 'span', 'status-chip status-' + (item.clarification || item.needsReview ? 'clarification' : item.state || ''),
      item.clarification ? ui('需要补充条件') : item.needsReview ? ui('需核对') : item.timedOut ? ui('查询超时') : statusLabel(item.state)));
    if (item.preview) meta.append(el(doc, 'span', null, item.complete ? ui('全部返回行') : ui('结果预览')));
    if (item.elapsed != null) meta.append(el(doc, 'span', null, formatElapsed(item.elapsed)));
    article.append(meta);
    // The API returns the explanatory basis after a dedicated line delimiter.
    const [answer, ...basisLines] = (item.body || '').split('\n本次 SQL 使用的口径（请核对）：');
    const basis = basisLines.join('\n本次 SQL 使用的口径（请核对）：');
    const scalar = item.complete && item.columns.length === 1 && item.rows.length === 1;
    let shownAnswer = answer;
    if (locale === 'en' && basis && scalar && /^「[^」]+」的查询结果/.test(answer) && !item.needsReview) {
      const value = item.rows[0][0];
      shownAnswer = value == null ? 'The result is NULL. This does not mean zero.'
        : value === '' ? 'The result is an empty string.' : 'Result: ' + cellText(value).text + '.';
    }
    const summary = el(doc, 'p', 'prose answer-summary', shownAnswer);
    if (item.state === 'failed') {
      const wrap = el(doc, 'div', 'failure');
      wrap.append(summary);
      article.append(wrap);
    } else {
      article.append(summary);
    }
    if (item.preview && item.columns.length && item.rows.length) {
      const wrap = el(doc, 'div', 'table-wrap');
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', ui('查询结果表格'));
      const table = el(doc, 'table');
      table.append(el(doc, 'caption', 'visually-hidden', caption(item)));
      const headRow = el(doc, 'tr');
      item.columns.forEach(function (column, index) {
        const numeric = item.rows.every(row => row[index] == null || typeof row[index] === 'number')
          && item.rows.some(row => typeof row[index] === 'number');
        const th = el(doc, 'th', numeric ? 'numeric' : null, column);
        th.scope = 'col';
        headRow.append(th);
      });
      const thead = el(doc, 'thead');
      thead.append(headRow);
      const tbody = el(doc, 'tbody');
      item.rows.forEach(function (row) {
        const line = el(doc, 'tr');
        item.columns.forEach(function (column, index) {
          const cell = cellText(Array.isArray(row) ? row[index] : null);
          const value = Array.isArray(row) ? row[index] : null;
          line.append(el(doc, 'td', cell.nullish ? 'null' : typeof value === 'number' ? 'numeric' : null, cell.text));
        });
        tbody.append(line);
      });
      table.append(thead, tbody);
      wrap.append(table);
      article.append(wrap);
      if (!item.complete) article.append(el(doc, 'p', 'table-note', ui('当前预览行数可能与总行数不同。')));
    } else if (item.preview) {
      article.append(el(doc, 'p', 'table-note', caption(item)));
    }
    if (basis) {
      const disclosure = el(doc, 'details', 'query-basis');
      disclosure.append(el(doc, 'summary', null, ui('查看统计口径')), el(doc, 'p', 'prose', basis));
      article.append(disclosure);
    }
    if (item.preview && item.columns.length) {
      const download = el(doc, 'button', 'text-action', item.complete ? ui('下载结果 CSV') : ui('下载预览 CSV'));
      download.type = 'button';
      download.addEventListener('click', function () { downloadResult(doc, item); });
      article.append(download);
    }
    if (item.sql) {
      const show = el(doc, 'button', 'text-action', ui('显示这条 SQL'));
      show.type = 'button';
      show.addEventListener('click', function () { handlers.showSql(item.id); });
      article.append(show);
    }
    if (['queued', 'running'].includes(item.state)) {
      const stop = el(doc, 'button', 'text-action', item.state === 'queued' ? ui('取消排队') : ui('停止'));
      stop.type = 'button';
      stop.addEventListener('click', function () { handlers.stop(item.id); });
      article.append(stop);
    }
    return article;
  }

  function render(doc, state, nodes, handlers) {
    const source = sourceById(state.sources, state.datasourceId);
    const conversation = state.detail;
    const ownRun = conversation && (conversation.runs || []).some(function (run) {
      return run.id === state.activeRunId && ['running', 'stopping'].includes(run.state);
    });
    const navigationLocked = Boolean(state.navigating || state.sending);
    const sidebarKey = JSON.stringify([state.sources, state.conversations, state.datasourceId,
      conversation && conversation.conversation.id, navigationLocked, state.deleteMode]);
    doc.querySelector('.shell').classList.toggle('details-closed', state.view === 'overview' || !state.detailsOpen);
    nodes.details.hidden = !state.detailsOpen;
    nodes.toggle.textContent = state.detailsOpen ? ui('收起详情') : ui('展开 SQL 详情');
    nodes.toggle.setAttribute('aria-expanded', state.detailsOpen ? 'true' : 'false');
    nodes.title.textContent = conversation ? (conversation.conversation.title || ui('未命名对话')) : ui('新对话');
    nodes.subtitle.textContent = source && source.name !== nodes.title.textContent ? source.name : '';
    doc.title = nodes.title.textContent + ' · SQL Agent Studio';
    nodes.banner.hidden = !state.notice;
    nodes.banner.textContent = state.notice || '';

    nodes.newer.disabled = !state.workspace || !source || navigationLocked;
    nodes.addSource.hidden = state.team;
    nodes.foot.hidden = state.team;
    nodes.signOut.hidden = !state.team;
    nodes.signOut.textContent = state.accountName ? state.accountName + ' · ' + ui('退出登录') : ui('退出登录');
    nodes.manage.hidden = !state.conversations.length && !state.deleteMode;
    nodes.manage.textContent = state.deleteMode ? ui('取消') : ui('管理对话');
    nodes.manage.setAttribute('aria-pressed', String(state.deleteMode));
    nodes.deleteSelected.hidden = !state.deleteMode;
    nodes.deleteSelected.disabled = !state.deleteSelection.size || navigationLocked;
    nodes.deleteSelected.textContent = ui('删除选中') + ' (' + state.deleteSelection.size + ')';
    nodes.addSource.disabled = !state.workspace || navigationLocked || Boolean(state.activeRunId);
    nodes.overviewButton.hidden = state.view !== 'overview';
    nodes.overviewButton.disabled = !state.workspace || !source || navigationLocked;
    nodes.overviewButton.textContent = state.view === 'overview' ? ui('返回对话') : ui('数据库介绍');
    nodes.error.hidden = !state.error;
    nodes.error.textContent = state.error;
    nodes.question.readOnly = state.sending;
    if (nodes.sidebarKey !== sidebarKey) {
      nodes.sidebarKey = sidebarKey;
      renderSidebar(doc, state, nodes, handlers, navigationLocked);
    }

    nodes.form.hidden = state.view === 'overview';
    nodes.toggle.hidden = state.view === 'overview';
    nodes.details.hidden = state.view === 'overview' || !state.detailsOpen;
    if (state.view === 'overview') {
      nodes.title.textContent = ui('数据库介绍');
      nodes.subtitle.textContent = source ? source.name : '';
      doc.title = ui('数据库介绍 · SQL Agent Studio');
      nodes.threadKey = null;
      const draftFields = nodes.thread.querySelector('form[data-knowledge]');
      const previewFields = nodes.thread.querySelector('form[data-preview]');
      const overviewKey = JSON.stringify([state.datasourceId, state.overview, state.draft, state.grounding,
        state.overviewError]);
      if (nodes.overviewKey === overviewKey) return;
      nodes.overviewKey = overviewKey;
      const scrollTop = nodes.thread.scrollTop;
      nodes.thread.replaceChildren();
      if (!state.workspace) {
        nodes.thread.append(el(doc, 'div', 'empty-thread', state.notice || overviewError(404)));
      } else if (state.overviewError) {
        nodes.thread.append(el(doc, 'div', 'empty-thread', state.overviewError));
      } else if (!state.overview) {
        nodes.thread.append(el(doc, 'div', 'empty-thread', ui('正在读取数据库介绍。')));
      } else {
        nodes.thread.append(renderOverview(doc, {
          summary: overviewSummary(state.overview.profile),
          tables: schemaTables(state.overview.profile),
          knowledge: knowledgeCards(state.overview.knowledge, state.datasourceId), readOnly: state.team
        }, handlers));
        const preview = renderRetrievalForm(doc, handlers);
        if (previewFields && nodes.overviewSource === state.datasourceId) {
          preview.elements.question.value = previewFields.elements.question.value;
          preview.elements.strategy.value = previewFields.elements.strategy.value;
        }
        nodes.thread.append(preview);
        if (state.grounding && state.grounding.datasource_id === state.datasourceId) {
          nodes.thread.append(renderGrounding(doc, state.grounding));
        }
        const knowledge = renderKnowledgeForm(doc, state, handlers);
        if (draftFields && nodes.overviewSource === state.datasourceId && nodes.overviewDraft === state.draft
            && nodes.overviewRecord === state.overview) {
          for (const name of ['title', 'format', 'body']) knowledge.elements[name].value = draftFields.elements[name].value;
        }
        if (!state.team) nodes.thread.append(knowledge);
        nodes.overviewSource = state.datasourceId;
        nodes.overviewDraft = state.draft;
        nodes.overviewRecord = state.overview;
      }
      nodes.thread.scrollTop = scrollTop;
      if (nodes.live.textContent !== ui('数据库介绍')) nodes.live.textContent = ui('数据库介绍');
      return;
    }
    nodes.overviewKey = null;
    const turns = conversation ? pairTurns(conversation) : [];
    const threadKey = JSON.stringify([conversation && conversation.conversation.id, turns, state.workspace,
      state.notice, state.datasourceId]);
    if (threadKey !== nodes.threadKey) {
      renderThread(doc, state, nodes, handlers, turns, conversation);
      nodes.threadKey = threadKey;
    }
    const busy = Boolean(state.sending || state.activeRunId);
    nodes.send.disabled = state.sending || state.navigating || !state.workspace || !source || !nodes.question.value.trim();
    nodes.send.textContent = state.sending ? ui('正在发送…') : state.activeRunId ? ui('加入队列') : ui('发送');
    nodes.runHint.hidden = !state.activeRunId || ownRun;
    nodes.runHint.textContent = nodes.runHint.hidden ? '' : ui('当前有查询在执行，新问题将排队。');
    const control = groundingControl(state.workspace);
    nodes.groundingInput.disabled = !control.enabled || !conversation || busy;
    if (!control.enabled) nodes.groundingInput.checked = true;
    nodes.strategy.hidden = !nodes.groundingInput.checked;
    nodes.strategy.disabled = nodes.groundingInput.disabled;
    nodes.historyInput.disabled = nodes.groundingInput.disabled;
    nodes.groundingNote.hidden = !control.note;
    nodes.groundingNote.textContent = control.note;
    const latest = turns.filter(function (item) { return item.role === 'assistant'; }).pop();
    const announcement = latest ? (latest.state === 'running' ? stageLabel(latest.stage) : statusLabel(latest.state)) : '';
    if (nodes.live.textContent !== announcement) nodes.live.textContent = announcement;
    renderDetails(doc, nodes, state, source, turns);
  }

  function renderThread(doc, state, nodes, handlers, turns, conversation) {
    const thread = nodes.thread;
    const scrollTop = thread.scrollTop;
    const conversationId = conversation && conversation.conversation.id;
    const follow = nodes.threadConversation !== conversationId || state.sending
      || thread.scrollHeight - scrollTop - thread.clientHeight <= 48;
    nodes.threadConversation = conversationId;
    thread.replaceChildren();
    if (!state.workspace || !state.sources.length) {
      const message = state.team ? ui('你的账号暂时没有获授权的数据源，请联系维护者。')
        : state.workspace ? ui('点击左侧「添加数据库」，选择 SQLite 文件后即可开始提问。') : ui('工作区还不能保存会话。');
      nodes.thread.append(el(doc, 'div', 'empty-thread', state.notice || message));
    } else if (!conversation) {
      const empty = el(doc, 'div', 'empty-thread');
      empty.append(
        el(doc, 'h2', null, ui('开始对话')),
        el(doc, 'p', 'prose', ui('直接输入问题，发送后自动保存为新对话。'))
      );
      nodes.thread.append(empty);
    } else if (!turns.length) {
      nodes.thread.append(el(doc, 'div', 'empty-thread', ui('这段对话还没有问题。')));
    } else {
      turns.forEach(function (item) { nodes.thread.append(renderMessage(doc, item, handlers)); });
    }
    thread.scrollTop = follow ? thread.scrollHeight : scrollTop;
  }

  function renderSidebar(doc, state, nodes, handlers, navigationLocked) {
    nodes.sourceList.replaceChildren();
    state.sources.forEach(function (item) {
      const group = el(doc, 'section', 'database-group');
      group.classList.toggle('source-active', item.id === state.datasourceId);
      const row = el(doc, 'div', 'database-row');
      const list = el(doc, 'nav', 'database-talks');
      list.setAttribute('aria-label', ui('对话') + ': ' + item.name);
      const expanded = state.expandedSources?.[item.id] ?? (item.id === state.datasourceId);
      const arrow = el(doc, 'button', 'database-arrow', expanded ? '▾' : '▸');
      arrow.type = 'button';
      arrow.setAttribute('aria-label', ui('展开或收起 ') + item.name);
      arrow.setAttribute('aria-expanded', String(expanded));
      list.hidden = !expanded;
      arrow.addEventListener('click', function () {
        state.expandedSources = state.expandedSources || {};
        state.expandedSources[item.id] = list.hidden;
        list.hidden = !list.hidden;
        arrow.textContent = list.hidden ? '▸' : '▾';
        arrow.setAttribute('aria-expanded', String(!list.hidden));
      });
      const source = el(doc, 'button', 'source', item.name);
      source.type = 'button';
      source.title = item.name;
      source.disabled = Boolean(navigationLocked);
      source.setAttribute('aria-pressed', String(item.id === state.datasourceId));
      source.addEventListener('click', function () { handlers.pickSource(item.id); });
      const plus = el(doc, 'button', 'new-chat database-add', '+');
      plus.type = 'button';
      plus.title = ui('新对话');
      plus.setAttribute('aria-label', ui('在 ') + item.name + ui(' 中新建对话'));
      plus.disabled = !state.workspace || Boolean(navigationLocked);
      plus.addEventListener('click', function () { handlers.newConversation(item.id); });
      const menu = el(doc, 'details', 'database-menu');
      const menuButton = el(doc, 'summary', null, '⋯');
      menuButton.setAttribute('aria-label', ui('数据库操作：') + item.name);
      menu.append(menuButton);
      const info = el(doc, 'button', 'overview-button', ui('数据库介绍'));
      info.type = 'button';
      info.disabled = !state.workspace || Boolean(navigationLocked);
      info.addEventListener('click', function () { handlers.overview(item.id); });
      menu.append(info);
      row.append(arrow, source, plus, menu);
      state.conversations.filter(function (talk) { return talk.datasource_id === item.id; }).forEach(function (talk) {
        if (state.deleteMode) {
          const label = el(doc, 'label', 'talk-selection');
          const checkbox = el(doc, 'input');
          checkbox.type = 'checkbox';
          checkbox.checked = state.deleteSelection.has(talk.id);
          checkbox.disabled = Boolean(navigationLocked);
          const caption = talk.title || ui('未命名对话');
          checkbox.setAttribute('aria-label', ui('选择对话') + ': ' + caption);
          checkbox.addEventListener('change', function () { handlers.selectForDeletion(talk.id, checkbox.checked); });
          label.append(checkbox, el(doc, 'span', null, caption));
          list.append(label);
          return;
        }
        const button = el(doc, 'button', 'talk', talk.title || ui('未命名对话'));
        button.type = 'button';
        button.title = talk.title || '';
        button.disabled = Boolean(navigationLocked);
        if (state.detail?.conversation.id === talk.id) button.setAttribute('aria-current', 'true');
        button.addEventListener('click', function () { handlers.pickConversation(talk.id); });
        list.append(button);
      });
      group.append(row, list);
      nodes.sourceList.append(group);
    });
  }

  function renderDetails(doc, nodes, state, source, turns) {
    const selected = turns.find(function (item) { return item.role === 'assistant' && item.id === state.selectedRunId; })
      || turns.filter(function (item) { return item.role === 'assistant'; }).pop()
      || null;
    const question = selected ? previousUser(turns, selected.id) : '';
    nodes.detailSource.textContent = source ? source.name : '';
    nodes.detailQuestion.textContent = question || ui('选择一条回答以查看对应问题。');
    const sqlText = selected && selected.sql ? selected.sql : ui('本次问题暂无 SQL。');
    if (nodes.sql.textContent !== sqlText) nodes.copyStatus.textContent = '';
    nodes.sql.textContent = sqlText;
    nodes.copySql.disabled = !selected || !selected.sql;
    nodes.runState.textContent = selected
      ? statusLabel(selected.state) + (selected.evaluated ? ui(' · 已评测') : ui(' · 未评测'))
      : ui('尚未选择查询');
    nodes.tokenIn.textContent = formatTokens(selected && selected.tokens ? selected.tokens.input : null);
    nodes.tokenOut.textContent = formatTokens(selected && selected.tokens ? selected.tokens.output : null);
    nodes.duration.textContent = formatElapsed(selected ? selected.elapsed : null);
    nodes.usageNote.textContent = selected && !selected.evaluated ? ui('尚未评估答案正确性。') : '';
    const grounding = selected && selected.grounding && selected.grounding.enabled === true ? selected.grounding : null;
    nodes.groundingStatus.textContent = grounding
      ? (ui('已启用数据库依据。检索策略为 ') + (grounding.strategy || 'keyword')
        + ui('。命中的表为 ') + ((grounding.matched_tables || []).join(', ') || ui('无'))
        + ui('。知识文档为 ') + ((grounding.knowledge || []).map(function (item) {
          return (item.title || item.id) + ' v' + item.version;
        }).join(', ') || ui('无'))
        + ui('。这不是 SQL 正确性的证明。'))
      : ui('未使用数据库依据。');
    nodes.trace.replaceChildren();
    if (selected) {
      const li = el(doc, 'li');
      li.append(el(doc, 'p', null, selected.state === 'running' || selected.state === 'stopping'
        ? stageLabel(selected.stage) : statusLabel(selected.state)));
      nodes.trace.append(li);
    }
  }

  function previousUser(turns, runId) {
    const index = turns.findIndex(function (item) { return item.id === runId; });
    if (index <= 0) return '';
    const previous = turns[index - 1];
    return previous && previous.role === 'user' ? previous.body : '';
  }

  function groundingControl(workspace) {
    if (workspace === true) return { enabled: true, label: ui('使用数据库依据'), note: '' };
    return {
      enabled: false,
      label: ui('使用数据库依据'),
      note: ui('未启用工作区，不能使用数据库依据。')
    };
  }

  function messagePayload(question, useGrounding, strategy = 'keyword') {
    const payload = { question: question, use_grounding: useGrounding === true };
    if (payload.use_grounding) payload.retrieval_strategy = strategy;
    return payload;
  }

  function strategySelect(doc, label) {
    const select = el(doc, 'select');
    select.setAttribute('aria-label', label);
    [['keyword', ui('关键词')], ['bm25', 'BM25'], ['hybrid', ui('混合检索')]].forEach(function ([value, text]) {
      const option = el(doc, 'option', null, text);
      option.value = value;
      select.append(option);
    });
    return select;
  }

  function overviewError(status) {
    if (status === 404) return ui('未启用工作区。请带 --workspace-db 重新启动后刷新。');
    if (status === 400) return ui('无法读取这个数据源的介绍。');
    return ui('无法读取数据库介绍。');
  }

  function overviewSummary(profile) {
    const tables = profile && Array.isArray(profile.tables) ? profile.tables : [];
    const coverage = profile && profile.coverage || {};
    return {
      name: profile && profile.name || '',
      kind: profile && profile.kind || '',
      tableCount: Number.isFinite(coverage.table_count) ? coverage.table_count : tables.length,
      fingerprint: profile && profile.fingerprint || '',
      extractedAt: profile && profile.extracted_at || null,
      schemaFacts: 'catalog',
      businessDefinitions: 'not_inferred'
    };
  }

  function schemaTables(profile) {
    return (profile && profile.tables || []).map(function (table) {
      return {
        name: table.name,
        columns: (table.columns || []).map(function (column) {
          return { name: column.name, type: column.type || '', primaryKey: column.primary_key === true };
        }),
        primaryKey: Array.isArray(table.primary_key) ? table.primary_key.slice() : [],
        foreignKeys: (table.foreign_keys || []).filter(function (key) {
          return key && key.origin === 'declared_foreign_key';
        }).map(function (key) {
          return {
            from: (key.from_columns || []).join(', '),
            to: (key.to_table || '') + '(' + (key.to_columns || []).join(', ') + ')'
          };
        })
      };
    });
  }

  function knowledgeCards(documents, datasourceId) {
    return (documents || []).filter(function (item) {
      return item && item.datasource_id === datasourceId;
    }).map(function (item) {
      return {
        id: item.id,
        title: item.title,
        format: item.format,
        version: item.version,
        provenance: item.provenance === 'user' ? 'user' : '',
        verification: 'user_provided_unverified',
        body: item.body || ''
      };
    });
  }

  function renderOverview(doc, model, handlers) {
    const root = el(doc, 'div', 'overview');
    const summary = model.summary;
    const head = el(doc, 'section', 'overview-block');
    head.append(
      el(doc, 'h2', null, ui('数据库摘要')),
      el(doc, 'p', 'prose', summary.name + ' · ' + (summary.kind || 'sqlite')),
      el(doc, 'p', 'prose', summary.tableCount + ui(' 张表。结构信息来自数据库目录，业务含义需由知识文档补充。'))
    );
    if (summary.fingerprint) head.append(el(doc, 'p', 'quiet', ui('指纹 ') + summary.fingerprint));
    head.append(el(doc, 'p', 'quiet', ui('抽取时间 ') + (summary.extractedAt ? formatWhen(summary.extractedAt) : ui('未记录'))));
    root.append(head);

    const schema = el(doc, 'section', 'overview-block');
    schema.append(el(doc, 'h2', null, ui('表结构')));
    if (!model.tables.length) {
      schema.append(el(doc, 'p', 'prose', ui('这个数据库没有用户表。')));
    }
    model.tables.forEach(function (table) {
      const details = el(doc, 'details', 'schema-table');
      details.append(el(doc, 'summary', null, table.name));
      if (table.columns.length) {
        const wrap = el(doc, 'div', 'table-wrap');
        const grid = el(doc, 'table');
        const headRow = el(doc, 'tr');
        [ui('列'), ui('类型'), ui('主键')].forEach(function (label) {
          const cell = el(doc, 'th', null, label);
          cell.scope = 'col';
          headRow.append(cell);
        });
        const thead = el(doc, 'thead');
        thead.append(headRow);
        const tbody = el(doc, 'tbody');
        table.columns.forEach(function (column) {
          const line = el(doc, 'tr');
          line.append(
            el(doc, 'td', null, column.name),
            el(doc, 'td', null, column.type || ui('未声明')),
            el(doc, 'td', null, column.primaryKey ? ui('是') : '')
          );
          tbody.append(line);
        });
        grid.append(thead, tbody);
        wrap.append(grid);
        details.append(wrap);
      } else {
        details.append(el(doc, 'p', 'quiet', ui('没有列。')));
      }
      details.append(el(doc, 'p', 'quiet', table.primaryKey.length
        ? ui('声明的主键：') + table.primaryKey.join(', ')
        : ui('没有声明主键。')));
      if (table.foreignKeys.length) {
        table.foreignKeys.forEach(function (key) {
          details.append(el(doc, 'p', 'quiet', ui('声明的外键：') + key.from + ' → ' + key.to));
        });
      } else {
        details.append(el(doc, 'p', 'quiet', ui('没有声明外键。')));
      }
      schema.append(details);
    });
    root.append(schema);

    const knowledge = el(doc, 'section', 'overview-block');
    knowledge.append(
      el(doc, 'h2', null, ui('业务知识')),
      el(doc, 'p', 'unverified', ui('用户提供，未经核验。这些说明不是数据库已经核对过的事实。'))
    );
    if (!model.knowledge.length) {
      knowledge.append(el(doc, 'p', 'prose', ui('还没有业务知识。写下来的内容只属于当前数据源。')));
    }
    model.knowledge.forEach(function (item) {
      const card = el(doc, 'article', 'message');
      card.append(
        el(doc, 'p', 'prose', item.title),
        el(doc, 'p', 'quiet', ui('版本 ') + item.version + ui(' · 来源：用户提供')),
        el(doc, 'p', 'unverified', ui('用户提供／未经核实')),
        el(doc, 'p', 'prose', item.body)
      );
      const edit = el(doc, 'button', 'text-action', ui('编辑'));
      edit.type = 'button';
      edit.addEventListener('click', function () { handlers.edit(item); });
      const remove = el(doc, 'button', 'text-action', ui('删除'));
      remove.type = 'button';
      remove.addEventListener('click', function () { handlers.remove(item); });
      if (!model.readOnly) card.append(edit, remove);
      knowledge.append(card);
    });
    root.append(knowledge);
    return root;
  }

  function renderKnowledgeForm(doc, state, handlers) {
    const draft = state.draft || { title: '', format: 'markdown', body: '', id: null };
    const form = el(doc, 'form', 'overview-block');
    form.dataset.knowledge = '';
    form.autocomplete = 'off';
    form.append(el(doc, 'h2', null, draft.id ? ui('编辑知识') : ui('添加知识')));
    const title = el(doc, 'input');
    title.name = 'title';
    title.value = draft.title;
    title.maxLength = 200;
    title.required = true;
    title.setAttribute('aria-label', ui('知识标题'));
    const format = el(doc, 'select');
    format.name = 'format';
    format.setAttribute('aria-label', ui('知识格式'));
    [['markdown', 'Markdown'], ['text', ui('纯文本')], ['json', 'JSON']].forEach(function (pair) {
      const option = el(doc, 'option', null, pair[1]);
      option.value = pair[0];
      if (pair[0] === draft.format) option.selected = true;
      format.append(option);
    });
    const body = el(doc, 'textarea');
    body.name = 'body';
    body.spellcheck = false;
    body.value = draft.body;
    body.maxLength = 24000;
    body.setAttribute('aria-label', ui('知识正文'));
    const save = el(doc, 'button', 'send', draft.id ? ui('保存修改') : ui('添加知识'));
    save.type = 'submit';
    if (!draft.id) {
      const template = el(doc, 'button', 'secondary', ui('填入指标口径模板'));
      template.type = 'button';
      template.addEventListener('click', function () {
        if (title.value.trim() || body.value.trim()) return;
        title.value = '指标口径：已付款订单总金额';
        format.value = 'json';
        body.value = JSON.stringify({
          table: 'orders', amount_column: 'amount', year_column: 'year',
          status_column: 'status', paid_value: 'paid'
        }, null, 2);
        body.focus();
      });
      function updateTemplate() {
        template.disabled = Boolean(title.value.trim() || body.value.trim());
      }
      template.addEventListener('click', updateTemplate);
      title.addEventListener('input', updateTemplate);
      body.addEventListener('input', updateTemplate);
      updateTemplate();
      form.append(template);
    }
    function labeled(text, input) {
      const label = el(doc, 'label', 'overview-field', text);
      label.append(input);
      return label;
    }
    form.append(labeled(ui('知识标题'), title), labeled(ui('知识格式'), format),
      labeled(ui('知识正文'), body), save);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      handlers.saveKnowledge({
        id: draft.id,
        version: draft.version,
        title: title.value,
        format: format.value,
        body: body.value
      });
    });
    return form;
  }

  function retrievalPreviewPayload(question, strategy) {
    const chosen = strategy === 'bm25' || strategy === 'hybrid' ? strategy : 'keyword';
    return { question: question, retrieval_strategy: chosen };
  }

  function renderRetrievalForm(doc, handlers) {
    const form = el(doc, 'form', 'overview-block');
    form.append(
      el(doc, 'h2', null, ui('检索预览')),
      el(doc, 'p', 'quiet', ui('按当前数据源的目录和业务知识做匹配。不会执行 SQL，也不会把结果交给 Agent。'))
    );
    form.dataset.preview = '';
    form.autocomplete = 'off';
    const input = el(doc, 'input');
    input.name = 'question';
    input.required = true;
    input.setAttribute('aria-label', ui('检索问题'));
    input.maxLength = 1000;
    const strategy = strategySelect(doc, ui('预览检索策略'));
    strategy.name = 'strategy';
    const button = el(doc, 'button', 'send', ui('查看匹配'));
    button.type = 'submit';
    const search = el(doc, 'label', 'overview-field', ui('检索问题'));
    search.append(input);
    const method = el(doc, 'label', 'overview-field', ui('预览检索策略'));
    method.append(strategy);
    form.append(search, method, button);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      handlers.preview(input.value, strategy.value);
    });
    return form;
  }

  function scoreLine(item) {
    if (item.score_kind === 'bm25' && typeof item.bm25_score === 'number') {
      return 'BM25 ' + item.bm25_score.toFixed(6) + ui('（数值越小越相关，与结构得分分别展示）');
    }
    if (typeof item.score === 'number') return ui('关键词得分 ') + item.score;
    return '';
  }

  function renderGrounding(doc, result) {
    const section = el(doc, 'section', 'overview-block');
    section.append(el(doc, 'h2', null, ui('这次问题匹配到的内容')), el(doc, 'p', 'prose', result.question));
    const latency = typeof result.retrieval_latency_ms === 'number' ? (' · ' + result.retrieval_latency_ms + ' ms') : '';
    section.append(el(doc, 'p', 'quiet', ui('检索策略 ') + (result.strategy || 'keyword') + latency));
    if (!(result.tables || []).length && !(result.knowledge || []).length) {
      section.append(el(doc, 'p', 'prose', ui('没有匹配到表、字段或知识。')));
    }
    (result.tables || []).forEach(function (table) {
      const card = el(doc, 'article', 'message');
      const columns = (table.columns || []).map(function (column) {
        return column.name + ' (' + column.reason + ')';
      }).join(', ');
      card.append(
        el(doc, 'p', 'prose', ui('表 ') + table.name),
        el(doc, 'p', 'quiet', ui('数据库结构事实 · ') + table.reason + (scoreLine(table) ? ' · ' + scoreLine(table) : '')),
        el(doc, 'p', 'prose', columns ? (ui('字段 ') + columns) : ui('没有单独命中的字段'))
      );
      section.append(card);
    });
    (result.relationships || []).forEach(function (item) {
      section.append(el(doc, 'p', 'quiet',
        ui('声明的外键：') + item.from_table + '.' + (item.from_columns || []).join(',') +
        ' → ' + item.to_table + '.' + (item.to_columns || []).join(',') + '. ' + item.reason));
    });
    (result.knowledge || []).forEach(function (item) {
      const card = el(doc, 'article', 'message');
      card.append(
        el(doc, 'p', 'prose', item.title),
        el(doc, 'p', 'unverified', ui('用户提供／未经核实')),
        el(doc, 'p', 'quiet', ui('用户提供的定义 · 版本 ') + item.version + ' · ' + item.reason
          + (scoreLine(item) ? ' · ' + scoreLine(item) : '')),
        el(doc, 'p', 'prose', item.excerpt || '')
      );
      if (item.instruction_like_text) {
        card.append(el(doc, 'p', 'quiet', ui('这段文字仍只是用户说明，不会当作系统指令。')));
      }
      section.append(card);
    });
    section.append(el(doc, 'p', 'quiet', ui('表和字段来自数据库目录。知识不能覆盖系统规则。')));
    return section;
  }

  function createState() {
    return {
      sources: [],
      conversations: [],
      detail: null,
      datasourceId: null,
      detailsOpen: false,
      selectedRunId: null,
      activeRunId: null,
      team: false, accountName: '', organizationLogout: false, deleteMode: false, deleteSelection: new Set(),
      workspace: false,
      notice: '',
      error: '',
      sending: false,
      navigating: false,
      view: 'chat',
      overview: null,
      draft: null,
      grounding: null
    };
  }

  function mount(doc, options) {
    const state = createState();
    if (doc.defaultView?.matchMedia?.('(max-width: 960px)').matches) state.detailsOpen = false;
    const drafts = new Map();
    let composerConversation;
    let revision = 0;
    let stopping = false;
    const transport = options && options.transport;
    const app = doc.getElementById('app');
    const shell = el(doc, 'div', 'shell');
    doc.documentElement.lang = locale;
    const skip = doc.querySelector('.skip');
    if (skip) skip.textContent = ui('跳转到对话');
    const systemTheme = doc.defaultView?.matchMedia?.('(prefers-color-scheme: dark)');
    let theme = readPref('theme') || 'system';
    function applyTheme() {
      const dark = theme === 'dark' || (theme === 'system' && systemTheme?.matches);
      doc.documentElement.dataset.theme = dark ? 'dark' : 'light';
      doc.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#151B22' : '#F4F6F8');
    }
    applyTheme();
    systemTheme?.addEventListener('change', applyTheme);
    const sidebar = el(doc, 'aside', 'sidebar');
    sidebar.id = 'workspace-navigation';
    sidebar.setAttribute('aria-label', ui('工作区'));
    const brand = el(doc, 'div', 'brand');
    brand.append(el(doc, 'p', 'brand-name', ui('SQL 智能问答工作区')), el(doc, 'p', 'brand-note', ui('本地只读工作区')));
    const sourceLabel = el(doc, 'p', 'side-label source-label', ui('数据源'));
    const sourceList = el(doc, 'div', 'source-list');
    sourceList.setAttribute('role', 'group');
    sourceList.setAttribute('aria-label', ui('数据源'));
    const newer = el(doc, 'button', 'new-chat', ui('新建会话'));
    newer.type = 'button';
    const overviewButton = el(doc, 'button', 'new-chat overview-button', ui('数据库介绍'));
    overviewButton.type = 'button';
    const talkLabel = el(doc, 'p', 'side-label talk-label', ui('对话'));
    const talkList = el(doc, 'nav', 'talk-list');
    talkList.setAttribute('aria-label', ui('对话'));
    const foot = el(doc, 'details', 'sidebar-foot');
    foot.append(el(doc, 'summary', null, ui('开发工具')));
    const singleQuery = el(doc, 'a', null, ui('单次问答'));
    singleQuery.href = '/';
    const observatory = el(doc, 'a', null, ui('观察台'));
    observatory.href = '/observatory';
    foot.append(singleQuery, observatory);
    const addSource = el(doc, 'button', 'add-database', ui('+ 添加数据库'));
    addSource.type = 'button';
    const upload = el(doc, 'dialog', 'upload-dialog');
    const uploadForm = el(doc, 'form');
    const uploadTitle = el(doc, 'h2', null, ui('添加数据库'));
    uploadTitle.id = 'upload-title';
    upload.setAttribute('aria-labelledby', uploadTitle.id);
    uploadForm.autocomplete = 'off';
    const file = el(doc, 'input'); file.type = 'file'; file.accept = '.sqlite,.db,.sqlite3'; file.required = true;
    const nameInput = el(doc, 'input'); nameInput.name = 'database_name'; nameInput.required = true; nameInput.maxLength = 100;
    const description = el(doc, 'textarea'); description.name = 'description'; description.maxLength = 10000;
    function field(label, input) { const node = el(doc, 'label', 'upload-field', label); node.append(input); return node; }
    const uploadError = el(doc, 'p', 'form-error');
    uploadError.setAttribute('role', 'alert');
    const uploadStatus = el(doc, 'p', 'hint');
    uploadStatus.setAttribute('role', 'status');
    const saveSource = el(doc, 'button', null, ui('添加并开始提问')); saveSource.type = 'submit';
    const cancelUpload = el(doc, 'button', null, ui('取消')); cancelUpload.type = 'button';
    cancelUpload.addEventListener('click', function () { upload.close(); });
    upload.addEventListener('cancel', function (event) {
      if (saveSource.disabled) event.preventDefault();
    });
    uploadForm.append(uploadTitle, el(doc, 'p', null, ui('上传完整、未加密的 SQLite 文件，最大20 MB。数据库以只读方式使用。')),
      field(ui('数据库文件'), file), field(ui('名称'), nameInput), field(ui('业务说明（可选，例如金额单位、付款状态与统计口径）'), description), uploadError, uploadStatus, saveSource, cancelUpload);
    upload.append(uploadForm);
    const closeNavigation = el(doc, 'button', 'nav-close secondary', ui('关闭导航'));
    closeNavigation.type = 'button';
    const preferences = el(doc, 'div', 'preferences');
    function choice(className, label, values, value) {
      const select = el(doc, 'select', className);
      select.setAttribute('aria-label', label);
      for (const [key, text] of values) {
        const option = el(doc, 'option', null, text);
        option.value = key;
        select.append(option);
      }
      select.value = value;
      const field = el(doc, 'label', 'preference', label);
      field.append(select);
      preferences.append(field);
      return select;
    }
    const languageSelect = choice('language-select', ui('界面语言'),
      [['zh-CN', '简体中文'], ['en', 'English']], locale);
    const themeSelect = choice('theme-select', ui('主题'),
      [['system', ui('跟随系统')], ['light', ui('浅色')], ['dark', ui('深色')]], theme);
    themeSelect.addEventListener('change', function () {
      theme = themeSelect.value;
      writePref('theme', theme);
      applyTheme();
    });
    languageSelect.addEventListener('change', function () {
      try { root.sessionStorage?.setItem('sql-studio-draft', question.value); } catch { /* optional */ }
      writePref('language', languageSelect.value);
      root.location.reload();
    });
    const management = el(doc, 'div', 'talk-management');
    const manage = el(doc, 'button', 'secondary', ui('管理对话'));
    const deleteSelected = el(doc, 'button', 'secondary delete-selected', ui('删除选中'));
    const signOut = el(doc, 'button', 'secondary sign-out', ui('退出登录'));
    for (const button of [manage, deleteSelected, signOut]) button.type = 'button';
    management.append(manage, deleteSelected);
    sidebar.append(brand, closeNavigation, sourceLabel, addSource, management, sourceList, preferences, signOut, foot, upload);
    manage.addEventListener('click', function () {
      state.deleteMode = !state.deleteMode;
      state.deleteSelection.clear();
      paint();
    });
    deleteSelected.addEventListener('click', function () {
      const ids = Array.from(state.deleteSelection);
      if (!ids.length || !root.confirm(ui('删除选中的对话？对话将从工作区隐藏。'))) return;
      navigate(async function () {
        await request('/api/conversations/delete', { ids: ids });
        if (state.detail && ids.includes(state.detail.conversation.id)) {
          state.detail = null;
          state.selectedRunId = null;
          selectDraft('new:' + state.datasourceId);
          remember();
        }
        for (const id of ids) drafts.delete(id);
        state.deleteSelection.clear();
        state.deleteMode = false;
        await refreshList();
      });
    });
    signOut.addEventListener('click', function () {
      if (state.organizationLogout) {
        root.location.assign('/auth/logout');
        return;
      }
      navigate(async function () {
        await request('/api/session', undefined, 'DELETE');
        root.location.href = '/workspace/';
      });
    });
    addSource.addEventListener('click', function () {
      uploadError.textContent = '';
      upload.showModal();
    });
    uploadForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!file.files[0] || file.files[0].size > 20 * 1024 * 1024) { uploadError.textContent = ui('请选择不超过20 MB的 SQLite 文件'); return; }
      saveSource.disabled = true;
      cancelUpload.disabled = true;
      uploadError.textContent = '';
      uploadStatus.textContent = ui('正在校验并添加…');
      try {
        const data = await new Promise(function (resolve, reject) {
          const reader = new FileReader(); reader.onload = function () { resolve(reader.result.split(',')[1]); }; reader.onerror = reject; reader.readAsDataURL(file.files[0]);
        });
        const result = await request('/api/sources/upload', { name: nameInput.value, description: description.value, data: data });
        state.sources = publicSources({ sources: state.sources.concat(result.source) });
        state.workspace = true; state.notice = ''; upload.close(); uploadForm.reset();
        handlers.newConversation(result.source.id);
      } catch (error) { uploadError.textContent = error.message || ui('添加失败，请重试'); }
      finally { saveSource.disabled = false; cancelUpload.disabled = false; uploadStatus.textContent = ''; }
    });

    const chat = el(doc, 'main', 'chat');
    chat.id = 'conversation';
    chat.tabIndex = -1;
    const head = el(doc, 'div', 'chat-head');
    const heading = el(doc, 'div');
    const title = el(doc, 'h1');
    const subtitle = el(doc, 'p');
    heading.append(title, subtitle);
    const toggle = el(doc, 'button', 'detail-toggle', ui('收起详情'));
    toggle.type = 'button';
    const navToggle = el(doc, 'button', 'nav-toggle secondary', '☰');
    navToggle.type = 'button';
    navToggle.setAttribute('aria-label', ui('数据源与会话'));
    navToggle.setAttribute('aria-controls', sidebar.id);
    const mobile = doc.defaultView?.matchMedia?.('(max-width: 760px)');
    function setNavigation(open, restoreFocus = false) {
      const active = Boolean(mobile?.matches && open);
      shell.classList.toggle('nav-open', active);
      sidebar.inert = Boolean(mobile?.matches && !active);
      chat.inert = active;
      details.inert = active;
      navToggle.setAttribute('aria-expanded', String(active));
      if (active) closeNavigation.focus();
      else if (restoreFocus) navToggle.focus();
    }
    navToggle.addEventListener('click', () => setNavigation(true));
    closeNavigation.addEventListener('click', () => setNavigation(false, true));
    mobile?.addEventListener('change', () => setNavigation(false));
    doc.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !upload.open && shell.classList.contains('nav-open')) {
        setNavigation(false, true);
      } else if (event.key === 'Escape' && mobile?.matches && state.detailsOpen && !upload.open) {
        state.detailsOpen = false;
        paint();
        toggle.focus();
      }
    });
    head.append(navToggle, heading, toggle, overviewButton);
    const banner = el(doc, 'p', 'demo-banner');
    banner.setAttribute('role', 'status');
    banner.hidden = true;
    const thread = el(doc, 'div', 'thread');
    const live = el(doc, 'div', 'visually-hidden');
    live.setAttribute('aria-live', 'polite');
    const form = el(doc, 'form', 'composer');
    const runHint = el(doc, 'p', 'hint');
    runHint.setAttribute('role', 'status');
    const groundingLabel = el(doc, 'label', 'grounding-toggle');
    const groundingInput = el(doc, 'input');
    groundingInput.type = 'checkbox';
    groundingInput.checked = true;
    groundingLabel.append(groundingInput, doc.createTextNode(ui(' 参考业务说明')));
    const historyLabel = el(doc, 'label', 'grounding-toggle');
    const historyInput = el(doc, 'input');
    historyInput.type = 'checkbox';
    historyInput.checked = true;
    historyLabel.append(historyInput, doc.createTextNode(ui(' 关联前文')));
    const groundingOptions = el(doc, 'div', 'grounding-options');
    const strategy = strategySelect(doc, ui('数据库依据检索策略'));
    const retrievalSettings = el(doc, 'details', 'retrieval-settings');
    retrievalSettings.append(el(doc, 'summary', null, ui('检索方式')), strategy);
    groundingOptions.append(groundingLabel,
      el(doc, 'p', 'setting-help', ui('参考这个数据库的结构和业务说明，帮助理解字段与统计口径。')),
      historyLabel, el(doc, 'p', 'setting-help', ui('追问时参考当前对话，例如“那杭州呢？”；新话题可新建会话。')),
      retrievalSettings);
    const querySettings = el(doc, 'details', 'query-settings');
    querySettings.append(el(doc, 'summary', null, ui('查询设置')), groundingOptions);
    const groundingNote = el(doc, 'p', 'quiet');
    const box = el(doc, 'div', 'composer-box');
    const question = el(doc, 'textarea');
    question.id = 'question';
    question.name = 'question';
    question.maxLength = 4000;
    question.autocomplete = 'off';
    question.placeholder = ui('用自然语言描述要查询的内容');
    question.setAttribute('enterkeyhint', 'send');
    question.setAttribute('aria-label', ui('输入问题'));
    const actions = el(doc, 'div', 'composer-actions');
    const send = el(doc, 'button', 'send', ui('发送'));
    send.type = 'submit';
    actions.append(send);
    box.append(question, actions);
    const error = el(doc, 'p', 'form-error');
    error.setAttribute('role', 'alert');
    querySettings.append(groundingNote);
    form.append(box, runHint, el(doc, 'p', 'hint keyboard-hint', ui('Ctrl + Enter 发送')));
    chat.append(head, banner, live, thread, error, form);

    const details = el(doc, 'aside', 'details');
    details.id = 'query-details';
    details.tabIndex = -1;
    details.setAttribute('aria-label', ui('查询详情'));
    toggle.setAttribute('aria-controls', details.id);
    const sourceBlock = el(doc, 'div', 'detail-block');
    sourceBlock.append(el(doc, 'h2', null, ui('数据源')), el(doc, 'p'));
    const questionBlock = el(doc, 'div', 'detail-block');
    questionBlock.append(el(doc, 'h2', null, ui('输入问题')), el(doc, 'p', 'prose'));
    const sqlBlock = el(doc, 'div', 'detail-block');
    const sql = el(doc, 'pre', 'sql');
    sql.translate = false;
    const copySql = el(doc, 'button', 'text-action', ui('复制 SQL'));
    copySql.type = 'button';
    const copyStatus = el(doc, 'span', 'quiet');
    copyStatus.setAttribute('role', 'status');
    copySql.addEventListener('click', async function () {
      const text = sql.textContent;
      try {
        await navigator.clipboard.writeText(text);
        if (sql.textContent === text) copyStatus.textContent = ui('已复制');
      } catch {
        if (sql.textContent === text) copyStatus.textContent = ui('请选中上方 SQL 手动复制');
      }
    });
    sqlBlock.append(el(doc, 'h2', null, 'SQL'), sql, copySql, copyStatus);
    const stateBlock = el(doc, 'div', 'detail-block');
    stateBlock.append(el(doc, 'h2', null, ui('执行状态')), el(doc, 'p'));
    const metricBlock = el(doc, 'div', 'detail-block');
    metricBlock.append(el(doc, 'h2', null, ui('Token 和耗时')));
    const metrics = el(doc, 'div', 'metrics');
    function metric(label) {
      const wrap = el(doc, 'div');
      const strong = el(doc, 'strong', null, ui('未记录'));
      wrap.append(el(doc, 'span', null, label), strong);
      metrics.append(wrap);
      return strong;
    }
    const tokenIn = metric(ui('输入 token'));
    const tokenOut = metric(ui('输出 token'));
    const duration = metric(ui('耗时'));
    const usageNote = el(doc, 'p', 'quiet');
    metricBlock.append(metrics, usageNote);
    const groundingBlock = el(doc, 'div', 'detail-block');
    const groundingStatus = el(doc, 'p', 'prose', ui('未使用数据库依据。'));
    groundingBlock.append(el(doc, 'h2', null, ui('数据库依据')), groundingStatus);
    const traceBlock = el(doc, 'div', 'detail-block');
    const traceDetails = el(doc, 'details');
    const trace = el(doc, 'ol', 'trace');
    traceDetails.append(el(doc, 'summary', null, ui('当前阶段')), trace);
    traceBlock.append(traceDetails);
    details.append(sourceBlock, questionBlock, sqlBlock, stateBlock, metricBlock, groundingBlock, traceBlock);
    shell.append(sidebar, chat, details);
    app.replaceChildren(shell);
    setNavigation(false);

    const nodes = {
      details: details, toggle: toggle, title: title, subtitle: subtitle, banner: banner,
      sourceList: sourceList, talkList: talkList, thread: thread, runHint: runHint, error: error,
      historyInput: historyInput, send: send, live: live, form: form, question: question, strategy: strategy,
      manage, deleteSelected, signOut, foot,
      newer: newer, addSource: addSource, overviewButton: overviewButton, detailSource: sourceBlock.querySelector('p'),
      detailQuestion: questionBlock.querySelector('p'), sql: sql, copySql: copySql, copyStatus: copyStatus,
      runState: stateBlock.querySelector('p'), tokenIn: tokenIn, tokenOut: tokenOut,
      duration: duration, usageNote: usageNote, trace: trace,
      groundingInput: groundingInput, groundingNote: groundingNote, groundingStatus: groundingStatus
    };

    function paint() { render(doc, state, nodes, handlers); }

    function showError(text) {
      state.error = text || '';
      nodes.error.hidden = !state.error;
      nodes.error.textContent = state.error;
    }

    async function request(path, payload, method) {
      const verb = method || (payload === undefined ? 'GET' : 'POST');
      if (transport) return transport(path, payload, verb);
      const response = await fetch(path, {
        method: verb,
        headers: payload === undefined ? {} : { 'Content-Type': 'application/json' },
        body: payload === undefined ? undefined : JSON.stringify(payload)
      });
      let data = {};
      try { data = await response.json(); } catch (error) { data = {}; }
      if (!response.ok) {
        if (response.status === 401) root.location.reload();
        const failure = new Error(data.error || ui('请求未完成'));
        failure.status = response.status;
        throw failure;
      }
      return data;
    }

    function remember() {
      if (typeof history === 'undefined' || !history.replaceState) return;
      const params = new URLSearchParams();
      if (state.datasourceId) params.set('datasource', state.datasourceId);
      if (state.view === 'overview') params.set('view', 'overview');
      else if (state.detail) params.set('conversation', state.detail.conversation.id);
      history.replaceState(null, '', '/workspace/?' + params.toString());
    }

    function selectDraft(conversationId) {
      if (composerConversation === conversationId) return;
      if (composerConversation && question.value.trim()) drafts.set(composerConversation, question.value);
      else drafts.delete(composerConversation);
      composerConversation = conversationId;
      question.value = drafts.get(conversationId) || '';
      groundingInput.checked = true;
      historyInput.checked = true;
      strategy.value = 'keyword';
    }

    function applyDetail(detail) {
      selectDraft(detail.conversation.id);
      state.detail = detail;
      state.datasourceId = detail.conversation.datasource_id;
      state.view = 'chat';
      const turns = pairTurns(detail);
      const assistants = turns.filter(function (item) { return item.role === 'assistant'; });
      const still = assistants.some(function (item) { return item.id === state.selectedRunId; });
      if (!still) state.selectedRunId = assistants.length ? assistants[assistants.length - 1].id : null;
      remember();
    }

    async function openConversation(id) {
      const version = revision;
      const detail = await request('/api/conversations/' + encodeURIComponent(id));
      if (version !== revision) return;
      applyDetail(detail);
      const active = detail.runs.find(function (run) { return ['running', 'stopping'].includes(run.state); });
      if (active) state.activeRunId = active.id;
      else if (detail.runs.some(function (run) { return run.id === state.activeRunId; })) state.activeRunId = null;
      paint();
      return detail;
    }

    async function refreshList() {
      const version = revision;
      const listed = await request('/api/conversations');
      if (version !== revision) return;
      state.conversations = listed.conversations || [];
      state.activeRunId = listed.active_run_id || null;
    }

    async function poll() {
      const version = revision;
      try {
        if (!state.workspace || state.navigating || state.sending || stopping) return;
        const active = state.activeRunId;
        await refreshList();
        if (version !== revision) return;
        const currentId = state.detail?.conversation.id;
        if (currentId && !state.conversations.some(conversation => conversation.id === currentId)) {
          state.detail = null;
          state.selectedRunId = null;
          state.detailsOpen = false;
          selectDraft('new:' + state.datasourceId);
          drafts.delete(currentId);
          remember();
          paint();
        } else if (state.view === 'chat' && state.detail && (active || state.activeRunId || state.detail.runs.some(run => run.state === 'queued'))) {
          await openConversation(state.detail.conversation.id);
        } else paint();
        if (version === revision && state.notice) {
          state.notice = '';
          nodes.banner.hidden = true;
          nodes.banner.textContent = '';
        }
      } catch {
        if (version !== revision) return;
        state.notice = ui('连接中断，正在重试…');
        nodes.banner.hidden = false;
        nodes.banner.textContent = state.notice;
      } finally {
        schedulePoll();
      }
    }

    function schedulePoll() {
      if (state.workspace) setTimeout(poll, 1000);
    }

    async function navigate(action) {
      if (state.navigating || state.sending || stopping) return;
      state.navigating = true;
      revision += 1;
      showError('');
      paint();
      try { await action(); }
      catch (error) { showError(error.message); }
      finally { state.navigating = false; paint(); }
    }

    async function boot() {
      try {
        const catalog = await request('/api/catalog');
        state.sources = publicSources(catalog);
        state.team = catalog.workspace_limited === true;
        state.accountName = catalog.account?.name || '';
        state.organizationLogout = catalog.account?.logout_path === '/auth/logout';
        try {
          await refreshList();
          state.workspace = true;
        } catch (error) {
          state.workspace = false;
          state.conversations = [];
          if (error.status !== 404) throw error;
        }
        if (!state.workspace) {
          state.notice = ui('未启用工作区。请带 --workspace-db 重新启动后刷新。当前不会把演示数据当成查询结果。');
        } else if (state.sources.length) {
          state.notice = '';
          const params = new URLSearchParams(options && options.search != null ? options.search : (typeof location !== 'undefined' ? location.search : ''));
          const requestedSource = params.get('datasource');
          if (sourceById(state.sources, requestedSource)) state.datasourceId = requestedSource;
          else state.datasourceId = state.sources[0].id;
          const requested = params.get('conversation');
          const known = state.conversations.some(function (item) { return item.id === requested; });
          const first = talksFor(state)[0];
          if (params.get('view') === 'overview') {
            state.view = 'overview';
            await loadOverview();
          } else if (known) await openConversation(requested);
          else if (first) await openConversation(first.id);
          try {
            const savedDraft = root.sessionStorage?.getItem('sql-studio-draft');
            root.sessionStorage?.removeItem('sql-studio-draft');
            if (savedDraft) question.value = savedDraft;
          } catch { /* optional */ }
        }
      } catch (error) {
        state.notice = ui('无法连接本地服务。确认服务已启动后刷新此页。');
        showError(error.message);
      }
      paint();
      schedulePoll();
    }

    async function loadOverview() {
      state.overviewError = '';
      state.grounding = null;
      try {
        const id = encodeURIComponent(state.datasourceId);
        const [profile, context] = await Promise.all([
          request('/api/sources/' + id + '/profile'),
          request('/api/sources/' + id + '/context')
        ]);
        if (context.datasource_id !== state.datasourceId) {
          throw Object.assign(new Error(ui('数据源不一致')), { status: 400 });
        }
        state.overview = { profile: profile, knowledge: context.knowledge || [] };
      } catch (error) {
        state.overview = null;
        state.overviewError = overviewError(error.status);
      }
      remember();
      paint();
    }

    const handlers = {
      selectForDeletion: function (id, selected) {
        if (selected) state.deleteSelection.add(id);
        else state.deleteSelection.delete(id);
        paint();
      },
      showSql: function (id) {
        state.selectedRunId = id;
        state.detailsOpen = true;
        paint();
        details.focus();
      },
      stop: async function (id) {
        if (stopping || state.sending || !state.detail) return;
        stopping = true;
        revision += 1;
        showError('');
        try {
          await request('/api/stop', { id: id });
          await openConversation(state.detail.conversation.id);
        } catch (error) {
          showError(error.message);
        } finally {
          stopping = false;
          paint();
        }
      },
      newConversation: function (id) {
        if (shell.classList.contains('nav-open')) setNavigation(false, true);
        navigate(async function () {
          state.datasourceId = id;
          selectDraft('new:' + id);
          state.detail = null;
          state.selectedRunId = null;
          state.view = 'chat';
          state.detailsOpen = false;
          state.expandedSources = { ...state.expandedSources, [id]: true };
          remember();
          question.focus();
        });
      },
      overview: function (id) {
        if (shell.classList.contains('nav-open')) setNavigation(false, true);
        navigate(async function () {
          if (state.datasourceId !== id) { state.detail = null; selectDraft('new:' + id); }
          state.datasourceId = id;
          state.view = 'overview';
          state.overview = null;
          state.draft = null;
          await loadOverview();
        });
      },
      pickSource: function (id) {
        if (shell.classList.contains('nav-open')) setNavigation(false, true);
        if (!sourceById(state.sources, id) || state.datasourceId === id) return;
        navigate(async function () {
          selectDraft('new:' + id);
          state.expandedSources = { ...state.expandedSources, [id]: true };
          state.datasourceId = id;
          state.detail = null;
          state.selectedRunId = null;
          state.overview = null;
          state.draft = null;
          state.grounding = null;
          if (state.view === 'overview') await loadOverview();
          else {
            const first = talksFor(state)[0];
            if (first) await openConversation(first.id);
            else remember();
          }
        });
      },
      pickConversation: function (id) {
        if (shell.classList.contains('nav-open')) setNavigation(false, true);
        navigate(function () { return openConversation(id); });
      },
      edit: function (item) {
        state.draft = { id: item.id, version: item.version, title: item.title, format: item.format, body: item.body };
        paint();
      },
      remove: function (item) {
        navigate(async function () {
          const id = encodeURIComponent(state.datasourceId);
          await request('/api/sources/' + id + '/knowledge/' + encodeURIComponent(item.id), undefined, 'DELETE');
          state.draft = null;
          await loadOverview();
        });
      },
      saveKnowledge: function (draft) {
        navigate(async function () {
          const id = encodeURIComponent(state.datasourceId);
          const path = draft.id
            ? '/api/sources/' + id + '/knowledge/' + encodeURIComponent(draft.id)
            : '/api/sources/' + id + '/knowledge';
          await request(path, { title: draft.title.trim(), format: draft.format, body: draft.body,
            expected_version: draft.version }, draft.id ? 'PUT' : 'POST');
          state.draft = null;
          await loadOverview();
        });
      },
      preview: function (text, strategy) {
        const question = String(text || '').trim();
        if (!question || !state.datasourceId) return;
        navigate(async function () {
          const id = encodeURIComponent(state.datasourceId);
          const result = await request('/api/sources/' + id + '/grounding', retrievalPreviewPayload(question, strategy), 'POST');
          if (result.datasource_id !== state.datasourceId) throw new Error(ui('数据源不一致'));
          state.grounding = result;
        });
      }
    };

    overviewButton.addEventListener('click', function () {
      navigate(async function () {
        if (state.view === 'overview') {
          const first = state.detail?.conversation || talksFor(state)[0];
          if (first) await openConversation(first.id);
          else {
            state.view = 'chat';
            remember();
          }
          return;
        }
        state.view = 'overview';
        state.draft = null;
        state.overview = null;
        paint();
        await loadOverview();
      });
    });

    newer.addEventListener('click', function () {
      navigate(async function () {
        const created = await request('/api/conversations', { datasource_id: state.datasourceId });
        await refreshList();
        await openConversation(created.id);
        question.focus();
      });
    });
    toggle.addEventListener('click', function () {
      state.detailsOpen = !state.detailsOpen;
      paint();
    });
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      const text = question.value.trim();
      if (!text || state.navigating || state.sending || !state.workspace || !state.datasourceId) {
        paint();
        return;
      }
      state.sending = true;
      revision += 1;
      state.error = '';
      paint();
      Promise.resolve().then(async function () {
        if (!state.detail) {
          const created = await request('/api/conversations', { datasource_id: state.datasourceId, title: text.slice(0, 40) });
          await openConversation(created.id);
          question.value = text;
        }
        return request('/api/conversations/' + encodeURIComponent(state.detail.conversation.id) + '/messages',
        { ...messagePayload(text, nodes.groundingInput.checked, strategy.value),
          use_conversation_context: historyInput.checked });
      })
        .then(function (started) {
          question.value = '';
          state.activeRunId = started.id;
          state.selectedRunId = started.id;
          return refreshList().then(function () { return openConversation(state.detail.conversation.id); });
        })
        .catch(function (error) { showError(error.message); })
        .finally(function () { state.sending = false; paint(); });
    });
    question.addEventListener('input', function () {
      nodes.send.disabled = !state.workspace || !state.datasourceId || state.navigating || state.sending || !question.value.trim();
    });
    groundingInput.addEventListener('change', function () { strategy.hidden = !groundingInput.checked; });
    question.addEventListener('keydown', function (event) {
      if (!event.isComposing && event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    paint();
    boot();
    return { state: state, nodes: nodes };
  }

  const api = {
    publicSources: publicSources,
    displaySummary: displaySummary,
    pairTurns: pairTurns,
    caption: caption,
    cellText: cellText,
    resultCsv: resultCsv,
    formatElapsed: formatElapsed,
    formatTokens: formatTokens,
    statusLabel: statusLabel,
    stageLabel: stageLabel,
    overviewError: overviewError,
    groundingControl: groundingControl,
    messagePayload: messagePayload,
    overviewSummary: overviewSummary,
    schemaTables: schemaTables,
    knowledgeCards: knowledgeCards,
    renderOverview: renderOverview,
    renderGrounding: renderGrounding,
    retrievalPreviewPayload: retrievalPreviewPayload,
    createState: createState,
    mount: mount
  };
  root.SQLAgentStudio = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof document !== 'undefined' && document.getElementById && document.getElementById('app')) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { mount(document); });
    else mount(document);
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
