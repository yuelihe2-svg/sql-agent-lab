'use strict';
/* SQL Agent Studio client. Text is assigned with textContent. No mock query results. */
(function (root) {
  const count = new Intl.NumberFormat('en-US');
  const when = new Intl.DateTimeFormat('en-US', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const seconds = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const stageNames = {
    preparing: 'Preparing the query',
    schema: 'Reading the database schema',
    selector: 'Selecting relevant columns',
    decomposer: 'Generating SQL',
    execute: 'Checking and running SQL',
    refiner: 'Repairing SQL',
    requirements: 'Reviewing output requirements',
    semantic_review: 'Reviewing the query',
    evaluation: 'Running an independent evaluation',
    finalizing: 'Preparing the result',
    finished: 'Preparing the result'
  };

  function formatWhen(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : when.format(date);
  }
  function formatElapsed(value) {
    if (value == null || !Number.isFinite(Number(value))) return 'Not recorded';
    return seconds.format(Number(value)) + ' s';
  }
  function formatTokens(value) {
    if (value == null) return 'Not recorded';
    return count.format(value);
  }
  function stageLabel(stage) {
    return stageNames[stage] || 'Working on the query';
  }
  function statusLabel(status) {
    return {
      completed: 'Completed',
      failed: 'Query failed',
      running: 'Running',
      stopping: 'Stopping',
      stopped: 'Stopped',
      interrupted: 'Interrupted'
    }[status] || 'Status not recorded';
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
    if (state === 'stopping') return 'Stop requested. Waiting for the current operation to return.';
    if (state === 'stopped') return 'The query stopped. Edit the question and run it again.';
    if (state === 'interrupted') return 'The service was interrupted. This query was not rerun automatically.';
    if (state === 'failed') return view.error || 'This query did not finish.';
    return 'This record has no confirmed execution result.';
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
        evaluated: view.evaluated === true,
        grounding: view.grounding && view.grounding.enabled === true ? view.grounding : { enabled: false }
      });
    });
    return items;
  }
  function caption(turn) {
    if (!turn || turn.preview !== true) return 'No query result to show.';
    if (turn.complete === true) return 'Query result. All returned rows are shown below.';
    return 'Result preview. The row count is not the total and is not a complete result.';
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
      article.setAttribute('aria-label', 'Your question');
      return article;
    }
    article.setAttribute('aria-label', 'Answer');
    const meta = el(doc, 'div', 'meta');
    meta.append(el(doc, 'span', 'status-' + (item.state || ''), statusLabel(item.state)));
    meta.append(el(doc, 'span', null, item.complete ? 'All returned rows' : (item.preview ? 'Result preview' : 'No result table')));
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
      article.append(wrap, el(doc, 'p', 'table-note', caption(item) + ' NULL means an empty value.'));
    } else if (item.preview) {
      article.append(el(doc, 'p', 'table-note', caption(item)));
    }
    if (item.sql) {
      const show = el(doc, 'button', 'text-action', 'Show this SQL');
      show.type = 'button';
      show.addEventListener('click', function () { handlers.showSql(item.id); });
      article.append(show);
    }
    if (item.state === 'running') {
      const stop = el(doc, 'button', 'text-action', 'Stop');
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
    nodes.toggle.textContent = state.detailsOpen ? 'Hide details' : 'Show SQL details';
    nodes.toggle.setAttribute('aria-expanded', state.detailsOpen ? 'true' : 'false');
    nodes.title.textContent = conversation ? (conversation.conversation.title || 'Untitled conversation') : 'No conversation';
    nodes.subtitle.textContent = source ? source.name : '';
    doc.title = nodes.title.textContent + ' · SQL Agent Studio';
    nodes.banner.hidden = !state.notice;
    nodes.banner.textContent = state.notice || '';

    nodes.sourceList.replaceChildren();
    if (!state.sources.length) nodes.sourceList.append(el(doc, 'p', 'quiet', 'No registered data sources.'));
    state.sources.forEach(function (item) {
      const button = el(doc, 'button', 'source');
      button.type = 'button';
      button.setAttribute('aria-pressed', item.id === state.datasourceId ? 'true' : 'false');
      button.append(el(doc, 'strong', null, item.name), el(doc, 'small', null, item.read_only ? 'Read-only SQLite' : item.kind || ''));
      button.addEventListener('click', function () { handlers.pickSource(item.id); });
      nodes.sourceList.append(button);
    });

    const talks = talksFor(state);
    nodes.talkList.replaceChildren();
    if (!talks.length) nodes.talkList.append(el(doc, 'p', 'quiet', 'No conversations for this data source.'));
    talks.forEach(function (item) {
      const button = el(doc, 'button', 'talk');
      button.type = 'button';
      if (conversation && item.id === conversation.conversation.id) button.setAttribute('aria-current', 'true');
      button.title = item.title;
      button.append(el(doc, 'strong', null, item.title), el(doc, 'small', null, formatWhen(item.updated_at)));
      button.addEventListener('click', function () { handlers.pickConversation(item.id); });
      nodes.talkList.append(button);
    });
    nodes.currentSource.textContent = source ? source.name : 'Not selected';
    nodes.currentMeta.textContent = source ? (source.read_only ? 'Read-only SQLite' : source.kind) : '';

    nodes.form.hidden = state.view === 'overview';
    nodes.toggle.hidden = state.view === 'overview';
    nodes.details.hidden = state.view === 'overview' || !state.detailsOpen;
    if (state.view === 'overview') {
      nodes.title.textContent = 'Database overview';
      nodes.subtitle.textContent = source ? source.name : '';
      doc.title = 'Database overview · SQL Agent Studio';
      nodes.thread.replaceChildren();
      if (!state.workspace) {
        nodes.thread.append(el(doc, 'div', 'empty-thread', state.notice || overviewError(404)));
      } else if (state.overviewError) {
        nodes.thread.append(el(doc, 'div', 'empty-thread', state.overviewError));
      } else if (!state.overview) {
        nodes.thread.append(el(doc, 'div', 'empty-thread', 'Reading the database overview.'));
      } else {
        nodes.thread.append(renderOverview(doc, {
          summary: overviewSummary(state.overview.profile),
          tables: schemaTables(state.overview.profile),
          knowledge: knowledgeCards(state.overview.knowledge, state.datasourceId)
        }, handlers));
        nodes.thread.append(renderRetrievalForm(doc, handlers));
        if (state.grounding && state.grounding.datasource_id === state.datasourceId) {
          nodes.thread.append(renderGrounding(doc, state.grounding));
        }
        nodes.thread.append(renderKnowledgeForm(doc, state, handlers));
      }
      nodes.live.textContent = 'Database overview';
      nodes.sourceName.textContent = source ? source.name : 'Not selected';
      nodes.error.textContent = state.error || '';
      return;
    }
    const turns = conversation ? pairTurns(conversation) : [];
    nodes.thread.replaceChildren();
    if (!state.workspace || !state.sources.length) {
      nodes.thread.append(el(doc, 'div', 'empty-thread', state.notice || 'This workspace cannot save conversations yet.'));
    } else if (!conversation) {
      const empty = el(doc, 'div', 'empty-thread');
      empty.append(
        el(doc, 'h2', null, 'Write a question for this database'),
        el(doc, 'p', 'prose', 'Start a conversation. The question and the query result are saved in this thread.')
      );
      nodes.thread.append(empty);
    } else if (!turns.length) {
      nodes.thread.append(el(doc, 'div', 'empty-thread', 'This conversation has no questions yet.'));
    } else {
      turns.forEach(function (item) { nodes.thread.append(renderMessage(doc, item, handlers)); });
    }
    nodes.sourceName.textContent = source ? source.name : 'Not selected';
    nodes.error.textContent = state.error || '';
    const busy = Boolean(state.sending || state.activeRunId);
    nodes.send.disabled = busy || !state.workspace || !state.sources.length;
    nodes.send.textContent = busy ? 'Working…' : 'Send';
    const control = groundingControl(state.workspace);
    nodes.groundingInput.disabled = !control.enabled || busy;
    if (!control.enabled) nodes.groundingInput.checked = false;
    nodes.groundingNote.hidden = !control.note;
    nodes.groundingNote.textContent = control.note;
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
    nodes.detailQuestion.textContent = question || 'Select an answer to see its question.';
    nodes.sql.textContent = selected && selected.sql ? selected.sql : 'This question has no SQL.';
    nodes.runState.textContent = selected
      ? statusLabel(selected.state) + (selected.evaluated ? ' · Evaluated' : ' · Not evaluated')
      : 'No query selected';
    nodes.tokenIn.textContent = formatTokens(selected && selected.tokens ? selected.tokens.input : null);
    nodes.tokenOut.textContent = formatTokens(selected && selected.tokens ? selected.tokens.output : null);
    nodes.duration.textContent = formatElapsed(selected ? selected.elapsed : null);
    nodes.usageNote.textContent = 'Missing values are shown as Not recorded. A successful execution is not an evaluation pass.';
    const grounding = selected && selected.grounding && selected.grounding.enabled === true ? selected.grounding : null;
    nodes.groundingStatus.textContent = grounding
      ? ('Database grounding is on. Strategy: ' + (grounding.strategy || 'keyword')
        + '. Matched tables: ' + ((grounding.matched_tables || []).join(', ') || 'none')
        + '. Knowledge documents: ' + ((grounding.knowledge || []).map(function (item) {
          return (item.title || item.id) + ' v' + item.version;
        }).join(', ') || 'none')
        + '. This does not prove the SQL is correct.')
      : 'Database grounding was not used.';
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
    if (workspace === true) return { enabled: true, label: 'Use Database Grounding', note: '' };
    return {
      enabled: false,
      label: 'Use Database Grounding',
      note: 'Workspace is off, so database grounding is unavailable.'
    };
  }

  function messagePayload(question, useGrounding) {
    return { question: question, use_grounding: useGrounding === true };
  }

  function overviewError(status) {
    if (status === 404) return 'Workspace is off. Restart with --workspace-db and refresh.';
    if (status === 400) return 'This data source overview could not be read.';
    return 'The database overview could not be read.';
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
      el(doc, 'h2', null, 'Database summary'),
      el(doc, 'p', 'prose', summary.name + ' · ' + (summary.kind || 'sqlite')),
      el(doc, 'p', 'prose', summary.tableCount + ' tables. Catalog facts come from the schema. Business rows were not read, and business meaning was not inferred.')
    );
    if (summary.fingerprint) head.append(el(doc, 'p', 'quiet', 'Fingerprint ' + summary.fingerprint));
    head.append(el(doc, 'p', 'quiet', 'Extracted ' + (summary.extractedAt ? formatWhen(summary.extractedAt) : 'Not recorded')));
    root.append(head);

    const schema = el(doc, 'section', 'overview-block');
    schema.append(el(doc, 'h2', null, 'Schema'));
    if (!model.tables.length) {
      schema.append(el(doc, 'p', 'prose', 'This database has no user tables.'));
    }
    model.tables.forEach(function (table) {
      const details = el(doc, 'details', 'schema-table');
      details.append(el(doc, 'summary', null, table.name));
      if (table.columns.length) {
        const wrap = el(doc, 'div', 'table-wrap');
        const grid = el(doc, 'table');
        const headRow = el(doc, 'tr');
        ['Column', 'Type', 'Primary key'].forEach(function (label) {
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
            el(doc, 'td', null, column.type || 'Not declared'),
            el(doc, 'td', null, column.primaryKey ? 'Yes' : '')
          );
          tbody.append(line);
        });
        grid.append(thead, tbody);
        wrap.append(grid);
        details.append(wrap);
      } else {
        details.append(el(doc, 'p', 'quiet', 'No columns.'));
      }
      details.append(el(doc, 'p', 'quiet', table.primaryKey.length
        ? 'Declared primary key: ' + table.primaryKey.join(', ')
        : 'No declared primary key.'));
      if (table.foreignKeys.length) {
        table.foreignKeys.forEach(function (key) {
          details.append(el(doc, 'p', 'quiet', 'Declared foreign key: ' + key.from + ' → ' + key.to));
        });
      } else {
        details.append(el(doc, 'p', 'quiet', 'No declared foreign key.'));
      }
      schema.append(details);
    });
    root.append(schema);

    const knowledge = el(doc, 'section', 'overview-block');
    knowledge.append(
      el(doc, 'h2', null, 'Business knowledge'),
      el(doc, 'p', 'unverified', 'User-provided and unverified. These notes are not facts checked against the database.')
    );
    if (!model.knowledge.length) {
      knowledge.append(el(doc, 'p', 'prose', 'No business knowledge yet. Text you add belongs only to this data source.'));
    }
    model.knowledge.forEach(function (item) {
      const card = el(doc, 'article', 'message');
      card.append(
        el(doc, 'p', 'prose', item.title),
        el(doc, 'p', 'quiet', 'version ' + item.version + ' · Source: user-provided'),
        el(doc, 'p', 'unverified', 'User-provided / Unverified'),
        el(doc, 'p', 'prose', item.body)
      );
      const edit = el(doc, 'button', 'text-action', 'Edit');
      edit.type = 'button';
      edit.addEventListener('click', function () { handlers.edit(item); });
      const remove = el(doc, 'button', 'text-action', 'Delete');
      remove.type = 'button';
      remove.addEventListener('click', function () { handlers.remove(item); });
      card.append(edit, remove);
      knowledge.append(card);
    });
    root.append(knowledge);
    return root;
  }

  function renderKnowledgeForm(doc, state, handlers) {
    const draft = state.draft || { title: '', format: 'markdown', body: '', id: null };
    const form = el(doc, 'form', 'overview-block');
    form.append(el(doc, 'h2', null, draft.id ? 'Edit knowledge' : 'Add knowledge'));
    const title = el(doc, 'input');
    title.value = draft.title;
    title.maxLength = 200;
    title.required = true;
    title.setAttribute('aria-label', 'Knowledge title');
    const format = el(doc, 'select');
    format.setAttribute('aria-label', 'Knowledge format');
    [['markdown', 'Markdown'], ['text', 'Plain text'], ['json', 'JSON']].forEach(function (pair) {
      const option = el(doc, 'option', null, pair[1]);
      option.value = pair[0];
      if (pair[0] === draft.format) option.selected = true;
      format.append(option);
    });
    const body = el(doc, 'textarea');
    body.value = draft.body;
    body.maxLength = 24000;
    body.setAttribute('aria-label', 'Knowledge body');
    const save = el(doc, 'button', 'send', draft.id ? 'Save changes' : 'Add knowledge');
    save.type = 'submit';
    form.append(title, format, body, save);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      handlers.saveKnowledge({
        id: draft.id,
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
      el(doc, 'h2', null, 'Retrieval preview'),
      el(doc, 'p', 'quiet', 'keyword is the default. bm25 reranks business knowledge only. Chinese text matches as a contiguous run, not as semantic search. This does not run SQL or send the result to the Agent.')
    );
    const input = el(doc, 'input');
    input.setAttribute('aria-label', 'Retrieval question');
    input.maxLength = 1000;
    const strategy = el(doc, 'select');
    strategy.setAttribute('aria-label', 'Retrieval strategy');
    [['keyword', 'keyword'], ['bm25', 'bm25'], ['hybrid', 'hybrid']].forEach(function (item) {
      const option = el(doc, 'option', null, item[1]);
      option.value = item[0];
      strategy.append(option);
    });
    strategy.value = 'keyword';
    const button = el(doc, 'button', 'send', 'Show matches');
    button.type = 'submit';
    form.append(strategy, input, button);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      handlers.preview(input.value, strategy.value);
    });
    return form;
  }

  function scoreLine(item) {
    if (item.score_kind === 'bm25' && typeof item.bm25_score === 'number') {
      return 'BM25 ' + item.bm25_score.toFixed(6) + '(lower is more relevant; not added to the schema score)';
    }
    if (typeof item.score === 'number') return 'Keyword score ' + item.score;
    return '';
  }

  function renderGrounding(doc, result) {
    const section = el(doc, 'section', 'overview-block');
    section.append(el(doc, 'h2', null, 'What this question matched'), el(doc, 'p', 'prose', result.question));
    const latency = typeof result.retrieval_latency_ms === 'number' ? (' · ' + result.retrieval_latency_ms + ' ms') : '';
    section.append(el(doc, 'p', 'quiet', 'Strategy ' + (result.strategy || 'keyword') + latency));
    if (!(result.tables || []).length && !(result.knowledge || []).length) {
      section.append(el(doc, 'p', 'prose', 'No tables, columns, or knowledge matched.'));
    }
    (result.tables || []).forEach(function (table) {
      const card = el(doc, 'article', 'message');
      const columns = (table.columns || []).map(function (column) {
        return column.name + ' (' + column.reason + ')';
      }).join(', ');
      card.append(
        el(doc, 'p', 'prose', 'Table ' + table.name),
        el(doc, 'p', 'quiet', 'Schema fact · ' + table.reason + (scoreLine(table) ? ' · ' + scoreLine(table) : '')),
        el(doc, 'p', 'prose', columns ? ('Columns ' + columns) : 'No separately matched column')
      );
      section.append(card);
    });
    (result.relationships || []).forEach(function (item) {
      section.append(el(doc, 'p', 'quiet',
        'Declared foreign key: ' + item.from_table + '.' + (item.from_columns || []).join(',') +
        ' → ' + item.to_table + '.' + (item.to_columns || []).join(',') + '. ' + item.reason));
    });
    (result.knowledge || []).forEach(function (item) {
      const card = el(doc, 'article', 'message');
      card.append(
        el(doc, 'p', 'prose', item.title),
        el(doc, 'p', 'unverified', 'User-provided / Unverified'),
        el(doc, 'p', 'quiet', 'User-provided definition · version ' + item.version + ' · ' + item.reason
          + (scoreLine(item) ? ' · ' + scoreLine(item) : '')),
        el(doc, 'p', 'prose', item.excerpt || '')
      );
      if (item.instruction_like_text) {
        card.append(el(doc, 'p', 'quiet', 'This text remains a user note and is not a system instruction.'));
      }
      section.append(card);
    });
    section.append(el(doc, 'p', 'quiet', 'Tables and columns come from the database catalog. Knowledge cannot override system rules.'));
    return section;
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
      sending: false,
      view: 'chat',
      overview: null,
      draft: null,
      grounding: null
    };
  }

  function mount(doc, options) {
    const state = createState();
    const transport = options && options.transport;
    const app = doc.getElementById('app');
    const shell = el(doc, 'div', 'shell');
    const sidebar = el(doc, 'aside', 'sidebar');
    sidebar.setAttribute('aria-label', 'Workspace');
    const brand = el(doc, 'div', 'brand');
    brand.append(el(doc, 'p', 'brand-name', 'SQL Agent Studio'), el(doc, 'p', 'brand-note', 'Local read-only workspace'));
    const sourceLabel = el(doc, 'p', 'side-label', 'Data source');
    const sourceList = el(doc, 'div', 'source-list');
    sourceList.setAttribute('role', 'group');
    sourceList.setAttribute('aria-label', 'Data source');
    const newer = el(doc, 'button', 'new-chat', 'New conversation');
    newer.type = 'button';
    const overviewButton = el(doc, 'button', 'new-chat', 'Database overview');
    overviewButton.type = 'button';
    const talkLabel = el(doc, 'p', 'side-label', 'Conversations');
    const talkList = el(doc, 'nav', 'talk-list');
    talkList.setAttribute('aria-label', 'Conversations');
    const foot = el(doc, 'div', 'sidebar-foot');
    foot.append(el(doc, 'p', 'quiet', 'Current data source'), el(doc, 'p'), el(doc, 'p', 'quiet'));
    const currentSource = foot.querySelector('p:nth-child(2)');
    const currentMeta = foot.querySelector('p:nth-child(3)');
    sidebar.append(brand, sourceLabel, sourceList, newer, overviewButton, talkLabel, talkList, foot);

    const chat = el(doc, 'main', 'chat');
    chat.id = 'conversation';
    chat.tabIndex = -1;
    const head = el(doc, 'div', 'chat-head');
    const heading = el(doc, 'div');
    const title = el(doc, 'h1');
    const subtitle = el(doc, 'p');
    heading.append(title, subtitle);
    const toggle = el(doc, 'button', 'detail-toggle', 'Hide details');
    toggle.type = 'button';
    head.append(heading, toggle);
    const banner = el(doc, 'p', 'demo-banner');
    banner.hidden = true;
    const thread = el(doc, 'div', 'thread');
    const live = el(doc, 'div', 'visually-hidden');
    live.setAttribute('aria-live', 'polite');
    const form = el(doc, 'form', 'composer');
    const sourceLine = el(doc, 'p', 'composer-source');
    sourceLine.append(doc.createTextNode('Data source '), el(doc, 'strong'));
    const sourceName = sourceLine.querySelector('strong');
    const groundingLabel = el(doc, 'label', 'grounding-toggle');
    const groundingInput = el(doc, 'input');
    groundingInput.type = 'checkbox';
    groundingInput.checked = false;
    groundingLabel.append(groundingInput, doc.createTextNode(' Use Database Grounding'));
    const groundingNote = el(doc, 'p', 'quiet');
    const box = el(doc, 'div', 'composer-box');
    const question = el(doc, 'textarea');
    question.id = 'question';
    question.name = 'question';
    question.maxLength = 4000;
    question.autocomplete = 'off';
    question.placeholder = 'Describe what you want to query';
    question.setAttribute('aria-label', 'Question');
    const actions = el(doc, 'div', 'composer-actions');
    const send = el(doc, 'button', 'send', 'Send');
    send.type = 'submit';
    actions.append(send);
    box.append(question, actions);
    const error = el(doc, 'p', 'form-error');
    error.setAttribute('role', 'alert');
    form.append(sourceLine, groundingLabel, groundingNote, box, el(doc, 'p', 'hint', 'Ctrl + Enter to send'), error);
    chat.append(head, banner, live, thread, form);

    const details = el(doc, 'aside', 'details');
    details.setAttribute('aria-label', 'Query details');
    const sourceBlock = el(doc, 'div', 'detail-block');
    sourceBlock.append(el(doc, 'h2', null, 'Data source'), el(doc, 'p'));
    const questionBlock = el(doc, 'div', 'detail-block');
    questionBlock.append(el(doc, 'h2', null, 'Question'), el(doc, 'p', 'prose'));
    const sqlBlock = el(doc, 'div', 'detail-block');
    const sql = el(doc, 'pre', 'sql');
    sql.translate = false;
    sqlBlock.append(el(doc, 'h2', null, 'SQL'), sql);
    const stateBlock = el(doc, 'div', 'detail-block');
    stateBlock.append(el(doc, 'h2', null, 'Execution status'), el(doc, 'p'));
    const metricBlock = el(doc, 'div', 'detail-block');
    metricBlock.append(el(doc, 'h2', null, 'Tokens and time'));
    const metrics = el(doc, 'div', 'metrics');
    function metric(label) {
      const wrap = el(doc, 'div');
      const strong = el(doc, 'strong', null, 'Not recorded');
      wrap.append(el(doc, 'span', null, label), strong);
      metrics.append(wrap);
      return strong;
    }
    const tokenIn = metric('Input tokens');
    const tokenOut = metric('Output tokens');
    const duration = metric('Time');
    const usageNote = el(doc, 'p', 'quiet');
    metricBlock.append(metrics, usageNote);
    const groundingBlock = el(doc, 'div', 'detail-block');
    const groundingStatus = el(doc, 'p', 'prose', 'Database grounding was not used.');
    groundingBlock.append(el(doc, 'h2', null, 'Grounding'), groundingStatus);
    const traceBlock = el(doc, 'div', 'detail-block');
    const traceDetails = el(doc, 'details');
    const trace = el(doc, 'ol', 'trace');
    traceDetails.append(el(doc, 'summary', null, 'Current stage'), trace);
    traceBlock.append(traceDetails);
    details.append(sourceBlock, questionBlock, sqlBlock, stateBlock, metricBlock, groundingBlock, traceBlock);
    shell.append(sidebar, chat, details);
    app.replaceChildren(shell);

    const nodes = {
      details: details, toggle: toggle, title: title, subtitle: subtitle, banner: banner,
      sourceList: sourceList, talkList: talkList, currentSource: currentSource,
      currentMeta: currentMeta, thread: thread, sourceName: sourceName, error: error,
      send: send, live: live, form: form, detailSource: sourceBlock.querySelector('p'),
      detailQuestion: questionBlock.querySelector('p'), sql: sql,
      runState: stateBlock.querySelector('p'), tokenIn: tokenIn, tokenOut: tokenOut,
      duration: duration, usageNote: usageNote, trace: trace,
      groundingInput: groundingInput, groundingNote: groundingNote, groundingStatus: groundingStatus
    };

    function paint() { render(doc, state, nodes, handlers); }

    function showError(text) {
      state.error = text || '';
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
        const failure = new Error(data.error || 'The request did not finish');
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

    function applyDetail(detail) {
      state.detail = detail;
      state.datasourceId = detail.conversation.datasource_id;
      state.view = 'chat';
      const turns = pairTurns(detail);
      const assistants = turns.filter(function (item) { return item.role === 'assistant'; });
      const still = assistants.some(function (item) { return item.id === state.selectedRunId; });
      if (!still) state.selectedRunId = assistants.length ? assistants[assistants.length - 1].id : null;
      const active = assistants.find(function (item) {
        return item.state === 'running' || item.state === 'stopping';
      });
      state.activeRunId = active ? active.id : null;
      remember();
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
          state.notice = 'No data source is connected. Register a read-only SQLite database with --sources, then refresh.';
        } else if (!state.workspace) {
          state.notice = 'Workspace is off. Restart with --workspace-db and refresh. Demo data is not used as a query result.';
        } else {
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
        }
      } catch (error) {
        state.notice = 'Cannot reach the local service. Start it, then refresh this page.';
        showError(error.message);
      }
      paint();
    }

    async function loadOverview() {
      state.overviewError = '';
      state.grounding = null;
      try {
        const id = encodeURIComponent(state.datasourceId);
        const profile = await request('/api/sources/' + id + '/profile');
        const context = await request('/api/sources/' + id + '/context');
        if (context.datasource_id !== state.datasourceId) {
          throw Object.assign(new Error('The data source does not match.'), { status: 400 });
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
          showError('Wait for this query to finish, or stop it first.');
          paint();
          return;
        }
        if (!sourceById(state.sources, id)) return;
        state.datasourceId = id;
        state.detail = null;
        state.selectedRunId = null;
        state.error = '';
        if (state.view === 'overview') {
          state.overview = null;
          state.draft = null;
          state.grounding = null;
          loadOverview();
          return;
        }
        const first = talksFor(state)[0];
        if (first) openConversation(first.id).catch(function (error) { showError(error.message); paint(); });
        else paint();
      },
      pickConversation: function (id) {
        if (state.activeRunId && (!state.detail || state.detail.conversation.id !== id)) {
          showError('Wait for this query to finish, or stop it first.');
          paint();
          return;
        }
        openConversation(id).catch(function (error) { showError(error.message); paint(); });
      },
      edit: function (item) {
        state.draft = { id: item.id, title: item.title, format: item.format, body: item.body };
        paint();
      },
      remove: function (item) {
        const id = encodeURIComponent(state.datasourceId);
        request('/api/sources/' + id + '/knowledge/' + encodeURIComponent(item.id), undefined, 'DELETE')
          .then(function () { state.draft = null; return loadOverview(); })
          .catch(function (error) { showError(error.message); paint(); });
      },
      saveKnowledge: function (draft) {
        const id = encodeURIComponent(state.datasourceId);
        const path = draft.id
          ? '/api/sources/' + id + '/knowledge/' + encodeURIComponent(draft.id)
          : '/api/sources/' + id + '/knowledge';
        request(path, { title: draft.title.trim(), format: draft.format, body: draft.body }, draft.id ? 'PUT' : 'POST')
          .then(function () { state.draft = null; return loadOverview(); })
          .catch(function (error) { showError(error.message); paint(); });
      },
      preview: function (text, strategy) {
        const question = String(text || '').trim();
        if (!question || !state.datasourceId) return;
        const id = encodeURIComponent(state.datasourceId);
        request('/api/sources/' + id + '/grounding', retrievalPreviewPayload(question, strategy), 'POST')
          .then(function (result) {
            if (result.datasource_id !== state.datasourceId) {
              throw Object.assign(new Error('The data source does not match.'), { status: 400 });
            }
            state.grounding = result;
            state.error = '';
            paint();
          })
          .catch(function (error) { showError(error.message); paint(); });
      }
    };

    overviewButton.addEventListener('click', function () {
      if (!state.workspace || !state.datasourceId) {
        showError(state.notice || overviewError(404));
        paint();
        return;
      }
      state.view = 'overview';
      state.error = '';
      state.draft = null;
      loadOverview();
    });

    newer.addEventListener('click', function () {
      if (!state.workspace || !state.datasourceId) {
        showError(state.notice || 'Select a data source first.');
        paint();
        return;
      }
      if (state.activeRunId) {
        showError('Wait for this query to finish, or stop it first.');
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
        if (!state.detail) showError('Start a conversation first.');
        paint();
        return;
      }
      state.sending = true;
      state.error = '';
      paint();
      request('/api/conversations/' + encodeURIComponent(state.detail.conversation.id) + '/messages',
        messagePayload(text, nodes.groundingInput.checked))
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
