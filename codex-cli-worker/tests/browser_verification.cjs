// Protocol fixture exercising real Chromium, external auth, and network guards.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { WebSocketServer } = require('ws');
const https = require('node:https');
const dns = require('node:dns').promises;
const {EventEmitter} = require('node:events');
const { inspect, allowMessage, allowRequest } = require('../browser.cjs');
const { rule, LIMITS } = require('../browser_permissions.cjs');

/**
 * Check the message and request rules, then run inspect against a fake Home
 * Assistant: a clean dashboard over loopback and LAN HTTP, the worker's own
 * capture, external modules, blocked writes and redirects, error cards, and
 * failed authentication and browser launch.
 */
async function main() {
  assert(!allowMessage({ type: 'call_service' }));
  assert(!allowMessage({ type: 'lovelace/config/save' }));
  assert(!allowMessage({ type: 'subscribe_events' }));
  assert(allowMessage({type: 'custom_icons/activesets'}));
  assert(allowMessage({type: 'custom_icons/icon', set: 'local', icon: 'rooms/kitchen'}));
  for (const type of ['custom_icons/select', 'custom_icons/iconify_download', 'browser_mod/connect', 'browser_mod/register']) assert(!allowMessage({type}));
  assert(!allowMessage({type: 'custom_icons/icon', set: 'local', icon: '../secret'}));
  assert(!allowMessage({type: 'custom_icons/list', set: '../local'}));
  assert(!allowMessage({type: 'custom_icons/activesets', active: true}));
  assert(allowMessage({id: 6, type: 'knob_swipe_navigation/config'}));
  assert(!allowMessage({type: 'knob_swipe_navigation/config', navigation_enabled: true}));
  assert(allowMessage({id: 7, type: 'knob_swipe_navigation/subscribe_rotations'}));
  assert(!allowMessage({type: 'knob_swipe_navigation/subscribe_rotations', device_id: 'other'}));
  assert(!allowMessage({type: 'knob_swipe_navigation/navigation_result'}));
  const alexaCondition = {condition: 'and', conditions: [
    {condition: 'not', conditions: [{condition: 'state', entity_id: 'input_text.alexa_howmuchis', state: ''}]},
    {condition: 'not', conditions: [{condition: 'state', entity_id: 'input_text.alexa_howmuchis', state: 'unknown'}]},
  ]};
  const readConditions = [alexaCondition,
    {condition: 'or', conditions: [
      {condition: 'state', entity_id: 'switch.washing_machine_start', state: 'on'},
      {condition: 'state', entity_id: 'binary_sensor.dryer_on_status', state: 'on'},
    ]},
    {condition: 'or', conditions: [1, 2, 3, 4].map(index =>
      ({condition: 'numeric_state', entity_id: `sensor.ibbq_probe_${index}`, above: 0}))},
    {condition: 'numeric_state', entity_id: 'todo.alexa_shopping_list_3', above: 0},
    {condition: 'state', entity_id: ['light.kitchen', 'light.bedroom'], state: ['on', 'off'], match: 'any',
      attribute: 'status', for: {minutes: 1}, alias: 'Wait for status', enabled: true},
    {condition: 'numeric_state', entity_id: 'sensor.temperature', below: 'input_number.limit',
      value_template: '{{ state.state | float }}'},
  ];
  for (const condition of readConditions) assert(allowMessage({type: 'subscribe_condition', condition}), JSON.stringify(condition));
  for (const condition of [null, [], {}, {condition: 'state'}, {condition: 'numeric_state', entity_id: 'sensor.temperature'},
    {condition: 'custom.condition', entity_id: 'light.kitchen'},
    {condition: 'and', conditions: [{condition: 'device', device_id: 'physical'}]},
    {condition: 'or', conditions: {condition: 'state'}},
    {condition: 'state', entity_id: 'light.kitchen', state: 'on', action: 'turn_on'},
    {condition: 'state', entity_id: 'light.kitchen', state: 'x'.repeat(4097)},
    {condition: 'and', conditions: Array(513).fill({condition: 'state', entity_id: 'light.kitchen', state: 'on'})},
  ]) assert(!allowMessage({type: 'subscribe_condition', condition}), JSON.stringify(condition));
  let nested = readConditions[3];
  for (let depth = 0; depth < 14; depth++) nested = {condition: 'not', conditions: [nested]};
  assert(!allowMessage({type: 'subscribe_condition', condition: nested}));
  assert(!allowMessage({type: 'subscribe_condition', condition: alexaCondition, extra: true}));
  for (const type of ['frontend/get_icons', 'render_template', 'sensor/numeric_device_classes', 'weather/subscribe_forecast',
    'recorder/statistic_during_period', 'recorder/statistics_during_period', 'camera/capabilities']) assert(allowMessage({type}));
  for (const type of ['camera/stream', 'camera/update_prefs', 'recorder/clear_statistics', 'recorder/import_statistics']) assert(!allowMessage({type}));
  assert(allowMessage({ type: 'subscribe_events', event_type: 'state_changed' }));
  assert(!allowMessage({type: 'subscribe_events', event_type: 'zha_event'}));
  assert(!allowRequest(new URL('http://evil.test/local/card.js'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/webhook/action'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/custom-action/on'), 'http://ha.test', 'GET', '/lovelace/0'));
  assert(!allowRequest(new URL('http://ha.test/api/services/light/turn_on'), 'http://ha.test', 'POST'));
  assert(allowRequest(new URL('http://ha.test/api/image/serve/' + 'a'.repeat(32) + '/512x512'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/image/upload'), 'http://ha.test', 'POST'));
  assert(allowRequest(new URL('http://ha.test/api/calendars/calendar.fixture?start=2026-01-01&end=2026-01-02'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/calendars/calendar.fixture'), 'http://ha.test', 'POST'));
  for (const pathname of ['/integration-a/frontend.js?v=123', '/integration-b/panel.mjs?version=456'])
    assert(!allowRequest(new URL(pathname, 'http://ha.test'), 'http://ha.test', 'GET'));

  const upstream = [], requests = [], tokens = [];
  let redirectedRequests = 0;
  const redirectTarget = http.createServer((_request, response) => { redirectedRequests++; response.end('external'); });
  await new Promise(resolve => redirectTarget.listen(0, '127.0.0.1', resolve));
  let bad = false, reject = false, external = false, permissionRequests = false, malformedMap = false, busyReads = true, deniedRead = false, fillConditions = false;
  const server = http.createServer((request, response) => {
    requests.push(request.url);
    if (request.url.startsWith('/api/map_tiles/raster/')) {
      assert.equal(new URL(request.url, 'http://fixture').searchParams.get('token'), 'private-map-access');
      response.setHeader('Content-Type', 'image/png');
      response.end(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a9kAAAAASUVORK5CYII=', 'base64'));
      return;
    }
    if (request.url.startsWith('/api/custom/data')) {
      response.setHeader('Content-Type', 'application/json'); response.end('{}'); return;
    }
    if (request.url.startsWith('/api/states') || request.url.startsWith('/api/calendars/')) {
      assert.equal(request.headers.authorization, 'Bearer temporary-browser-token');
      response.setHeader('Content-Type', 'application/json');
      response.end('{"state":"on"}'); return;
    }
    if (request.url === '/local/missing.js') { response.writeHead(404); response.end(); return; }
    if (['/browser_mod.js', '/custom_icons/main.js', '/integration-a/frontend.js?v=123'].includes(request.url)) {
      response.setHeader('Content-Type', 'text/javascript');
      response.end('window.loadedAssets = (window.loadedAssets || 0) + 1;'); return;
    }
    if (request.url === '/integration-b/panel.mjs?version=456') {
      response.setHeader('Content-Type', 'text/javascript');
      response.end("fetch('/api/states/sensor.registered_bundle_loaded', {headers:{Authorization:'Bearer verification-session'}});"); return;
    }
    if (request.url === '/local/redirect.js') {
      response.writeHead(302, {Location: `http://127.0.0.1:${redirectTarget.address().port}/external.js`});
      response.end(); return;
    }
    response.setHeader('Content-Type', 'text/html');
    response.end(`<!doctype html><html><body><script src="/browser_mod.js"></script><script src="/custom_icons/main.js"></script><script src="/integration-a/frontend.js?v=123"></script><home-assistant></home-assistant><script>
      if (window.loadedAssets !== 3) console.error('Dashboard assets failed to execute');
      const report = name => fetch('/api/states/sensor.' + name, {headers:{Authorization:'Bearer verification-session'}});
      // A page script must never see Core's real socket or credentials, even
      // through Playwright's in-page WebSocket plumbing.
      const dispatch = globalThis.__pwWebSocketDispatch;
      if (dispatch) globalThis.__pwWebSocketDispatch = request => {
        const text = JSON.stringify(request);
        if (text.includes('temporary-browser-token') || text.includes('private-map-access')) report('page_saw_real_credentials');
        return dispatch(request);
      };
      const ha = document.querySelector('home-assistant');
      ha.attachShadow({mode:'open'}).innerHTML = '<hui-view><h1>Home dashboard</h1><p>Kitchen light: on</p></hui-view>';
      ha.shadowRoot.innerHTML += '<hui-error-card style="display:none">Hidden loading placeholder</hui-error-card>';
      window.externalAuthSetToken = async (_, token) => {
        if (token.access_token !== 'verification-session') throw Error('Real token reached the page');
        const ws = new WebSocket('ws://' + location.host + '/api/websocket');
        // Home Assistant requires request IDs to increase on each connection.
        let nextId = 1;
        const ids = {};
        const send = message => { const id = nextId++; ws.send(JSON.stringify({id, ...message})); return id; };
        ws.onmessage = async event => {
          if (event.data.includes('private-map-access')) report('map_token_leaked');
          const parsed = JSON.parse(event.data);
          const msg = Array.isArray(parsed) ? parsed[0] : parsed;
          if (msg.type === 'auth_required') ws.send(JSON.stringify({type:'auth',access_token:token.access_token}));
          if (msg.type === 'auth_ok') {
            ha.hass = {connection:ws,user:{name:'Verification'}};
            if (ws._ws) report('page_reached_core_socket');
            send({type:'get_states'});
            send({type:'weather/subscribe_forecast',entity_id:'weather.fixture',forecast_type:'daily'});
            send({type:'call_service',domain:'system_log',service:'write'});
            send({type:'call_service',domain:'persistent_notification',service:'create'});
            send({type:'knob_swipe_navigation/config'});
            send({type:'knob_swipe_navigation/subscribe_rotations'});
            for (const condition of ${JSON.stringify(readConditions)}) send({type:'subscribe_condition',condition});
            ${fillConditions ? `for (let index = 0; index < ${LIMITS.condition_subscriptions}; index++)
              send({type:'subscribe_condition',condition:{condition:'numeric_state',entity_id:'sensor.fixture',above:0}});` : ''}
            send({type:'recorder/statistic_during_period',statistic_id:'sensor.fixture',fixed_period:{start_time:'2026-01-01T00:00:00Z',end_time:'2026-01-02T00:00:00Z'},types:['mean']});
            send({type:'camera/capabilities',entity_id:'camera.fixture'});
            ${permissionRequests ? `
            send({type:'bubble_card_tools/list_modules'});
            ids.map = send({type:'map_tiles/access_token'});
            send({type:'auth/sign_path',path:'/api/camera_proxy/camera.fixture'});
            send({type:'call_service',domain:'light',service:'turn_on'});
            fetch('/api/custom/data?entity_id=sensor.fixture').catch(()=>{});
            ` : ''}
            ${bad ? `send({type:'knob_swipe_navigation/navigation_result',result:'navigated'});
            send({type:'browser_mod/connect',browserID:'fixture'});
            send({type:'subscribe_events',event_type:'zha_event'});
            send({type:'camera/stream',entity_id:'camera.fixture'});
            send({type:'recorder/clear_statistics',statistic_ids:['sensor.fixture']});` : ''}
            ids.resources = send({type:'lovelace/resources'});
            ${busyReads ? `for (let index = 0; index < 650; index++) ids.lastPing = send({type:'ping'});` : ''}
            await fetch('/api/states/light.kitchen', {headers:{Authorization:'Bearer '+token.access_token}});
            await fetch('/api/calendars/calendar.fixture?start=2026-01-01&end=2026-01-02', {headers:{Authorization:'Bearer '+token.access_token}});
            ${bad ? `send({type:'call_service',domain:'light',service:'turn_on'});
            // A reused ID is refused, as Core would, and the connection keeps working.
            ws.send(JSON.stringify({id:1,type:'get_states'}));
            ids.afterReuse = send({type:'get_states'});
            fetch('/api/services/light/turn_on',{method:'POST'}).catch(()=>{});
            const script=document.createElement('script');script.src='/local/missing.js';document.body.append(script);
            const redirect=document.createElement('script');redirect.src='/local/redirect.js';document.body.append(redirect);
            const unsafe=document.createElement('script');unsafe.src='/unsafe-action.js';document.body.append(unsafe);
            for (const url of ['/integration-b/other.mjs', '/integration-b/panel.mjs?version=other']) {
              const script=document.createElement('script');script.src=url;document.body.append(script);
            }
            ha.shadowRoot.querySelector('hui-view').innerHTML+='<hui-error-card>Custom element does not exist: missing-card</hui-error-card>';
            console.error('Fixture custom card error');` : ''}
          }
          if (msg.id === ids.resources && msg.success) {
            for (const resource of msg.result) {
              const script = document.createElement('script'); script.type = 'module'; script.src = resource.url; document.body.append(script);
            }
          }
          if (msg.id === ids.map && msg.success) {
            if (msg.result.token !== 'verification-map-session') throw Error('Map credential reached page scripts');
            const image = new Image();
            image.src = '/api/map_tiles/raster/3/4/5.png?token=' + msg.result.token;
            ha.shadowRoot.append(image);
          }
          if (ids.lastPing && msg.id === ids.lastPing && msg.success) report('busy_connection_survived');
          if (ids.afterReuse && msg.id === ids.afterReuse && msg.success) report('connection_survived_reused_id');
          if (msg.error?.code === 'id_reuse' && msg.error.message.includes('invalid or reused request ID') &&
              msg.error.message.includes('Approval is unavailable')) report('id_refusal_explained');
        };
      };
      window.externalApp.getExternalAuth(JSON.stringify({callback:'externalAuthSetToken'}));
    </script></body></html>`);
  });
  const wss = new WebSocketServer({ server });
  const malformedCloseDelays = [];
  wss.on('connection', ws => {
    let malformedAt;
    ws.on('close', () => { if (malformedAt) malformedCloseDelays.push(Date.now() - malformedAt); });
    ws.send(JSON.stringify({type:'auth_required'}));
    ws.on('message', raw => {
      const message = JSON.parse(String(raw));
      upstream.push(message.type);
      if (message.type === 'auth') {
        tokens.push(message.access_token);
        ws.send(JSON.stringify({type: reject ? 'auth_invalid' : 'auth_ok'}));
      } else if (message.type === 'map_tiles/access_token') {
        if (malformedMap) malformedAt = Date.now();
        ws.send(JSON.stringify([...(malformedMap ? [null] : []), {id:message.id,type:'result',success:true,result:{token:'private-map-access'}}]));
      } else if (deniedRead && message.type === 'get_states') {
        ws.send(JSON.stringify({id:message.id,type:'result',success:false,error:{code:'unauthorized'}}));
      } else ws.send(JSON.stringify({id:message.id,type:'result',success:true,result:message.type === 'lovelace/resources'
        ? [{url: '/integration-b/panel.mjs?version=456', type: 'module'},
          ...(external ? [{url: 'https://registered.example.test/card/main.js', type: 'module'}] : [])] : []}));
    });
  });
  await new Promise(resolve => server.listen(0, '0.0.0.0', resolve));
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ha-browser-test-'));
  try {
    const input = { url: `http://127.0.0.1:${server.address().port}`, path:'/lovelace/0', directory, access_token:'temporary-browser-token',
      resources: {files: ['/browser_mod.js'], directories: ['/custom_icons'], extra_urls: ['/integration-a/frontend.js?v=123']} };
    const first = await inspect(input);
    assert.equal(first.status, 'captured', JSON.stringify(first));
    assert.deepEqual(first.errors, []);
    assert.deepEqual(first.blocked, []);
    assert.deepEqual(first.permission_candidates, [], 'ordinary conditions and knob subscriptions need no approvals');
    assert.equal(requests.filter(url => url === '/api/states/sensor.busy_connection_survived').length, 2);
    busyReads = false;
    // Core's socket lives in the controller, never inside the page.
    assert(!requests.includes('/api/states/sensor.page_reached_core_socket'));
    assert(!requests.includes('/api/states/sensor.page_saw_real_credentials'));
    assert.equal(requests.filter(url => url === '/integration-b/panel.mjs?version=456').length, 2);
    assert.equal(requests.filter(url => url === '/api/states/sensor.registered_bundle_loaded').length, 2);
    assert(first.findings.every(item => item.kind === 'diagnostic' && item.count === 2));
    assert(first.findings.every(item => item.viewports.join(',') === 'desktop,mobile'));
    assert.deepEqual(first.screenshots.map(s => [s.width,s.height]), [[1440,1000],[390,844]]);
    for (const shot of first.screenshots) assert(fs.statSync(shot.file).size > 100);
    assert(tokens.every(token => token === input.access_token));
    const lan = Object.values(os.networkInterfaces()).flat().find(address => address.family === 'IPv4' && !address.internal);
    assert(lan, 'A non-loopback IPv4 interface is required for the HTTP-origin regression');
    input.url = `http://${lan.address}:${server.address().port}`;
    const localHttp = await inspect(input);
    assert.equal(localHttp.status, 'captured', JSON.stringify(localHttp));
    assert.equal(localHttp.screenshots.length, 2);
    assert(upstream.includes('weather/subscribe_forecast'));
    assert(upstream.includes('knob_swipe_navigation/config'));
    assert(upstream.includes('knob_swipe_navigation/subscribe_rotations'));
    assert.equal(upstream.filter(type => type === 'subscribe_condition').length, readConditions.length * 4);
    assert(upstream.includes('recorder/statistic_during_period'));
    assert(upstream.includes('camera/capabilities'));
    assert(requests.includes('/integration-a/frontend.js?v=123'));
    assert(requests.some(url => url.startsWith('/api/calendars/calendar.fixture')));
    const worker = await new Promise((resolve, reject) => {
      const child = execFile(process.platform === 'win32' ? 'python' : 'python3',
        [path.join(__dirname, 'browser_worker_check.py')], {timeout: 130000}, (error, stdout, stderr) => {
          if (error) return reject(new Error(`${error.message}: ${stderr}`));
          try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
        });
      child.stdin.on('error', reject);
      child.stdin.end(JSON.stringify({...input, session_id: 'fixture-session', installation_id: 'a'.repeat(32),
        fixture_permissions: Array.from({length:128}, (_, index) => rule('websocket', {type:'custom/get', index, payload:'x'.repeat(700)}))}));
    });
    assert.equal(worker.status, 'captured', JSON.stringify(worker));
    assert.deepEqual(worker.stored_images.map(image => image.viewport), ['desktop', 'mobile']);
    assert(worker.stored_images.every(image => image.size > 100));
    if (process.platform !== 'win32') assert(worker.memory_peak_mib > 0);
    assert(worker.memory_peak_mib <= worker.memory_limit_mib);
    // Exercise real browser fulfillment with deterministic external DNS/HTTPS.
    // The production fetcher still constructs and pins its own connection.
    const originalLookup = dns.lookup, originalRequest = https.request;
    const modules = new Map([
      ['https://registered.example.test/card/main.js', `
        import {value} from 'https://cdn.jsdelivr.net/npm/fixture@1.0.0/+esm';
        if (value !== 'loaded') throw Error('Nested ESM import failed');
        fetch('/api/states/sensor.external_asset_loaded', {headers:{Authorization:'Bearer verification-session'}});
      `],
      ['https://cdn.jsdelivr.net/npm/fixture@1.0.0/+esm', `
        export {value} from '/npm/@fixture/dependency@1.0.0/+esm';
      `],
      ['https://cdn.jsdelivr.net/npm/@fixture/dependency@1.0.0/+esm', `
        export {value} from '/npm/fixture-leaf@1.0.0/index.js/+esm';
      `],
      ['https://cdn.jsdelivr.net/npm/fixture-leaf@1.0.0/index.js/+esm', "export const value = 'loaded';"],
    ]);
    const moduleRequests = [];
    external = true;
    try {
      /** Resolve the two fixture hosts, and no others, to a public address. */
      dns.lookup = async host => {
        assert(['registered.example.test', 'cdn.jsdelivr.net'].includes(host));
        return [{address: '8.8.8.8', family: 4}];
      };
      /**
       * Stand in for https.request: serve the fixture modules and fail when a
       * request carries an Authorization or Cookie header.
       */
      https.request = (url, options, callback) => {
        assert(modules.has(url.href), url.href);
        moduleRequests.push(url.href);
        assert(!options.headers.Authorization); assert(!options.headers.Cookie);
        const req = new EventEmitter();
        req.destroy = error => {req.emit('error', error); req.emit('close');};
        req.end = () => queueMicrotask(() => {
          const response = new EventEmitter(); response.statusCode = 200;
          response.headers = {'content-type': 'text/javascript', 'access-control-allow-origin': '*'};
          callback(response);
          response.emit('data', Buffer.from(modules.get(url.href)));
          response.emit('end'); req.emit('close');
        });
        return req;
      };
      const rendered = await inspect(input);
      assert.equal(rendered.status, 'captured', JSON.stringify(rendered));
      assert.equal(requests.filter(url => url === '/api/states/sensor.external_asset_loaded').length, 2);
      for (const url of modules.keys()) assert.equal(moduleRequests.filter(request => request === url).length, 2, url);
    } finally {
      dns.lookup = originalLookup; https.request = originalRequest; external = false;
    }
    permissionRequests = true;
    const pending = await inspect(input);
    assert.equal(pending.permission_candidates.length, 3, JSON.stringify(pending));
    assert(!pending.permission_candidates.some(item => item.request.includes('subscribe_condition')));
    assert(pending.blocked.some(item => item.includes('bubble_card_tools/list_modules:') && item.includes('Review this exact request for approval')));
    assert(!JSON.stringify(pending).includes('private-map-access'));
    assert(!upstream.includes('bubble_card_tools/list_modules'));
    const granted = await inspect({...input, permissions: pending.permission_candidates});
    assert.equal(granted.permission_candidates.length, 0, JSON.stringify(granted));
    assert.equal(granted.permissions_used.length, 3);
    assert(upstream.includes('bubble_card_tools/list_modules'));
    assert(upstream.includes('subscribe_condition'));
    assert(upstream.includes('map_tiles/access_token'));
    assert(!upstream.includes('auth/sign_path'));
    assert(!upstream.includes('call_service'));
    assert.equal(requests.filter(url => url.startsWith('/api/map_tiles/raster/')).length, 2);
    assert.equal(requests.filter(url => url.startsWith('/api/custom/data')).length, 2);
    assert(!JSON.stringify(granted).includes('private-map-access'));
    assert(!requests.includes('/api/states/sensor.page_saw_real_credentials'), 'the real map token must stay out of page scripts');
    malformedMap = true;
    const malformed = await inspect({...input, permissions: pending.permission_candidates});
    assert(!requests.includes('/api/states/sensor.map_token_leaked'), 'malformed batches must never bypass token scrubbing');
    assert(malformed.errors.some(error => error.includes('Home Assistant sent an invalid frame')), JSON.stringify(malformed));
    // The controller closes Core's side at once instead of leaving it open until the page closes.
    assert(malformedCloseDelays.length > 0 && malformedCloseDelays.every(delay => delay < 1000), JSON.stringify(malformedCloseDelays));
    malformedMap = false;
    // Removing the grants restores the exact same blocked candidate batch.
    const revoked = await inspect(input);
    assert.deepEqual(revoked.permission_candidates.map(item => item.id).sort(), pending.permission_candidates.map(item => item.id).sort());
    permissionRequests = false;
    bad = true;
    const second = await inspect(input);
    assert.equal(second.status, 'issues');
    assert(second.errors.some(error => error.includes('Custom element')));
    assert(second.errors.some(error => error.includes('HTTP 404')));
    assert(second.blocked.some(item => item.startsWith('WebSocket call_service (light.turn_on):') && item.includes('Approval is unavailable')));
    assert(!second.blocked.some(item => item.includes('WebSocket knob_swipe_navigation/subscribe_rotations')));
    assert(second.blocked.some(item => item.startsWith('WebSocket knob_swipe_navigation/navigation_result:') && item.includes('updates integration state')));
    assert(second.blocked.some(item => item.startsWith('WebSocket browser_mod/connect:') && item.includes('separate support')));
    assert(second.blocked.some(item => item.startsWith('WebSocket subscribe_events (zha_event):') && item.includes('Listening is read-only') && item.includes('administrators')));
    assert(second.blocked.some(item => item.startsWith('WebSocket camera/stream:') && item.includes('Approval is unavailable')));
    assert(second.blocked.some(item => item.startsWith('WebSocket recorder/clear_statistics:')));
    assert(second.blocked.includes('WebSocket get_states (invalid or reused request ID): Approval is unavailable.'));
    assert.equal(requests.filter(url => url === '/api/states/sensor.id_refusal_explained').length, 2);
    assert.equal(requests.filter(url => url === '/api/states/sensor.connection_survived_reused_id').length, 2);
    assert(second.blocked.includes('GET /unsafe-action.js'));
    assert(!requests.includes('/unsafe-action.js'));
    assert(second.blocked.includes('GET /integration-b/other.mjs'));
    assert(second.blocked.includes('GET /integration-b/panel.mjs'));
    assert(!requests.includes('/integration-b/other.mjs'));
    assert(!requests.includes('/integration-b/panel.mjs?version=other'));
    assert.equal(second.findings.find(item => item.message === 'Fixture custom card error').count, 2);
    assert(second.blocked.includes('Redirect /local/redirect.js'));
    assert.equal(redirectedRequests, 0);
    assert(!upstream.includes('call_service'));
    assert(!upstream.includes('browser_mod/connect'));
    assert(!upstream.includes('subscribe_events'));
    assert(!upstream.includes('knob_swipe_navigation/navigation_result'));
    assert(!upstream.includes('camera/stream'));
    assert(!upstream.includes('recorder/clear_statistics'));
    assert(!requests.some(url => url.startsWith('/api/services')));
    assert(!second.permission_candidates.some(item => item.kind === 'websocket'), 'unsupported WebSocket operations must not be offered for approval');
    bad = false;
    fillConditions = true;
    const beforeConditions = upstream.filter(type => type === 'subscribe_condition').length;
    const limited = await inspect(input);
    assert.equal(upstream.filter(type => type === 'subscribe_condition').length - beforeConditions, LIMITS.condition_subscriptions * 2,
      'cap active subscriptions after acknowledgement and release slots between viewports');
    assert(limited.blocked.some(item => item.includes('too many active condition subscriptions') && item.includes('Approval is unavailable')));
    assert.deepEqual(limited.permission_candidates, []);
    fillConditions = false;
    deniedRead = true;
    const denied = await inspect(input);
    assert(denied.errors.some(error => error.includes('Home Assistant get_states: unauthorized') && error.includes('Browser approval cannot change Home Assistant permissions')));
    assert.deepEqual(denied.permission_candidates, []);
    deniedRead = false;
    reject = true;
    assert.equal((await inspect(input)).status, 'unavailable');
    const executable = process.env.HA_BROWSER_EXECUTABLE;
    try {
      process.env.HA_BROWSER_EXECUTABLE = path.join(directory, 'missing-chromium');
      const missing = await inspect(input);
      assert.equal(missing.status, 'unavailable');
      assert.equal(missing.stage, 'launch');
    } finally {
      if (executable === undefined) delete process.env.HA_BROWSER_EXECUTABLE;
      else process.env.HA_BROWSER_EXECUTABLE = executable;
    }
    console.log('Dashboard browser checks passed: loopback and LAN HTTP, worker memory guard and stored images, two viewports, external auth, calendar/weather reads, blocked writes and redirects, error cards, auth/launch failure, cleanup.');
  } finally {
    for (const client of wss.clients) client.terminate();
    await new Promise(resolve => wss.close(resolve));
    await new Promise(resolve => server.close(resolve));
    await new Promise(resolve => redirectTarget.close(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
