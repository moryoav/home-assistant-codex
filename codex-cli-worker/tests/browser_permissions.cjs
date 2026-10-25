// Rule enforcement tests do not need a browser or Home Assistant.
const assert = require('node:assert/strict');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { BrowserPermissions, CommandTracker, LIMITS, canonical, rule, validate, boundedResult, websocketBlockReason } = require('../browser_permissions.cjs');

const moduleList = { id: 1, type: 'bubble_card_tools/list_modules' };
const first = new BrowserPermissions();
assert.equal(first.message(moduleList, 'desktop'), false);
assert.equal(first.message({ type: moduleList.type, id: 77 }, 'mobile'), false);
const candidates = first.evidence().permission_candidates;
assert.equal(candidates.length, 1);
assert.equal(candidates[0].count, 2);
assert.deepEqual(Object.keys(candidates[0]).sort(), ['id', 'kind', 'request', 'count', 'viewports'].sort());
assert.deepEqual(candidates[0].viewports, ['desktop', 'mobile']);
const allowed = new BrowserPermissions(candidates);
assert.equal(allowed.message({ ...moduleList, id: 2 }), true);
assert.equal(allowed.message({ ...moduleList, query: 'different parameters' }), false);
assert.equal(allowed.message({ ...moduleList, type: 'bubble_card_tools/delete_modules' }), false);
assert.equal(validate({ ...candidates[0], id: 'f'.repeat(64) }), null);
for (const type of ['call_service', 'auth/sign_path', 'camera/webrtc/offer', 'camera/webrtc/candidate',
  'execute_script', 'subscribe_trigger', 'custom/get_and_update', 'custom/read', 'auth/list', 'config/list']) {
  assert.equal(rule('websocket', { type }), null, type);
}
assert.equal(rule('websocket', { type: 'custom/get', password: 'secret' }), null);
assert.equal(rule('websocket', { type: 'custom/get', value: 'Bearer secret' }), null);
assert.equal(rule('websocket', { type: 'custom/get', value: Infinity }), null);
assert.equal(rule('websocket', { type: 'custom/get', value: 2 ** 60 }), null);
assert.equal(rule('websocket', { type: 'custom/get', value: 'x'.repeat(1300) }), null);
for (const message of [{id: 1}, {id: 2, type: null}, {id: 3, type: 77}])
  assert.equal(new BrowserPermissions().message(message), false);
assert.equal(rule('websocket', { type: 'camera/webrtc/get_client_config', entity_id: 'camera.front' }), null);
const condition = rule('websocket', { type: 'subscribe_condition', condition: { condition: 'state', entity_id: 'light.kitchen', state: 'on' } });
assert.ok(condition);
assert.equal(new BrowserPermissions([condition]).message(JSON.parse(condition.request)), true);
// Commands the controller restricts by name or parameters stay outside approvals.
for (const message of [{ type: 'knob_swipe_navigation/subscribe_rotations' }, { type: 'knob_swipe_navigation/navigation_result' },
  { type: 'custom_icons/list', set: '../../../config' }, { type: 'custom_icons/icon', set: 'local', icon: '../secret' },
  { type: 'subscribe_events' }, { type: 'subscribe_events', event_type: 'call_service' }, { type: 'browser_mod/settings/get' }]) {
  assert.equal(rule('websocket', message), null, JSON.stringify(message));
}
assert.match(websocketBlockReason(moduleList), /Review this exact request for approval/);
assert.match(websocketBlockReason({type: 'subscribe_events', event_type: 'zha_event'}), /read-only.*administrators/);
assert.match(websocketBlockReason({type: 'browser_mod/connect'}), /stored browser details.*separate support/);
assert.match(websocketBlockReason({type: 'subscribe_events'}), /Listening is not a write.*approval is unavailable/);
assert.match(websocketBlockReason({type: 'knob_swipe_navigation/navigation_result'}), /updates integration state/);
assert.match(websocketBlockReason({type: 'knob_swipe_navigation/subscribe_rotations', extra: true}), /Extra parameters.*approval is unavailable/);
assert.match(websocketBlockReason({type: 'custom/get', password: 'hidden'}), /Approval is unavailable/);
assert.match(websocketBlockReason({type: null}), /Invalid request type/);

const origin = 'http://homeassistant:8123';
const http = new BrowserPermissions();
assert.equal(http.http(new URL('/api/custom/data?entity_id=sensor.test', origin), origin, 'GET'), false);
const exact = new BrowserPermissions(http.evidence().permission_candidates);
assert.equal(exact.http(new URL('/api/custom/data?entity_id=sensor.test', origin), origin, 'GET'), true);
for (const [url, method] of [['/api/custom/data?entity_id=sensor.other', 'GET'], ['/api/custom/data', 'POST'],
  ['/api/custom/data?token=private', 'GET'], ['/api/services/light/turn_on', 'GET'],
  ['/api/custom/delete_all', 'GET'], ['/auth/token', 'GET'], ['/api/custom/get_token', 'GET'],
  ['http://evil.test/api/custom/data?entity_id=sensor.test', 'GET']]) {
  assert.equal(exact.http(new URL(url, origin), origin, method), false, url);
}
// Write words count however they are separated: '_', '/', '-', '.' or camelCase.
for (const writePath of ['/api/custom/delete-all', '/api/x/turn-on', '/api/x/reboot.cgi', '/api/foo/start.json',
  '/api/x/setValue', '/api/x/factory_reset', '/api/x/clear_all'])
  assert.equal(rule('http', { method: 'GET', path: writePath, query: [] }), null, writePath);
assert.ok(rule('http', { method: 'GET', path: '/api/x/getSettings', query: [] }));
// Malformed escapes all decode to U+FFFD, so they cannot identify one exact query.
const malformed = new BrowserPermissions();
assert.equal(malformed.http(new URL('/api/custom/data?x=%FF', origin), origin, 'GET'), false);
assert.deepEqual(malformed.evidence().permission_candidates, []);
// Distinct keys sort independently of locale; repeated values retain their order.
const ordered = new BrowserPermissions();
ordered.http(new URL('/api/custom/data?b=2&a=1&_x=0&A=3', origin), origin, 'GET');
ordered.http(new URL('/api/custom/data?A=3&_x=0&a=1&b=2', origin), origin, 'GET');
const [sortedQuery] = ordered.evidence().permission_candidates;
assert.equal(ordered.evidence().permission_candidates.length, 1);
assert.deepEqual(JSON.parse(sortedQuery.request).query.map(pair => pair[0]), ['A', '_x', 'a', 'b']);
const repeated = new BrowserPermissions();
const originalQuery = new URL('/api/custom/data?entity_id=light.kitchen&b=2&entity_id=lock.front', origin);
repeated.http(originalQuery, origin, 'GET');
const repeatedGrant = repeated.evidence().permission_candidates;
assert.deepEqual(JSON.parse(repeatedGrant[0].request).query,
  [['b', '2'], ['entity_id', 'light.kitchen'], ['entity_id', 'lock.front']]);
const repeatedAllowed = new BrowserPermissions(repeatedGrant);
assert.equal(repeatedAllowed.http(originalQuery, origin, 'GET'), true);
assert.equal(repeatedAllowed.http(new URL('/api/custom/data?b=2&entity_id=light.kitchen&entity_id=lock.front', origin), origin, 'GET'), true);
assert.equal(repeatedAllowed.http(new URL('/api/custom/data?entity_id=lock.front&b=2&entity_id=light.kitchen', origin), origin, 'GET'), false,
  'changing the first value of a repeated parameter requires another approval');
const map = new BrowserPermissions();
assert.equal(map.message({ id: 7, type: 'map_tiles/access_token' }), false);
assert.equal(map.http(new URL('/api/map_tiles/raster/3/4/5.png?token=private-map-token', origin), origin, 'GET'), false);
assert.equal(map.evidence().permission_candidates.length, 1);
assert.ok(!JSON.stringify(map.evidence()).includes('private-map-token'));
const tiles = new BrowserPermissions(map.evidence().permission_candidates);
assert.equal(tiles.message({ id: 8, type: 'map_tiles/access_token' }), true);
assert.equal(tiles.message({ id: 8, type: 'map_tiles/access_token', extra: true }), false);
assert.equal(tiles.http(new URL('/api/map_tiles/raster/4/8/9.png?token=verification-map-session', origin), origin, 'GET'), true);
for (const path of ['/api/map_tiles/raster/3/8/0.png', '/api/map_tiles/raster/30/0/0.png',
  '/api/map_tiles/raster/3/4/5.png?redirect=evil', '/api/map_tiles/raster/3/4/5.png?token=a&token=b']) {
  // Invalid map paths must not fall through to an exact generic HTTP grant.
  const blocked = new BrowserPermissions([rule('map_tiles')]);
  assert.equal(blocked.http(new URL(path, origin), origin, 'GET'), false, path);
  assert.deepEqual(blocked.evidence().permission_candidates, []);
}
const bounded = new BrowserPermissions();
for (let index = 0; index < 50; index++) bounded.message({ type: 'custom/get', entity: index });
assert.equal(bounded.evidence().permission_candidates.length, 16);
// Core requires increasing request IDs, so each response matches one request.
const commands = new CommandTracker();
assert.equal(commands.accept({id: 0, type: 'ping'}), 'invalid or reused request ID', 'Core starts IDs at 1');
assert.equal(commands.accept({id: 1, type: 'map_tiles/access_token'}), null);
assert.equal(commands.accept({id: 1, type: 'ping'}), 'invalid or reused request ID');
assert.equal(commands.accept({id: '2', type: 'ping'}), 'invalid or reused request ID');
for (let id = 2; id <= 2000; id++) {
  assert.equal(commands.accept({id, type: 'ping'}), null);
  commands.settle({id, type: 'pong'});
}
// Answered requests are forgotten, so long sessions keep correlating token responses.
assert.equal(commands.type(1), 'map_tiles/access_token');
assert.equal(commands.pending.size, 1);
commands.settle({id: 1, type: 'result', success: true});
assert.equal(commands.type(1), undefined);
const busy = new CommandTracker(3);
for (let id = 1; id <= 3; id++) assert.equal(busy.accept({id, type: 'get_states'}), null);
assert.equal(busy.accept({id: 4, type: 'get_states'}), 'too many unanswered requests');
// Acknowledged subscriptions retain slots, across sockets, until Core confirms cleanup.
const budget = {active: 0, limit: 2};
const subscriptions = new CommandTracker(1000, budget);
const otherSocket = new CommandTracker(1000, budget);
assert.equal(subscriptions.accept({id: 1, type: 'subscribe_condition'}), null);
subscriptions.settle({id: 1, type: 'result', success: true});
assert.equal(otherSocket.accept({id: 1, type: 'subscribe_condition'}), null);
otherSocket.settle({id: 1, type: 'result', success: true});
assert.equal(subscriptions.pending.size, 0);
assert.match(subscriptions.accept({id: 2, type: 'subscribe_condition'}), /too many active condition subscriptions/);
assert.match(otherSocket.accept({id: 2, type: 'subscribe_condition'}), /too many active condition subscriptions/);
assert.equal(subscriptions.accept({id: 3, type: 'unsubscribe_events', subscription: 1}), null);
subscriptions.settle({id: 3, type: 'result', success: false});
assert.equal(budget.active, 2, 'a failed unsubscribe retains the active slot');
assert.equal(subscriptions.accept({id: 4, type: 'unsubscribe_events', subscription: 1}), null);
assert.match(subscriptions.accept({id: 5, type: 'subscribe_condition'}), /too many active condition subscriptions/);
subscriptions.settle({id: 4, type: 'result', success: true});
assert.equal(budget.active, 1, 'release only when Core acknowledges the unsubscribe');
assert.equal(subscriptions.accept({id: 6, type: 'subscribe_condition'}), null);
subscriptions.settle({id: 6, type: 'result', success: false});
assert.equal(budget.active, 1, 'a condition validation or access error releases its slot');
assert.equal(subscriptions.accept({id: 7, type: 'subscribe_condition'}), null);
subscriptions.close();
subscriptions.close();
assert.equal(budget.active, 1, 'close is idempotent and releases pending subscriptions');
otherSocket.close();
assert.equal(budget.active, 0);
// Saved rules plus every one-off grant an exchange's retries can carry are all loaded.
const grantLimit = LIMITS.saved_rules + LIMITS.candidates * LIMITS.retries;
const allGrants = Array.from({length: grantLimit}, (_, index) => rule('websocket', {type: 'custom/get', index}));
assert.equal(new BrowserPermissions(allGrants).message({type: 'custom/get', index: grantLimit - 1}), true,
  'the final one-off request must not be dropped after the saved rules');
const large = new BrowserPermissions();
for (let index = 0; index < 16; index++) large.message({type: 'custom/get', index, payload: '"'.repeat(550)});
assert.equal(large.evidence().permission_candidates.length, 16);
const findings = Array.from({length: 40}, (_, index) => ({kind: 'dashboard', message: '\\'.repeat(297) + String(index), count: 2, viewports: ['desktop', 'mobile']}));
const result = boundedResult({status: 'issues', ...large.evidence(), errors: findings.map(item => item.message), blocked: [], findings,
  screenshots: [{name: 'desktop', file: 'desktop.png', error_cards: findings.map(item => item.message)}, {name: 'mobile', file: 'mobile.png'}]});
assert.ok(Buffer.byteLength(JSON.stringify(result)) < 60 * 1024);
assert.equal(result.status, 'issues');
assert.equal(result.findings.length, 40, 'keep structured evidence and screenshots when pruning duplicated output');
assert.equal(result.screenshots.length, 2);
for (const candidate of result.permission_candidates) assert.ok(validate(candidate), 'never truncate an approvable request');

// The worker's Python validator must reach the same decision on the same requests.
const corpus = [
  ['websocket', { type: 'bubble_card_tools/list_modules' }],
  ['websocket', { type: 'subscribe_condition', condition: { condition: 'state', entity_id: 'light.x', state: 'on' } }],
  ['websocket', { type: 'system_health/info' }],
  ['websocket', { type: 'custom/get', value: 1.5, list: [1, 2, { deep: true }], text: 'Grüße ☃' }],
  ['websocket', { type: 'knob_swipe_navigation/subscribe_rotations' }],
  ['websocket', { type: 'custom_icons/list', set: '../../../config' }],
  ['websocket', { type: 'subscribe_events', event_type: 'call_service' }],
  ['websocket', { type: 'custom/get_and_update' }],
  ['websocket', { type: 'custom/get', note: 'Bearer\u001cabc' }],
  ['websocket', { type: 'custom/get', value: 'token = abc' }],
  ['websocket', { type: 'custom/get', 'paſsword': 'x' }],
  ['websocket', { type: 'custom/get', ['toKen']: 'x' }],
  ['websocket', { type: 'custom/get', value: 2 ** 60 }],
  ['http', { method: 'GET', path: '/api/custom/data', query: [['entity_id', 'sensor.x']] }],
  ['http', { method: 'HEAD', path: '/api/x/getSettings', query: [] }],
  ['http', { method: 'GET', path: '/api/custom/delete-all', query: [] }],
  ['http', { method: 'GET', path: '/api/x/setValue', query: [] }],
  ['http', { method: 'GET', path: '/api/webhook/x', query: [] }],
  ['http', { method: 'GET', path: '/api/custom/data', query: [['api_key', 'x']] }],
  ['map_tiles', { type: 'map_tiles/raster' }],
];
const items = corpus.map(([kind, value]) => {
  const request = kind === 'websocket' ? Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'id')) : value;
  const snapshot = canonical(request);
  return { kind, request: snapshot, id: createHash('sha256').update(`${kind}\n${snapshot}`).digest('hex'), js: Boolean(rule(kind, value)) };
});
const check = `
import json, sys
sys.path.insert(0, sys.argv[1])
from browser_permissions import valid_rule
items = json.loads(sys.stdin.buffer.read().decode("utf-8"))
print(json.dumps([valid_rule({"id": i["id"], "kind": i["kind"], "request": i["request"]}) is not None for i in items]))`;
const python = JSON.parse(execFileSync(process.platform === 'win32' ? 'python' : 'python3', ['-c', check, path.join(__dirname, '..')],
  { input: Buffer.from(JSON.stringify(items), 'utf8') }).toString());
items.forEach((item, index) => assert.equal(python[index], item.js, `Python and JavaScript disagree on ${item.kind} ${item.request}`));
assert.ok(items.some(item => item.js) && items.some(item => !item.js));
console.log('PASS: exact browser grants, payload changes, hard blocks, secret rejection, restricted commands, write words, raster capability, origin/method checks, request IDs, bounded evidence, and Python/JavaScript agreement.');
