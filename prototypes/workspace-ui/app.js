'use strict';
/* Static mock for SQL Agent Studio. No network and no model calls. Text is assigned with textContent. */
(function (root) {
  const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const count = new Intl.NumberFormat('zh-CN');
  const when = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const seconds = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 1, maximumFractionDigits: 1 });

  const SOURCES = [
    {
      id: 'demo-retail',
      name: '演示零售账本',
      kind: 'sqlite',
      read_only: true,
      summary: '虚构的门店收款账，用来演示对话和 SQL。',
      tables: [
        ['stores', '门店'],
        ['cities', '城市'],
        ['cashiers', '收银员'],
        ['shifts', '班次'],
        ['products', '商品'],
        ['orders', '订单'],
        ['payments', '收款'],
        ['refunds', '退款']
      ],
      domains: '可以讨论门店收款、退款、城市差异和班次。没有真实顾客或交易。'
    },
    {
      id: 'demo-members',
      name: '演示会员档案',
      kind: 'sqlite',
      read_only: true,
      summary: '另一份虚构档案，用来确认切换数据源后对话会换一组。',
      tables: [
        ['members', '会员'],
        ['tiers', '等级'],
        ['points_ledger', '积分流水']
      ],
      domains: '可以讨论会员等级和积分门槛。积分数字同样是演示值。'
    }
  ];

  function sourceById(id) {
    return SOURCES.find(function (source) { return source.id === id; });
  }

  function formatMoney(value) { return money.format(value); }
  function formatCount(value) { return count.format(value); }
  function formatWhen(iso) { return when.format(new Date(iso)); }
  function formatDuration(ms) {
    if (ms == null) return '未记录';
    return seconds.format(ms / 1000) + ' 秒';
  }
  function formatTokens(value) {
    if (value == null) return '未记录';
    return count.format(value);
  }

  function profileReply(source) {
    return {
      kind: 'profile',
      status: 'completed',
      demo: true,
      summary: source.summary,
      domains: source.domains,
      tables: source.tables,
      suggestions: source.id === 'demo-retail'
        ? ['这个数据库包含什么？', '2026 年 10 月净收款额是多少？', '按城市拆分呢？']
        : ['这个数据库包含什么？', '会员等级有哪些？'],
      sql: null,
      columns: null,
      rows: null,
      ledger: null,
      durationMs: 1200,
      tokens: { input: 640, output: 180 },
      trace: [
        { role: 'Selector', state: '已完成', detail: '这一问要的是库简介，没有挑选查询字段。' },
        { role: 'Decomposer', state: '未生成', detail: '没有写 SQL。' },
        { role: 'Refiner', state: '未运行', detail: '没有 SQL 需要检查。' }
      ]
    };
  }

  function totalReply() {
    return {
      kind: 'result',
      status: 'completed',
      demo: true,
      summary: '2026 年 10 月净收款是 1,284,600.00 元。这是预先写好的演示结果，不是查询返回值。',
      sql: '-- 演示 SQL，未连接数据库\nSELECT ROUND(SUM(receipt_amount - refund_amount), 2) AS net_receipts\nFROM payments\nWHERE paid_on >= \'2026-10-01\'\n  AND paid_on < \'2026-11-01\';',
      columns: [
        { key: 'metric', label: '指标', type: 'text' },
        { key: 'amount', label: '净收款额', type: 'money' }
      ],
      rows: [{ metric: '2026 年 10 月净收款', amount: 1284600 }],
      ledger: { label: '净收款（元）', value: 1284600 },
      durationMs: 4800,
      tokens: { input: 1842, output: 306 },
      trace: [
        { role: 'Selector', state: '已完成', detail: '保留 payments.receipt_amount、payments.refund_amount、payments.paid_on。' },
        { role: 'Decomposer', state: '已完成', detail: '写成按 2026 年 10 月汇总的只读查询。' },
        { role: 'Refiner', state: '未修改', detail: '没有发现需要修复的问题。' }
      ]
    };
  }

  function cityReply() {
    return {
      kind: 'result',
      status: 'completed',
      demo: true,
      summary: '沿用演示对话里的 2026 年 10 月净收款，按城市拆开。四行相加仍是 1,284,600.00 元，都是写好的演示数字。',
      sql: '-- 演示 SQL，未连接数据库\nSELECT city_name,\n       ROUND(SUM(receipt_amount - refund_amount), 2) AS net_receipts\nFROM payments\nJOIN stores ON stores.id = payments.store_id\nJOIN cities ON cities.id = stores.city_id\nWHERE paid_on >= \'2026-10-01\'\n  AND paid_on < \'2026-11-01\'\nGROUP BY city_name\nORDER BY net_receipts DESC;',
      columns: [
        { key: 'city', label: '城市', type: 'text' },
        { key: 'amount', label: '净收款（元）', type: 'money' },
        { key: 'note', label: '备注', type: 'text' }
      ],
      rows: [
        { city: '杭州', amount: 420000, note: null },
        { city: '成都', amount: 310200, note: '' },
        { city: '深圳', amount: 289400, note: '含一笔演示退款' },
        { city: '西安', amount: 265000, note: null }
      ],
      ledger: null,
      durationMs: 3600,
      tokens: { input: 2104, output: 428 },
      trace: [
        { role: 'Selector', state: '已完成', detail: '沿用 payments，并补上 stores 与 cities。' },
        { role: 'Decomposer', state: '已完成', detail: '在上一问的时间范围上按城市分组。' },
        { role: 'Refiner', state: '未修改', detail: '分组查询仍是只读语句。' }
      ]
    };
  }

  function tiersReply() {
    return {
      kind: 'result',
      status: 'completed',
      demo: true,
      summary: '演示会员档案里有 3 个等级。门槛是界面样例，不是会员库的输出。',
      sql: '-- 演示 SQL，未连接数据库\nSELECT tier_name, min_points\nFROM tiers\nORDER BY min_points;',
      columns: [
        { key: 'tier', label: '等级', type: 'text' },
        { key: 'points', label: '积分门槛', type: 'count' }
      ],
      rows: [
        { tier: '普通', points: 0 },
        { tier: '银卡', points: 1000 },
        { tier: '金卡', points: 5000 }
      ],
      ledger: null,
      durationMs: 2100,
      tokens: { input: 880, output: 140 },
      trace: [
        { role: 'Selector', state: '已完成', detail: '选用 tiers.tier_name 和 tiers.min_points。' },
        { role: 'Decomposer', state: '已完成', detail: '按积分门槛升序读取等级。' },
        { role: 'Refiner', state: '未修改', detail: '语句只包含 SELECT。' }
      ]
    };
  }

  function blockedReply() {
    return {
      kind: 'failure',
      status: 'failed',
      demo: true,
      summary: '只读工作区不会执行删除。把问题改成查询后再发送，例如询问 10 月的净收款。',
      sql: null,
      columns: null,
      rows: null,
      ledger: null,
      durationMs: null,
      tokens: { input: null, output: null },
      trace: [
        { role: 'Selector', state: '未运行', detail: '请求在执行前被拦住。' },
        { role: 'Decomposer', state: '未生成', detail: '没有为删除动作写 SQL。' },
        { role: 'Refiner', state: '未运行', detail: '没有候选语句。' }
      ]
    };
  }

  function limitReply() {
    return {
      kind: 'failure',
      status: 'failed',
      demo: true,
      summary: '演示原型没有连接数据库，所以没有执行这个问题。可以改问“这个数据库包含什么？”，或从左侧打开已有对话。',
      sql: null,
      columns: null,
      rows: null,
      ledger: null,
      durationMs: null,
      tokens: { input: null, output: null },
      trace: [
        { role: 'Selector', state: '未运行', detail: '演示模式只回答准备好的问题。' },
        { role: 'Decomposer', state: '未生成', detail: '没有调用模型。' },
        { role: 'Refiner', state: '未运行', detail: '没有 SQL。' }
      ]
    };
  }

  function replyFor(question, datasourceId) {
    const text = String(question || '').trim();
    const source = sourceById(datasourceId) || SOURCES[0];
    if (/包含什么|有哪些表|数据库简介/.test(text)) return profileReply(source);
    if (/删|drop\b|delete\b/i.test(text)) return blockedReply();
    if (source.id === 'demo-members') {
      if (/等级/.test(text)) return tiersReply();
      return limitReply();
    }
    if (/城市/.test(text)) return cityReply();
    if (/净收款|10\s*月|十月/.test(text)) return totalReply();
    return limitReply();
  }

  function message(id, role, body, createdAt, turn) {
    return { id: id, role: role, body: body, createdAt: createdAt, turn: turn || null };
  }

  function seedConversations() {
    const retailProfile = profileReply(sourceById('demo-retail'));
    const membersProfile = profileReply(sourceById('demo-members'));
    return [
      {
        id: 'retail-october',
        datasourceId: 'demo-retail',
        title: '10 月净收款',
        createdAt: '2026-11-16T13:04:00.000Z',
        updatedAt: '2026-11-16T13:11:00.000Z',
        messages: [
          message('u-profile', 'user', '这个数据库包含什么？', '2026-11-16T13:04:00.000Z'),
          message('a-profile', 'assistant', retailProfile.summary, '2026-11-16T13:04:08.000Z', retailProfile),
          message('u-total', 'user', '2026 年 10 月净收款额是多少？', '2026-11-16T13:08:00.000Z'),
          message('a-total', 'assistant', totalReply().summary, '2026-11-16T13:08:12.000Z', totalReply()),
          message('u-city', 'user', '按城市拆分呢？', '2026-11-16T13:10:00.000Z'),
          message('a-city', 'assistant', cityReply().summary, '2026-11-16T13:11:00.000Z', cityReply())
        ]
      },
      {
        id: 'retail-blocked',
        datasourceId: 'demo-retail',
        title: '删除请求被拒绝',
        createdAt: '2026-11-16T14:02:00.000Z',
        updatedAt: '2026-11-16T14:02:06.000Z',
        messages: [
          message('u-delete', 'user', '把 10 月的订单都删掉', '2026-11-16T14:02:00.000Z'),
          message('a-delete', 'assistant', blockedReply().summary, '2026-11-16T14:02:06.000Z', blockedReply())
        ]
      },
      {
        id: 'retail-escape',
        datasourceId: 'demo-retail',
        title: '文本按原样显示',
        createdAt: '2026-11-16T15:20:00.000Z',
        updatedAt: '2026-11-16T15:20:04.000Z',
        messages: [
          message('u-html', 'user', '<img src=x onerror=alert(1)>', '2026-11-16T15:20:00.000Z'),
          message('a-html', 'assistant', '上面的内容按文本显示。演示页面不会把它当成 HTML。', '2026-11-16T15:20:04.000Z', {
            kind: 'note',
            status: 'completed',
            demo: true,
            summary: '上面的内容按文本显示。演示页面不会把它当成 HTML。',
            sql: null,
            columns: null,
            rows: null,
            ledger: null,
            durationMs: 200,
            tokens: { input: 20, output: 24 },
            trace: [
              { role: 'Selector', state: '未运行', detail: '这句话不是数据问题。' },
              { role: 'Decomposer', state: '未生成', detail: '没有写 SQL。' },
              { role: 'Refiner', state: '未运行', detail: '没有 SQL。' }
            ]
          })
        ]
      },
      {
        id: 'members-tiers',
        datasourceId: 'demo-members',
        title: '会员等级',
        createdAt: '2026-11-15T09:12:00.000Z',
        updatedAt: '2026-11-15T09:13:00.000Z',
        messages: [
          message('u-tier', 'user', '会员等级有哪些？', '2026-11-15T09:12:00.000Z'),
          message('a-tier', 'assistant', tiersReply().summary, '2026-11-15T09:13:00.000Z', tiersReply())
        ]
      },
      {
        id: 'members-profile',
        datasourceId: 'demo-members',
        title: '会员库里有什么',
        createdAt: '2026-11-15T09:00:00.000Z',
        updatedAt: '2026-11-15T09:01:00.000Z',
        messages: [
          message('u-member-profile', 'user', '这个数据库包含什么？', '2026-11-15T09:00:00.000Z'),
          message('a-member-profile', 'assistant', membersProfile.summary, '2026-11-15T09:01:00.000Z', membersProfile)
        ]
      }
    ];
  }

  function createState() {
    const conversations = seedConversations();
    return {
      datasourceId: 'demo-retail',
      conversationId: 'retail-october',
      detailsOpen: true,
      traceOpen: true,
      selectedMessageId: 'a-city',
      conversations: conversations,
      error: '',
      pendingId: null
    };
  }

  function activeConversation(state) {
    return state.conversations.find(function (item) {
      return item.id === state.conversationId;
    }) || null;
  }

  function conversationsForSource(state) {
    return state.conversations.filter(function (item) {
      return item.datasourceId === state.datasourceId;
    });
  }

  function statusLabel(status) {
    return {
      completed: '已完成',
      failed: '未执行',
      running: '正在生成 SQL…',
      stopped: '已停止'
    }[status] || '状态未记录';
  }

  function appendQuestion(state, question, ids) {
    const text = String(question || '').trim();
    const conversation = activeConversation(state);
    if (!conversation) return { ok: false, error: '先选择一段对话。' };
    if (!text) return { ok: false, error: '先写一个问题。' };
    if (state.pendingId) return { ok: false, error: '等这次查询结束，或先停止它。' };
    const stamp = new Date().toISOString();
    const nonce = Date.now().toString(36);
    const userId = ids && ids.userId ? ids.userId : 'u-' + nonce;
    const pendingId = ids && ids.pendingId ? ids.pendingId : 'pending-' + nonce;
    conversation.messages.push(message(userId, 'user', text, stamp));
    conversation.messages.push(message(pendingId, 'assistant', '正在生成 SQL…', stamp, {
      kind: 'pending',
      status: 'running',
      demo: true,
      summary: '正在生成 SQL…',
      sql: null,
      columns: null,
      rows: null,
      ledger: null,
      durationMs: null,
      tokens: { input: null, output: null },
      trace: [
        { role: 'Selector', state: '进行中', detail: '正在对照这个数据源的表。' },
        { role: 'Decomposer', state: '等待', detail: '还没有 SQL。' },
        { role: 'Refiner', state: '等待', detail: '还没有 SQL。' }
      ]
    }));
    if (conversation.title === '未命名对话') {
      conversation.title = text.length > 28 ? text.slice(0, 28) + '…' : text;
    }
    conversation.updatedAt = stamp;
    state.pendingId = pendingId;
    state.selectedMessageId = pendingId;
    state.error = '';
    return { ok: true, pendingId: pendingId, userId: userId };
  }

  function completePending(state, pendingId) {
    if (state.pendingId !== pendingId) return false;
    const conversation = activeConversation(state);
    const index = conversation.messages.findIndex(function (item) { return item.id === pendingId; });
    if (index < 1) return false;
    const question = conversation.messages[index - 1].body;
    const turn = replyFor(question, conversation.datasourceId);
    const stamp = new Date().toISOString();
    conversation.messages[index] = message(pendingId, 'assistant', turn.summary, stamp, turn);
    conversation.updatedAt = stamp;
    state.pendingId = null;
    state.selectedMessageId = pendingId;
    return true;
  }

  function stopPending(state) {
    if (!state.pendingId) return false;
    const conversation = activeConversation(state);
    const pending = conversation.messages.find(function (item) { return item.id === state.pendingId; });
    if (!pending) return false;
    pending.body = '已停止。这次没有执行查询。';
    pending.turn = {
      kind: 'stopped',
      status: 'stopped',
      demo: true,
      summary: pending.body,
      sql: null,
      columns: null,
      rows: null,
      ledger: null,
      durationMs: null,
      tokens: { input: null, output: null },
      trace: [
        { role: 'Selector', state: '已停止', detail: '停止发生在演示等待期间。' },
        { role: 'Decomposer', state: '未生成', detail: '没有写 SQL。' },
        { role: 'Refiner', state: '未运行', detail: '没有 SQL。' }
      ]
    };
    state.pendingId = null;
    return true;
  }

  function selectSource(state, datasourceId) {
    if (state.pendingId) return { ok: false, error: '等这次查询结束，或先停止它。' };
    if (!sourceById(datasourceId)) return { ok: false, error: '没有这个数据源。' };
    state.datasourceId = datasourceId;
    const list = conversationsForSource(state);
    state.conversationId = list.length ? list[0].id : null;
    state.selectedMessageId = latestAssistantId(activeConversation(state));
    state.error = '';
    return { ok: true };
  }

  function selectConversation(state, conversationId) {
    if (state.pendingId && conversationId !== state.conversationId) {
      return { ok: false, error: '等这次查询结束，或先停止它。' };
    }
    const conversation = state.conversations.find(function (item) { return item.id === conversationId; });
    if (!conversation || conversation.datasourceId !== state.datasourceId) {
      return { ok: false, error: '这段对话不在当前数据源里。' };
    }
    state.conversationId = conversationId;
    state.selectedMessageId = latestAssistantId(conversation);
    state.error = '';
    return { ok: true };
  }

  function createConversation(state, id) {
    if (state.pendingId) return { ok: false, error: '等这次查询结束，或先停止它。' };
    const stamp = new Date().toISOString();
    const conversation = {
      id: id || 'local-' + stamp,
      datasourceId: state.datasourceId,
      title: '未命名对话',
      createdAt: stamp,
      updatedAt: stamp,
      messages: []
    };
    state.conversations.unshift(conversation);
    state.conversationId = conversation.id;
    state.selectedMessageId = null;
    state.error = '';
    return { ok: true, id: conversation.id };
  }

  function latestAssistantId(conversation) {
    if (!conversation) return null;
    for (let i = conversation.messages.length - 1; i >= 0; i -= 1) {
      if (conversation.messages[i].role === 'assistant') return conversation.messages[i].id;
    }
    return null;
  }

  function selectedMessage(state) {
    const conversation = activeConversation(state);
    if (!conversation) return null;
    return conversation.messages.find(function (item) { return item.id === state.selectedMessageId; }) || null;
  }

  function cellText(column, value) {
    if (value == null) return { text: 'NULL', nullish: true };
    if (column.type === 'money') return { text: formatMoney(value), nullish: false };
    if (column.type === 'count') return { text: formatCount(value), nullish: false };
    return { text: String(value), nullish: false };
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
      const body = el(doc, 'p', 'prose', item.body);
      article.append(body);
      article.setAttribute('aria-label', '你的问题');
      return article;
    }
    const turn = item.turn || {};
    article.setAttribute('aria-label', '回答');
    if (turn.demo) article.dataset.demo = 'true';
    const meta = el(doc, 'div', 'meta');
    meta.append(el(doc, 'span', 'status-' + (turn.status || ''), statusLabel(turn.status)));
    meta.append(el(doc, 'span', null, '演示数据'));
    if (turn.durationMs != null) meta.append(el(doc, 'span', null, formatDuration(turn.durationMs)));
    article.append(meta);
    if (turn.ledger) {
      const figure = el(doc, 'p', 'ledger');
      figure.append(el(doc, 'small', null, turn.ledger.label));
      figure.append(doc.createTextNode(formatMoney(turn.ledger.value)));
      article.append(figure);
    }
    const summary = el(doc, 'p', 'prose', turn.summary || item.body);
    if (turn.kind === 'failure') {
      const wrap = el(doc, 'div', 'failure');
      wrap.append(summary);
      article.append(wrap);
    } else {
      article.append(summary);
    }
    if (turn.kind === 'profile') {
      const countLine = el(doc, 'p', 'prose', '账本里有 ' + formatCount(turn.tables.length) + ' 张表。');
      article.append(countLine);
      const list = el(doc, 'dl', 'profile-list');
      turn.tables.forEach(function (pair) {
        const row = el(doc, 'div');
        const name = el(doc, 'dt', null, pair[0]);
        name.translate = false;
        row.append(name, el(doc, 'dd', null, pair[1]));
        list.append(row);
      });
      article.append(list, el(doc, 'p', 'prose', turn.domains));
      if (turn.suggestions && turn.suggestions.length) {
        const group = el(doc, 'div', 'suggestions');
        group.setAttribute('role', 'group');
        group.setAttribute('aria-label', '推荐问题');
        turn.suggestions.forEach(function (suggestion) {
          const button = el(doc, 'button', 'suggestion', suggestion);
          button.type = 'button';
          button.addEventListener('click', function () { handlers.ask(suggestion); });
          group.append(button);
        });
        article.append(group);
      }
    }
    if (turn.columns && turn.rows) {
      const wrap = el(doc, 'div', 'table-wrap');
      const table = el(doc, 'table');
      const caption = el(doc, 'caption', null, '演示结果，不是数据库返回的记录');
      const thead = el(doc, 'thead');
      const headRow = el(doc, 'tr');
      turn.columns.forEach(function (column) {
        const th = el(doc, 'th', null, column.label);
        th.scope = 'col';
        headRow.append(th);
      });
      thead.append(headRow);
      const tbody = el(doc, 'tbody');
      turn.rows.forEach(function (row) {
        const line = el(doc, 'tr');
        turn.columns.forEach(function (column) {
          const cell = cellText(column, row[column.key]);
          line.append(el(doc, 'td', cell.nullish ? 'null' : null, cell.text));
        });
        tbody.append(line);
      });
      table.append(caption, thead, tbody);
      wrap.append(table);
      article.append(wrap, el(doc, 'p', 'table-note', 'NULL 表示空值。空白单元格是空字符串。这些行是全部演示行。'));
    }
    if (turn.sql) {
      const show = el(doc, 'button', 'text-action', '显示这条 SQL');
      show.type = 'button';
      show.addEventListener('click', function () { handlers.showSql(item.id); });
      article.append(show);
    }
    if (turn.status === 'running') {
      const stop = el(doc, 'button', 'text-action', '停止');
      stop.type = 'button';
      stop.addEventListener('click', function () { handlers.stop(); });
      article.append(stop);
    }
    return article;
  }

  function render(doc, state, nodes, handlers) {
    const source = sourceById(state.datasourceId);
    const conversation = activeConversation(state);
    doc.querySelector('.shell').classList.toggle('details-closed', !state.detailsOpen);
    nodes.details.hidden = !state.detailsOpen;
    nodes.toggle.textContent = state.detailsOpen ? '收起详情' : '展开 SQL 详情';
    nodes.toggle.setAttribute('aria-expanded', state.detailsOpen ? 'true' : 'false');
    nodes.title.textContent = conversation ? conversation.title : '没有对话';
    nodes.subtitle.textContent = source ? source.name : '';
    doc.title = (conversation ? conversation.title : 'SQL Agent Studio') + ' · SQL Agent Studio';

    nodes.sourceList.replaceChildren();
    SOURCES.forEach(function (item) {
      const button = el(doc, 'button', 'source');
      button.type = 'button';
      button.setAttribute('aria-pressed', item.id === state.datasourceId ? 'true' : 'false');
      button.append(el(doc, 'strong', null, item.name), el(doc, 'small', null, formatCount(item.tables.length) + ' 张表'));
      button.addEventListener('click', function () { handlers.pickSource(item.id); });
      nodes.sourceList.append(button);
    });

    const talks = conversationsForSource(state);
    nodes.talkList.replaceChildren();
    if (!talks.length) nodes.talkList.append(el(doc, 'p', 'quiet', '这个数据源还没有对话。'));
    talks.forEach(function (item) {
      const button = el(doc, 'button', 'talk');
      button.type = 'button';
      if (item.id === state.conversationId) button.setAttribute('aria-current', 'true');
      button.title = item.title;
      button.append(el(doc, 'strong', null, item.title), el(doc, 'small', null, formatWhen(item.updatedAt)));
      button.addEventListener('click', function () { handlers.pickConversation(item.id); });
      nodes.talkList.append(button);
    });
    nodes.currentSource.textContent = source ? source.name : '';
    nodes.currentMeta.textContent = source ? '只读 SQLite' : '';

    nodes.thread.replaceChildren();
    if (!conversation || !conversation.messages.length) {
      const empty = el(doc, 'div', 'empty-thread');
      empty.append(
        el(doc, 'h2', null, '写下要问这个数据库的问题'),
        el(doc, 'p', 'prose', '回答、SQL 和表格都会留在这段对话里。下面的问题可以直接发送。')
      );
      const group = el(doc, 'div', 'suggestions');
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', '推荐问题');
      (source && source.id === 'demo-members'
        ? ['这个数据库包含什么？', '会员等级有哪些？']
        : ['这个数据库包含什么？', '2026 年 10 月净收款额是多少？', '按城市拆分呢？']
      ).forEach(function (suggestion) {
        const button = el(doc, 'button', 'suggestion', suggestion);
        button.type = 'button';
        button.addEventListener('click', function () { handlers.ask(suggestion); });
        group.append(button);
      });
      empty.append(group);
      nodes.thread.append(empty);
    } else {
      conversation.messages.forEach(function (item) {
        nodes.thread.append(renderMessage(doc, item, handlers));
      });
    }
    nodes.sourceName.textContent = source ? source.name : '';
    nodes.error.textContent = state.error || '';
    nodes.send.disabled = Boolean(state.pendingId);
    nodes.send.textContent = state.pendingId ? '处理中…' : '发送';
    const latest = conversation && conversation.messages.filter(function (item) { return item.role === 'assistant'; }).pop();
    nodes.live.textContent = latest && latest.turn ? statusLabel(latest.turn.status) : '';
    renderDetails(doc, nodes, state, source, conversation);
  }

  function renderDetails(doc, nodes, state, source, conversation) {
    const selected = selectedMessage(state);
    const turn = selected && selected.turn;
    const question = conversation && selected
      ? previousUser(conversation, selected.id)
      : '';
    nodes.detailSource.textContent = source ? source.name : '';
    nodes.detailQuestion.textContent = question || '选中一条回答后，这里显示对应的问题。';
    nodes.sql.textContent = turn && turn.sql ? turn.sql : '这一问没有 SQL。';
    nodes.sql.hidden = false;
    nodes.runState.textContent = turn ? statusLabel(turn.status) : '没有选中的查询';
    nodes.tokenIn.textContent = formatTokens(turn && turn.tokens ? turn.tokens.input : null);
    nodes.tokenOut.textContent = formatTokens(turn && turn.tokens ? turn.tokens.output : null);
    nodes.duration.textContent = formatDuration(turn ? turn.durationMs : null);
    nodes.usageNote.textContent = turn && turn.tokens && turn.tokens.input != null ? 'Token 和耗时是演示记录。' : '没有记录时显示未记录。';
    nodes.trace.replaceChildren();
    (turn && turn.trace ? turn.trace : []).forEach(function (step) {
      const li = el(doc, 'li');
      const head = el(doc, 'div', 'trace-head');
      const role = el(doc, 'span', 'role', step.role);
      role.translate = false;
      head.append(role, el(doc, 'span', null, step.state));
      li.append(head, el(doc, 'p', 'quiet', step.detail));
      nodes.trace.append(li);
    });
    nodes.traceDetails.open = state.traceOpen;
  }

  function previousUser(conversation, assistantId) {
    const index = conversation.messages.findIndex(function (item) { return item.id === assistantId; });
    if (index <= 0) return '';
    const previous = conversation.messages[index - 1];
    return previous && previous.role === 'user' ? previous.body : '';
  }

  function syncUrl(state) {
    if (typeof history === 'undefined' || !history.replaceState || typeof location === 'undefined') return;
    const params = new URLSearchParams(location.search);
    params.set('datasource', state.datasourceId);
    if (state.conversationId) params.set('conversation', state.conversationId);
    else params.delete('conversation');
    params.set('details', state.detailsOpen ? 'open' : 'closed');
    if (state.selectedMessageId) params.set('message', state.selectedMessageId);
    else params.delete('message');
    const next = location.pathname + '?' + params.toString();
    history.replaceState(null, '', next);
  }

  function applyLocation(state, search) {
    const params = new URLSearchParams(search || '');
    const datasourceId = params.get('datasource');
    if (sourceById(datasourceId)) state.datasourceId = datasourceId;
    const listed = conversationsForSource(state);
    const requested = params.get('conversation');
    if (listed.some(function (item) { return item.id === requested; })) state.conversationId = requested;
    else state.conversationId = listed.length ? listed[0].id : null;
    if (params.get('details') === 'closed') state.detailsOpen = false;
    const selected = params.get('message');
    const conversation = activeConversation(state);
    if (conversation && conversation.messages.some(function (item) { return item.id === selected; })) {
      state.selectedMessageId = selected;
    } else {
      state.selectedMessageId = latestAssistantId(conversation);
    }
  }

  function mount(doc, options) {
    const state = createState();
    applyLocation(state, options && options.search != null ? options.search : (typeof location !== 'undefined' ? location.search : ''));
    const app = doc.getElementById('app');
    const shell = el(doc, 'div', 'shell');
    const sidebar = el(doc, 'aside', 'sidebar');
    sidebar.setAttribute('aria-label', '工作区');
    const brand = el(doc, 'div', 'brand');
    brand.append(el(doc, 'p', 'brand-name', 'SQL Agent Studio'), el(doc, 'p', 'brand-note', '本地演示工作区'));
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
    const footLabel = el(doc, 'p', 'quiet', '当前数据源');
    const currentSource = el(doc, 'p');
    const currentMeta = el(doc, 'p', 'quiet');
    foot.append(footLabel, currentSource, currentMeta);
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
    const banner = el(doc, 'p', 'demo-banner', '演示数据。表、SQL 和数字都是写好的样例，不是数据库输出。');
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
    question.required = false;
    question.maxLength = 4000;
    question.autocomplete = 'off';
    question.placeholder = '例如：2026 年 10 月的净收款…';
    question.setAttribute('aria-label', '问题');
    const actions = el(doc, 'div', 'composer-actions');
    const send = el(doc, 'button', 'send', '发送');
    send.type = 'submit';
    const hint = el(doc, 'p', 'hint', 'Ctrl + Enter 发送');
    actions.append(send);
    box.append(question, actions);
    const error = el(doc, 'p', 'form-error');
    error.setAttribute('role', 'alert');
    form.append(sourceLine, box, hint, error);
    chat.append(head, banner, live, thread, form);

    const details = el(doc, 'aside', 'details');
    details.setAttribute('aria-label', '查询详情');
    const sourceBlock = el(doc, 'div', 'detail-block');
    sourceBlock.append(el(doc, 'h2', null, '数据源'), el(doc, 'p'));
    const detailSource = sourceBlock.querySelector('p');
    const questionBlock = el(doc, 'div', 'detail-block');
    questionBlock.append(el(doc, 'h2', null, '对应问题'), el(doc, 'p', 'prose'));
    const detailQuestion = questionBlock.querySelector('p');
    const sqlBlock = el(doc, 'div', 'detail-block');
    sqlBlock.append(el(doc, 'h2', null, 'SQL'));
    const sql = el(doc, 'pre', 'sql');
    sql.translate = false;
    sqlBlock.append(sql);
    const stateBlock = el(doc, 'div', 'detail-block');
    stateBlock.append(el(doc, 'h2', null, '执行状态'), el(doc, 'p'));
    const runState = stateBlock.querySelector('p');
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
    const summary = el(doc, 'summary', null, '执行详情');
    const trace = el(doc, 'ol', 'trace');
    traceDetails.append(summary, trace);
    traceBlock.append(traceDetails);
    details.append(sourceBlock, questionBlock, sqlBlock, stateBlock, metricBlock, traceBlock);
    shell.append(sidebar, chat, details);
    app.replaceChildren(shell);

    const nodes = {
      details: details, toggle: toggle, title: title, subtitle: subtitle,
      sourceList: sourceList, talkList: talkList, currentSource: currentSource,
      currentMeta: currentMeta, thread: thread, sourceName: sourceName, error: error,
      send: send, live: live, detailSource: detailSource, detailQuestion: detailQuestion,
      sql: sql, runState: runState, tokenIn: tokenIn, tokenOut: tokenOut,
      duration: duration, usageNote: usageNote, trace: trace, traceDetails: traceDetails
    };

    function showError(text) {
      state.error = text || '';
      nodes.error.textContent = state.error;
      if (state.error) question.focus();
    }

    function paint() {
      render(doc, state, nodes, handlers);
      syncUrl(state);
      const marker = state.conversationId + ':' + state.selectedMessageId;
      if (nodes.lastScrolled !== marker) {
        nodes.lastScrolled = marker;
        const selectedNode = state.selectedMessageId
          ? nodes.thread.querySelector('[data-message-id="' + state.selectedMessageId + '"]')
          : null;
        if (selectedNode && selectedNode.scrollIntoView) selectedNode.scrollIntoView({ block: 'nearest' });
      }
    }

    function ask(text) {
      const current = activeConversation(state);
      if (current) {
        const existing = current.messages.find(function (item) { return item.role === 'user' && item.body === text; });
        if (existing) {
          const index = current.messages.indexOf(existing);
          const answer = current.messages[index + 1];
          state.selectedMessageId = answer ? answer.id : existing.id;
          state.detailsOpen = true;
          paint();
          const target = answer ? nodes.thread.querySelector('[data-message-id="' + answer.id + '"]') : null;
          if (target && target.scrollIntoView) target.scrollIntoView({ block: 'nearest' });
          return;
        }
      }
      submitText(text);
    }

    let timer = 0;
    function submitText(text) {
      const result = appendQuestion(state, text);
      if (!result.ok) {
        showError(result.error);
        return;
      }
      question.value = '';
      paint();
      timer = setTimeout(function () {
        completePending(state, result.pendingId);
        paint();
      }, 700);
    }

    const handlers = {
      ask: ask,
      stop: function () {
        clearTimeout(timer);
        stopPending(state);
        paint();
      },
      showSql: function (id) {
        state.selectedMessageId = id;
        state.detailsOpen = true;
        paint();
        if (nodes.sql.scrollIntoView) nodes.sql.scrollIntoView({ block: 'nearest' });
      },
      pickSource: function (id) {
        const result = selectSource(state, id);
        if (!result.ok) showError(result.error);
        else paint();
      },
      pickConversation: function (id) {
        const result = selectConversation(state, id);
        if (!result.ok) showError(result.error);
        else paint();
      }
    };

    newer.addEventListener('click', function () {
      const result = createConversation(state);
      if (!result.ok) {
        showError(result.error);
        return;
      }
      question.value = '';
      paint();
      const desktop = typeof matchMedia === 'function' && matchMedia('(min-width: 761px) and (pointer: fine)').matches;
      if (desktop) question.focus();
    });
    toggle.addEventListener('click', function () {
      state.detailsOpen = !state.detailsOpen;
      paint();
    });
    traceDetails.addEventListener('toggle', function () {
      state.traceOpen = traceDetails.open;
    });
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      submitText(question.value);
    });
    question.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    paint();
    return { state: state, nodes: nodes, ask: ask, submitText: submitText, handlers: handlers };
  }

  const api = {
    SOURCES: SOURCES,
    sourceById: sourceById,
    replyFor: replyFor,
    formatMoney: formatMoney,
    formatWhen: formatWhen,
    formatDuration: formatDuration,
    formatTokens: formatTokens,
    createState: createState,
    appendQuestion: appendQuestion,
    completePending: completePending,
    stopPending: stopPending,
    selectSource: selectSource,
    selectConversation: selectConversation,
    createConversation: createConversation,
    cellText: cellText,
    mount: mount
  };
  root.SQLAgentStudio = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { mount(document); });
    else mount(document);
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
