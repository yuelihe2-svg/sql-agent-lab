'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const studio = require('./app.js');

const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
assert.equal(/\binnerHTML\b|insertAdjacentHTML|outerHTML|document\.write|\beval\(/.test(source), false);
assert.equal(/1284600|demo-retail|演示 SQL|准确率/.test(source), false);
assert.equal(html.includes('/workspace/app.js'), true);

assert.equal(studio.formatTokens(null), '未记录');
assert.equal(studio.formatTokens(0), '0');
assert.equal(studio.formatElapsed(null), '未记录');
assert.equal(studio.cellText(null).text, 'NULL');
assert.equal(studio.cellText('').text, '');
assert.equal(studio.caption({ preview: true, complete: false }).includes('预览'), true);
assert.equal(studio.caption({ preview: true, complete: true }).includes('全部返回行'), true);
assert.equal(studio.statusLabel('interrupted'), '已中断');
assert.equal(studio.statusLabel('stopped'), '已停止');

const sources = studio.publicSources({
  sources: [{ id: 'shop', name: 'Shop', kind: 'sqlite', read_only: true, path: 'D:/secret/business.sqlite' }]
});
assert.deepEqual(sources, [{ id: 'shop', name: 'Shop', kind: 'sqlite', read_only: true }]);

const detail = {
  messages: [
    { id: 'm1', seq: 1, role: 'user', body: 'How many rows?' },
    { id: 'm2', seq: 2, role: 'user', body: 'How many are left?' }
  ],
  runs: [
    {
      id: 'r1', user_message_id: 'm1', state: 'completed', stage: 'finished',
      result: {
        summary: '「n」的查询结果是 42。', sql: 'SELECT n FROM numbers',
        columns: ['n'], rows: [[42]], preview: true, has_more: false, truncated: false,
        complete: true, elapsed_seconds: 1.2, tokens: { input: null, output: 3 }, evaluated: false
      }
    },
    {
      id: 'r2', user_message_id: 'm2', state: 'failed', stage: 'execute',
      result: { summary: '未找到对应的表或字段，请核对名称，或补充问题中的业务含义。', sql: 'SELECT missing', columns: [], rows: [], preview: true, complete: false, evaluated: false, tokens: { input: null, output: null } }
    }
  ]
};
const turns = studio.pairTurns(detail);
assert.deepEqual(turns.map(function (item) { return item.role; }), ['user', 'assistant', 'user', 'assistant']);
assert.equal(turns[1].sql, 'SELECT n FROM numbers');
assert.equal(turns[1].complete, true);
assert.equal(turns[1].body.includes('42'), true);
assert.equal(turns[3].state, 'failed');
assert.equal(studio.displaySummary({ state: 'stopped', result: {} }).includes('已停止'), true);
assert.equal(studio.displaySummary({ state: 'interrupted', result: {} }).includes('未自动重跑'), true);
assert.equal(studio.displaySummary({
  state: 'completed',
  result: { summary: '「n」的查询结果是 42。', evaluated: false }
}).includes('100'), false);

console.log('workspace ui checks passed');
