// Rule enforcement tests do not need a browser or Home Assistant.
const assert = require('node:assert/strict');
const { BrowserPermissions, rule, validate, trackCommand, boundedResult } = require('../browser_permissions.cjs');

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
const commands = new Map();
assert.equal(trackCommand(commands, {id: 0, type: 'map_tiles/access_token'}), true);
for (let id = 1; id <= 650; id++) assert.equal(trackCommand(commands, {id, type: 'ping'}), true);
assert.equal(commands.size, 500);
assert.equal(commands.get(0), 'map_tiles/access_token');
assert.equal(trackCommand(commands, {id: 651, type: 'map_tiles/access_token'}), false);
assert.equal(trackCommand(commands, {id: 0, type: 'ping'}), false);
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
console.log('PASS: exact browser grants, payload changes, hard blocks, secret rejection, raster capability, origin/method checks, and bounded evidence.');
