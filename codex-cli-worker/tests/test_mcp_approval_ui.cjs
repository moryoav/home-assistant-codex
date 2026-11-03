const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.value = ''; this.events = {}; }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
  addEventListener(event, callback) { (this.events[event] ||= []).push(callback); }
}
const calls = [];
const errors = [];
const buttons = [];
const context = {
  document: { createElement(tag) { const e = new Element(tag); if (tag === 'button') buttons.push(e); return e; } },
  textNode(tag, text) { const e = new Element(tag); e.textContent = text; return e; },
  state: { busy: false, id: 'task' },
  controls() { for (const b of buttons) b.disabled = context.state.busy || b.dataset.canAllow === 'false'; },
  showError(error) { if (error) errors.push(error); },
  async api(path, payload) { calls.push({ path, payload }); },
  async loadList() {}, async fetchSelected() {},
};
const source = fs.readFileSync(path.join(__dirname, '../web/assets/chat.js'), 'utf8');
const start = source.indexOf('function renderApproval(');
const end = source.indexOf('/**', source.indexOf('async function sendApproval(', start));
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const flatten = e => [e, ...e.children.flatMap(flatten)];
const request = { approval_id: 'opaque-ui-id', kind: 'mcp', status: 'pending', can_allow: true,
  server: 'local-smoke-test', reason: '<img src=x onerror=alert(1)>', fields: [
    { id: 'confirm', label: 'Echo?', choices: [{ value: true, label: 'Yes' }, { value: false, label: 'No' }] } ] };

(async () => {
  let card = context.renderApproval(request, 'task', 'turn', true);
  const nodes = flatten(card);
  const approve = nodes.find(n => n.textContent === 'Approve');
  const reject = nodes.find(n => n.textContent === 'Reject');
  const select = nodes.find(n => n.tag === 'select');
  assert(approve.disabled);
  context.controls();
  assert(approve.disabled, 'global controls must not enable unanswered approval');
  assert(!reject.disabled);
  select.value = '1'; select.events.change.forEach(callback => callback());
  assert(!approve.disabled);
  await approve.onclick();
  assert.equal(calls[0].path, 'tasks/task/approvals/opaque-ui-id');
  assert.equal(calls[0].payload.content.confirm, false, 'boolean value must survive JSON submission');
  assert.equal(calls[0].payload.decision, 'allow');
  assert.equal(calls[0].payload.turn_id, 'turn');
  assert.equal(nodes.find(n => n.textContent === request.reason).tag, 'p', 'server text is plain text');
  assert(nodes.every(n => n.innerHTML === undefined));
  await reject.onclick();
  assert.equal(calls[1].payload.decision, 'deny');
  assert(!('content' in calls[1].payload));
  card = context.renderApproval({ ...request, fields: [], can_allow: false }, 'task', 'turn', true);
  assert(flatten(card).find(n => n.textContent === 'Approve').disabled);
  card = context.renderApproval({ approval_id: 'cmd', kind: 'command', status: 'pending', can_allow: true }, 'task', 'turn', true);
  assert(flatten(card).some(n => n.textContent === 'Allow once'));
  assert(flatten(card).some(n => n.textContent === 'Deny'));
  card = context.renderApproval({ ...request, status: 'denied' }, 'task', 'turn', true);
  assert(!flatten(card).some(n => n.textContent === 'Approve'));
  assert.equal(errors.length, 0);
  console.log('UI checks passed: explicit selection, Approve/Reject, typed payload, authenticated-route path, expired/unsupported state, plain text rendering, command controls.');
})().catch(error => { console.error(error); process.exitCode = 1; });
