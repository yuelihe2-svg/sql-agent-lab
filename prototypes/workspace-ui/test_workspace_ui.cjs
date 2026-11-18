'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const studio = require('./app.js');

const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
assert.equal(/\binnerHTML\b|insertAdjacentHTML|outerHTML|document\.write|\beval\(/.test(source), false);
assert.equal(/\bfetch\s*\(/.test(source), false);
assert.equal(html.includes('app.js'), true);

const state = studio.createState();
const october = state.conversations.find((item) => item.id === 'retail-october');
assert.deepEqual(
  october.messages.filter((item) => item.role === 'user').map((item) => item.body),
  ['这个数据库包含什么？', '2026 年 10 月净收款额是多少？', '按城市拆分呢？']
);
assert.equal(october.messages.length, 6);

const profile = studio.replyFor('这个数据库包含什么？', 'demo-retail');
assert.equal(profile.demo, true);
assert.equal(profile.tables.length, 8);
assert.equal(profile.sql, null);

const members = studio.replyFor('这个数据库包含什么？', 'demo-members');
assert.equal(members.tables.length, 3);
assert.notEqual(members.summary, profile.summary);

const total = studio.replyFor('2026 年 10 月净收款额是多少？', 'demo-retail');
assert.equal(total.demo, true);
assert.equal(total.ledger.value, 1284600);
assert.equal(total.sql.includes('演示 SQL'), true);
assert.equal(total.rows.length, 1);

const city = studio.replyFor('按城市拆分呢？', 'demo-retail');
const sum = city.rows.reduce((totalAmount, row) => totalAmount + row.amount, 0);
assert.equal(sum, 1284600);
assert.equal(city.rows.some((row) => row.note === null), true);
assert.equal(studio.cellText({ type: 'text' }, null).text, 'NULL');
assert.equal(studio.cellText({ type: 'text' }, '').text, '');

const blocked = studio.replyFor('把 10 月的订单都删掉', 'demo-retail');
assert.equal(blocked.status, 'failed');
assert.equal(blocked.sql, null);
assert.equal(studio.formatTokens(null), '未记录');
assert.equal(studio.formatDuration(null), '未记录');
assert.equal(studio.formatTokens(0), '0');

const before = october.messages.map((item) => item.body);
const added = studio.appendQuestion(state, '<img src=x onerror=alert(1)>', { userId: 'u-xss', pendingId: 'p-xss' });
assert.equal(added.ok, true);
assert.equal(october.messages[0].body, before[0]);
assert.equal(october.messages[6].body, '<img src=x onerror=alert(1)>');
assert.equal(studio.completePending(state, 'p-xss'), true);
assert.equal(october.messages.length, 8);
assert.equal(october.messages[0].body, '这个数据库包含什么？');
assert.equal(october.messages[7].turn.demo, true);
assert.equal(october.messages[7].turn.kind, 'failure');

const switched = studio.selectSource(state, 'demo-members');
assert.equal(switched.ok, true);
assert.equal(state.datasourceId, 'demo-members');
assert.equal(studio.createState().conversations.filter((item) => item.datasourceId === 'demo-members').some((item) => item.id === state.conversationId), true);
const memberTalk = state.conversations.find((item) => item.id === state.conversationId);
assert.equal(memberTalk.datasourceId, 'demo-members');
assert.equal(memberTalk.messages.some((item) => item.body.includes('零售账本')), false);

const fresh = studio.createState();
assert.equal(studio.createConversation(fresh, 'local-new').ok, true);
assert.equal(fresh.conversationId, 'local-new');
assert.equal(fresh.conversations[0].messages.length, 0);
assert.equal(fresh.conversations.find((item) => item.id === 'retail-october').messages.length, 6);

const running = studio.createState();
studio.appendQuestion(running, '会员等级有哪些？', { userId: 'u-run', pendingId: 'p-run' });
assert.equal(studio.selectConversation(running, 'retail-blocked').ok, false);
assert.equal(studio.stopPending(running), true);
assert.equal(running.pendingId, null);
assert.equal(running.conversations.find((item) => item.id === 'retail-october').messages.at(-1).turn.status, 'stopped');

console.log('workspace ui prototype checks passed');
