'use strict';
/* SQL Agent Studio client. Text is assigned with textContent. No mock query results. */
(function (root) {
  const count = new Intl.NumberFormat('zh-CN');
  const when = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const seconds = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const stageNames = {
    preparing: '正在准备查询',
    schema: '正在读取数据库结构',
    selector: '正在筛选相关字段',
    decomposer: '正在生成 SQL',
    execute: '正在检查并执行 SQL',
    refiner: '正在修复 SQL',
    requirements: '正在分析返回要求',
    semantic_review: '正在复核查询',
    evaluation: '正在独立评测',
    finalizing: '正在整理结果',
    finished: '正在整理结果'
  };

  function formatWhen(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : when.format(date);
  }
  function formatElapsed(value) {
    if (value == null || !Number.isFinite(Number(value))) return '未记录';
    return seconds.format(Number(value)) + ' 秒';
  }
  function formatTokens(value) {
    if (value == null) return '未记录';
    return count.format(value);
  }
  function stageLabel(stage) {
    return stageNames[stage] || '正在处理查询';
  }
  function statusLabel(status) {
    return {
      completed: '已完成',
      failed: '查询失败',
      running: '正在处理',
      stopping: '正在停止',
      stopped: '已停止',
      interrupted: '已中断'
    }[status] || '状态未记录';
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
    if (view.summary) return view.summary;
    const state = run && run.state;
    if (state === 'running') return stageLabel(run.stage);
    if (state === 'stopping') return '已收到停止请求，正在等待当前操作返回。';
    if (state === 'stopped') return '查询已停止。可以修改问题后再次发起。';
    if (state === 'interrupted') return '服务曾中断，本次查询未自动重跑。';
    if (state === 'failed') return view.error || '本次查询未完成。';
    return '此记录没有可确认的执行结果。';
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
        evaluated: view.evaluated === true
      });
    });
    return items;
  }
  function caption(turn) {
    if (!turn || turn.preview !== true) return '没有可展示的查询结果。';
    if (turn.complete === true) return '查询结果。下列是全部返回行。';
    return '结果预览。行数不是总数，也不能当作完整结果。';
  }
  function cellText(value) {
    if (value == null) return { text: 'NULL', nullish: true };
    if (typeof value === 'number' && Number.isFinite(value)) return { text: count.format(value), nullish: false };
    if (typeof value === 'boolean') return { text: value ? 'true' : 'false', nullish: false };
    return { text: String(value), nullish: false };
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
    article.dataset.messageId = item.id;
    if (item.role === 'user') {
      article.append(el(doc, 'p', 'prose', item.body));
      article.setAttribute('aria-label', '你的问题');
      return article;
    }
    article.setAttribute('aria-label', '回答');
    const meta = el(doc, 'div', 'meta');
    meta.append(el(doc, 'span', 'status-' + (item.state || ''), statusLabel(item.state)));
    meta.append(el(doc, 'span', null, item.complete ? '全部返回行' : (item.preview ? '结果预览' : '无结果表')));
    if (item.elapsed != null) meta.append(el(doc, 'span', null, formatElapsed(item.elapsed)));
    article.append(meta);
    const summary = el(doc, 'p', 'prose', item.body || '');
    if (item.state === 'failed') {
      const wrap = el(doc, 'div', 'failure');
      wrap.append(summary);
      article.append(wrap);
    } else {
      article.append(summary);
    }
    if (item.preview && item.columns.length && item.rows.length) {
      const wrap = el(doc, 'div', 'table-wrap');
      const table = el(doc, 'table');
      table.append(el(doc, 'caption', null, caption(item)));
      const headRow = el(doc, 'tr');
      item.columns.forEach(function (column) {
        const th = el(doc, 'th', null, column);
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
          line.append(el(doc, 'td', cell.nullish ? 'null' : null, cell.text));
        });
        tbody.append(line);
      });
      table.append(thead, tbody);
      wrap.append(table);
      article.append(wrap, el(doc, 'p', 'table-note', caption(item) + ' NULL 表示空值。'));
    } else if (item.preview) {
      article.append(el(doc, 'p', 'table-note', caption(item)));
    }
    if (item.sql) {
      const show = el(doc, 'button', 'text-action', '显示这条 SQL');
      show.type = 'button';
      show.addEventListener('click', function () { handlers.showSql(item.id); });
      article.append(show);
    }
    if (item.state === 'running') {
      const stop = el(doc, 'button', 'text-action', '停止');
      stop.type = 'button';
      stop.addEventListener('click', function () { handlers.stop(item.id); });
      article.append(stop);
    }
    return article;
  }

  function render(doc, state, nodes, handlers) {
    const source = sourceById(state.sources, state.datasourceId);
    const conversation = state.detail;
    doc.querySelector('.shell').classList.toggle('details-closed', !state.detailsOpen);
    nodes.details.hidden = !state.detailsOpen;
    nodes.toggle.textContent = state.detailsOpen ? '收起详情' : '展开 SQL 详情';
    nodes.toggle.setAttribute('aria-expanded', state.detailsOpen ? 'true' : 'false');
    nodes.title.textContent = conversation ? (conversation.conversation.title || '未命名对话') : '没有对话';
    nodes.subtitle.textContent = source ? source.name : '';
    doc.title = nodes.title.textContent + ' · SQL Agent Studio';
    nodes.banner.hidden = !state.notice;
    nodes.banner.textContent = state.notice || '';

    nodes.sourceList.replaceChildren();
    if (!state.sources.length) nodes.sourceList.append(el(doc, 'p', 'quiet', '没有已注册的数据源。'));
    state.sources.forEach(function (item) {
      const button = el(doc, 'button', 'source');
      button.type = 'button';
      button.setAttribute('aria-pressed', item.id === state.datasourceId ? 'true' : 'false');
      button.append(el(doc, 'strong', null, item.name), el(doc, 'small', null, item.read_only ? '只读 SQLite' : item.kind || ''));
      button.addEventListener('click', function () { handlers.pickSource(item.id); });
      nodes.sourceList.append(button);
    });

    const talks = talksFor(state);
    nodes.talkList.replaceChildren();
    if (!talks.length) nodes.talkList.append(el(doc, 'p', 'quiet', '这个数据源还没有对话。'));
    talks.forEach(function (item) {
      const button = el(doc, 'button', 'talk');
      button.type = 'button';
      if (conversation && item.id === conversation.conversation.id) button.setAttribute('aria-current', 'true');
      button.title = item.title;
      button.append(el(doc, 'strong', null, item.title), el(doc, 'small', null, formatWhen(item.updated_at)));
      button.addEventListener('click', function () { handlers.pickConversation(item.id); });
      nodes.talkList.append(button);
    });
    nodes.currentSource.textContent = source ? source.name : '未选择';
    nodes.currentMeta.textContent = source ? (source.read_only ? '只读 SQLite' : source.kind) : '';

    const turns = conversation ? pairTurns(conversation) : [];
    nodes.thread.replaceChildren();
    if (!state.workspace || !state.sources.length) {
      nodes.thread.append(el(doc, 'div', 'empty-thread', state.notice || '工作区还不能保存会话。'));
    } else if (!conversation) {
      const empty = el(doc, 'div', 'empty-thread');
      empty.append(
        el(doc, 'h2', null, '写下要问这个数据库的问题'),
        el(doc, 'p', 'prose', '先新建会话。问题和查询结果会保存在这一段对话里。')
      );
      nodes.thread.append(empty);
    } else if (!turns.length) {
      nodes.thread.append(el(doc, 'div', 'empty-thread', '这段对话还没有问题。'));
    } else {
      turns.forEach(function (item) { nodes.thread.append(renderMessage(doc, item, handlers)); });
    }
    nodes.sourceName.textContent = source ? source.name : '未选择';
    nodes.error.textContent = state.error || '';
    const busy = Boolean(state.sending || state.activeRunId);
    nodes.send.disabled = busy || !state.workspace || !state.sources.length;
    nodes.send.textContent = busy ? '处理中…' : '发送';
    const latest = turns.filter(function (item) { return item.role === 'assistant'; }).pop();
    nodes.live.textContent = latest ? statusLabel(latest.state) : '';
    renderDetails(doc, nodes, state, source, turns);
  }

  function renderDetails(doc, nodes, state, source, turns) {
    const selected = turns.find(function (item) { return item.role === 'assistant' && item.id === state.selectedRunId; })
      || turns.filter(function (item) { return item.role === 'assistant'; }).pop()
      || null;
    const question = selected ? previousUser(turns, selected.id) : '';
    nodes.detailSource.textContent = source ? source.name : '';
    nodes.detailQuestion.textContent = question || '选中一条回答后，这里显示对应的问题。';
    nodes.sql.textContent = selected && selected.sql ? selected.sql : '这一问没有 SQL。';
    nodes.runState.textContent = selected
      ? statusLabel(selected.state) + (selected.evaluated ? ' · 已评测' : ' · 未评测')
      : '没有选中的查询';
    nodes.tokenIn.textContent = formatTokens(selected && selected.tokens ? selected.tokens.input : null);
    nodes.tokenOut.textContent = formatTokens(selected && selected.tokens ? selected.tokens.output : null);
    nodes.duration.textContent = formatElapsed(selected ? selected.elapsed : null);
    nodes.usageNote.textContent = '没有记录时显示未记录。执行成功不等于评测通过。';
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

  function createState() {
    return {
      sources: [],
      conversations: [],
      detail: null,
      datasourceId: null,
      detailsOpen: true,
      selectedRunId: null,
      activeRunId: null,
      workspace: false,
      notice: '',
      error: '',
      sending: false
    };
  }

  function mount(doc, options) {
    const state = createState();
    const transport = options && options.transport;
    const app = doc.getElementById('app');
    const shell = el(doc, 'div', 'shell');
    const sidebar = el(doc, 'aside', 'sidebar');
    sidebar.setAttribute('aria-label', '工作区');
    const brand = el(doc, 'div', 'brand');
    brand.append(el(doc, 'p', 'brand-name', 'SQL Agent Studio'), el(doc, 'p', 'brand-note', '本地只读工作区'));
    const sourceLabel = el(doc, 'p', 'side-label', '数据源');
    const sourceList = el(doc, 'div', 'source-list');
    sourceList.setAttribute('role', 'group');
    sourceList.setAttribute('aria-label', '数据源');
    const newer = el(doc, 'button', 'new-chat', '新建会话');
    newer.type = 'button';
    const talkLabel = el(doc, 'p', 'side-label', '对话');
    const talkList = el(doc, 'nav', 'talk-list');
    talkList.setAttribute('aria-label', '对话');
    const foot = el(doc, 'div', 'sidebar-foot');
    foot.append(el(doc, 'p', 'quiet', '当前数据源'), el(doc, 'p'), el(doc, 'p', 'quiet'));
    const currentSource = foot.querySelector('p:nth-child(2)');
    const currentMeta = foot.querySelector('p:nth-child(3)');
    sidebar.append(brand, sourceLabel, sourceList, newer, talkLabel, talkList, foot);

    const chat = el(doc, 'main', 'chat');
    chat.id = 'conversation';
    chat.tabIndex = -1;
    const head = el(doc, 'div', 'chat-head');
    const heading = el(doc, 'div');
    const title = el(doc, 'h1');
    const subtitle = el(doc, 'p');
    heading.append(title, subtitle);
    const toggle = el(doc, 'button', 'detail-toggle', '收起详情');
    toggle.type = 'button';
    head.append(heading, toggle);
    const banner = el(doc, 'p', 'demo-banner');
    banner.hidden = true;
    const thread = el(doc, 'div', 'thread');
    const live = el(doc, 'div', 'visually-hidden');
    live.setAttribute('aria-live', 'polite');
    const form = el(doc, 'form', 'composer');
    const sourceLine = el(doc, 'p', 'composer-source');
    sourceLine.append(doc.createTextNode('数据源 '), el(doc, 'strong'));
    const sourceName = sourceLine.querySelector('strong');
    const box = el(doc, 'div', 'composer-box');
    const question = el(doc, 'textarea');
    question.id = 'question';
    question.name = 'question';
    question.maxLength = 4000;
    question.autocomplete = 'off';
    question.placeholder = '用自然语言描述要查询的内容';
    question.setAttribute('aria-label', '问题');
    const actions = el(doc, 'div', 'composer-actions');
    const send = el(doc, 'button', 'send', '发送');
    send.type = 'submit';
    actions.append(send);
    box.append(question, actions);
    const error = el(doc, 'p', 'form-error');
    error.setAttribute('role', 'alert');
    form.append(sourceLine, box, el(doc, 'p', 'hint', 'Ctrl + Enter 发送'), error);
    chat.append(head, banner, live, thread, form);

    const details = el(doc, 'aside', 'details');
    details.setAttribute('aria-label', '查询详情');
    const sourceBlock = el(doc, 'div', 'detail-block');
    sourceBlock.append(el(doc, 'h2', null, '数据源'), el(doc, 'p'));
    const questionBlock = el(doc, 'div', 'detail-block');
    questionBlock.append(el(doc, 'h2', null, '对应问题'), el(doc, 'p', 'prose'));
    const sqlBlock = el(doc, 'div', 'detail-block');
    const sql = el(doc, 'pre', 'sql');
    sql.translate = false;
    sqlBlock.append(el(doc, 'h2', null, 'SQL'), sql);
    const stateBlock = el(doc, 'div', 'detail-block');
    stateBlock.append(el(doc, 'h2', null, '执行状态'), el(doc, 'p'));
    const metricBlock = el(doc, 'div', 'detail-block');
    metricBlock.append(el(doc, 'h2', null, 'Token 和耗时'));
    const metrics = el(doc, 'div', 'metrics');
    function metric(label) {
      const wrap = el(doc, 'div');
      const strong = el(doc, 'strong', null, '未记录');
      wrap.append(el(doc, 'span', null, label), strong);
      metrics.append(wrap);
      return strong;
    }
    const tokenIn = metric('输入 token');
    const tokenOut = metric('输出 token');
    const duration = metric('耗时');
    const usageNote = el(doc, 'p', 'quiet');
    metricBlock.append(metrics, usageNote);
    const traceBlock = el(doc, 'div', 'detail-block');
    const traceDetails = el(doc, 'details');
    const trace = el(doc, 'ol', 'trace');
    traceDetails.append(el(doc, 'summary', null, '当前阶段'), trace);
    traceBlock.append(traceDetails);
    details.append(sourceBlock, questionBlock, sqlBlock, stateBlock, metricBlock, traceBlock);
    shell.append(sidebar, chat, details);
    app.replaceChildren(shell);

    const nodes = {
      details: details, toggle: toggle, title: title, subtitle: subtitle, banner: banner,
      sourceList: sourceList, talkList: talkList, currentSource: currentSource,
      currentMeta: currentMeta, thread: thread, sourceName: sourceName, error: error,
      send: send, live: live, detailSource: sourceBlock.querySelector('p'),
      detailQuestion: questionBlock.querySelector('p'), sql: sql,
      runState: stateBlock.querySelector('p'), tokenIn: tokenIn, tokenOut: tokenOut,
      duration: duration, usageNote: usageNote, trace: trace
    };

    function paint() { render(doc, state, nodes, handlers); }

    function showError(text) {
      state.error = text || '';
      nodes.error.textContent = state.error;
    }

    async function request(path, payload) {
      if (transport) return transport(path, payload);
      const response = await fetch(path, {
        method: payload === undefined ? 'GET' : 'POST',
        headers: payload === undefined ? {} : { 'Content-Type': 'application/json' },
        body: payload === undefined ? undefined : JSON.stringify(payload)
      });
      let data = {};
      try { data = await response.json(); } catch (error) { data = {}; }
      if (!response.ok) {
        const failure = new Error(data.error || '请求未完成');
        failure.status = response.status;
        throw failure;
      }
      return data;
    }

    function applyDetail(detail) {
      state.detail = detail;
      state.datasourceId = detail.conversation.datasource_id;
      const turns = pairTurns(detail);
      const assistants = turns.filter(function (item) { return item.role === 'assistant'; });
      const still = assistants.some(function (item) { return item.id === state.selectedRunId; });
      if (!still) state.selectedRunId = assistants.length ? assistants[assistants.length - 1].id : null;
      const active = assistants.find(function (item) {
        return item.state === 'running' || item.state === 'stopping';
      });
      state.activeRunId = active ? active.id : null;
      if (typeof history !== 'undefined' && history.replaceState) {
        const params = new URLSearchParams();
        params.set('datasource', state.datasourceId);
        params.set('conversation', detail.conversation.id);
        history.replaceState(null, '', '/workspace/?' + params.toString());
      }
    }

    async function openConversation(id) {
      const detail = await request('/api/conversations/' + encodeURIComponent(id));
      applyDetail(detail);
      paint();
      return detail;
    }

    async function refreshList() {
      const listed = await request('/api/conversations');
      state.conversations = listed.conversations || [];
      if (!state.activeRunId) state.activeRunId = listed.active_run_id || null;
    }

    async function boot() {
      try {
        const catalog = await request('/api/catalog');
        state.sources = publicSources(catalog);
        try {
          await refreshList();
          state.workspace = true;
        } catch (error) {
          state.workspace = false;
          state.conversations = [];
          if (error.status !== 404) throw error;
        }
        if (!state.sources.length) {
          state.notice = '尚未接入数据源。请在启动命令里用 --sources 注册只读 SQLite，然后刷新此页。';
        } else if (!state.workspace) {
          state.notice = '未启用 Workspace。请带 --workspace-db 重新启动后刷新。当前不会把演示数据当成查询结果。';
        } else {
          state.notice = '';
          const params = new URLSearchParams(options && options.search != null ? options.search : (typeof location !== 'undefined' ? location.search : ''));
          const requestedSource = params.get('datasource');
          if (sourceById(state.sources, requestedSource)) state.datasourceId = requestedSource;
          else state.datasourceId = state.sources[0].id;
          const requested = params.get('conversation');
          const known = state.conversations.some(function (item) { return item.id === requested; });
          const first = talksFor(state)[0];
          if (known) await openConversation(requested);
          else if (first) await openConversation(first.id);
        }
      } catch (error) {
        state.notice = '无法连接本地服务。确认服务已启动后刷新此页。';
        showError(error.message);
      }
      paint();
    }

    const handlers = {
      showSql: function (id) {
        state.selectedRunId = id;
        state.detailsOpen = true;
        paint();
      },
      stop: function (id) {
        request('/api/stop', { id: id }).then(function () {
          return openConversation(state.detail.conversation.id);
        }).catch(function (error) { showError(error.message); paint(); });
      },
      pickSource: function (id) {
        if (state.activeRunId) {
          showError('等这次查询结束，或先停止它。');
          paint();
          return;
        }
        if (!sourceById(state.sources, id)) return;
        state.datasourceId = id;
        state.detail = null;
        state.selectedRunId = null;
        state.error = '';
        const first = talksFor(state)[0];
        if (first) openConversation(first.id).catch(function (error) { showError(error.message); paint(); });
        else paint();
      },
      pickConversation: function (id) {
        if (state.activeRunId && (!state.detail || state.detail.conversation.id !== id)) {
          showError('等这次查询结束，或先停止它。');
          paint();
          return;
        }
        openConversation(id).catch(function (error) { showError(error.message); paint(); });
      }
    };

    newer.addEventListener('click', function () {
      if (!state.workspace || !state.datasourceId) {
        showError(state.notice || '先选择数据源。');
        paint();
        return;
      }
      if (state.activeRunId) {
        showError('等这次查询结束，或先停止它。');
        paint();
        return;
      }
      request('/api/conversations', { datasource_id: state.datasourceId }).then(function (created) {
        state.error = '';
        return refreshList().then(function () { return openConversation(created.id); });
      }).catch(function (error) { showError(error.message); paint(); });
    });
    toggle.addEventListener('click', function () {
      state.detailsOpen = !state.detailsOpen;
      paint();
    });
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      const text = question.value.trim();
      if (!text || state.sending || state.activeRunId || !state.detail) {
        if (!state.detail) showError('先新建会话。');
        paint();
        return;
      }
      state.sending = true;
      state.error = '';
      paint();
      request('/api/conversations/' + encodeURIComponent(state.detail.conversation.id) + '/messages', { question: text })
        .then(function (started) {
          question.value = '';
          state.activeRunId = started.id;
          state.selectedRunId = started.id;
          return openConversation(state.detail.conversation.id);
        })
        .catch(function (error) { showError(error.message); })
        .finally(function () { state.sending = false; paint(); });
    });
    question.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
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
    formatElapsed: formatElapsed,
    formatTokens: formatTokens,
    statusLabel: statusLabel,
    stageLabel: stageLabel,
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
