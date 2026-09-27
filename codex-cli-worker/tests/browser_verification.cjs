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
  for (const type of ['frontend/get_icons', 'render_template', 'sensor/numeric_device_classes', 'weather/subscribe_forecast']) assert(allowMessage({type}));
  assert(allowMessage({ type: 'subscribe_events', event_type: 'state_changed' }));
  assert(!allowRequest(new URL('http://evil.test/local/card.js'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/webhook/action'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/custom-action/on'), 'http://ha.test', 'GET', '/lovelace/0'));
  assert(!allowRequest(new URL('http://ha.test/api/services/light/turn_on'), 'http://ha.test', 'POST'));
  assert(allowRequest(new URL('http://ha.test/api/image/serve/' + 'a'.repeat(32) + '/512x512'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/image/upload'), 'http://ha.test', 'POST'));
  assert(allowRequest(new URL('http://ha.test/api/calendars/calendar.fixture?start=2026-01-01&end=2026-01-02'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/calendars/calendar.fixture'), 'http://ha.test', 'POST'));

  const upstream = [], requests = [], tokens = [];
  let redirectedRequests = 0;
  const redirectTarget = http.createServer((_request, response) => { redirectedRequests++; response.end('external'); });
  await new Promise(resolve => redirectTarget.listen(0, '127.0.0.1', resolve));
  let bad = false, reject = false, external = false;
  const server = http.createServer((request, response) => {
    requests.push(request.url);
    if (request.url.startsWith('/api/states') || request.url.startsWith('/api/calendars/')) {
      assert.equal(request.headers.authorization, 'Bearer temporary-browser-token');
      response.setHeader('Content-Type', 'application/json');
      response.end('{"state":"on"}'); return;
    }
    if (request.url === '/local/missing.js') { response.writeHead(404); response.end(); return; }
    if (['/browser_mod.js', '/custom_icons/main.js'].includes(request.url)) {
      response.setHeader('Content-Type', 'text/javascript');
      response.end('window.loadedAssets = (window.loadedAssets || 0) + 1;'); return;
    }
    if (request.url === '/local/redirect.js') {
      response.writeHead(302, {Location: `http://127.0.0.1:${redirectTarget.address().port}/external.js`});
      response.end(); return;
    }
    response.setHeader('Content-Type', 'text/html');
    response.end(`<!doctype html><html><body><script src="/browser_mod.js"></script><script src="/custom_icons/main.js"></script><home-assistant></home-assistant><script>
      if (window.loadedAssets !== 2) console.error('Registered assets failed to execute');
      const ha = document.querySelector('home-assistant');
      ha.attachShadow({mode:'open'}).innerHTML = '<hui-view><h1>Home dashboard</h1><p>Kitchen light: on</p></hui-view>';
      ha.shadowRoot.innerHTML += '<hui-error-card style="display:none">Hidden loading placeholder</hui-error-card>';
      window.externalAuthSetToken = async (_, token) => {
        if (token.access_token !== 'verification-session') throw Error('Real token reached the page');
        const ws = new WebSocket('ws://' + location.host + '/api/websocket');
        ws.onmessage = async event => {
          const msg = JSON.parse(event.data);
          if (msg.type === 'auth_required') ws.send(JSON.stringify({type:'auth',access_token:token.access_token}));
          if (msg.type === 'auth_ok') {
            ha.hass = {connection:ws,user:{name:'Verification'}};
            ws.send(JSON.stringify({id:1,type:'get_states'}));
            ws.send(JSON.stringify({id:3,type:'weather/subscribe_forecast',entity_id:'weather.fixture',forecast_type:'daily'}));
            ws.send(JSON.stringify({id:4,type:'call_service',domain:'system_log',service:'write'}));
            ws.send(JSON.stringify({id:5,type:'call_service',domain:'persistent_notification',service:'create'}));
            ${external ? "ws.send(JSON.stringify({id:6,type:'lovelace/resources'}));" : ''}
            await fetch('/api/states/light.kitchen', {headers:{Authorization:'Bearer '+token.access_token}});
            await fetch('/api/calendars/calendar.fixture?start=2026-01-01&end=2026-01-02', {headers:{Authorization:'Bearer '+token.access_token}});
            ${bad ? `ws.send(JSON.stringify({id:2,type:'call_service',domain:'light',service:'turn_on'}));
            fetch('/api/services/light/turn_on',{method:'POST'}).catch(()=>{});
            const script=document.createElement('script');script.src='/local/missing.js';document.body.append(script);
            const redirect=document.createElement('script');redirect.src='/local/redirect.js';document.body.append(redirect);
            const unsafe=document.createElement('script');unsafe.src='/unsafe-action.js';document.body.append(unsafe);
            ha.shadowRoot.querySelector('hui-view').innerHTML+='<hui-error-card>Custom element does not exist: missing-card</hui-error-card>';
            console.error('Fixture custom card error');` : ''}
          }
          if (msg.id === 6 && msg.success) {
            const script = document.createElement('script'); script.type = 'module'; script.src = msg.result[0].url; document.body.append(script);
          }
        };
      };
      window.externalApp.getExternalAuth(JSON.stringify({callback:'externalAuthSetToken'}));
    </script></body></html>`);
  });
  const wss = new WebSocketServer({ server });
  wss.on('connection', ws => {
    ws.send(JSON.stringify({type:'auth_required'}));
    ws.on('message', raw => {
      const message = JSON.parse(String(raw));
      upstream.push(message.type);
      if (message.type === 'auth') {
        tokens.push(message.access_token);
        ws.send(JSON.stringify({type: reject ? 'auth_invalid' : 'auth_ok'}));
      } else ws.send(JSON.stringify({id:message.id,type:'result',success:true,result:message.type === 'lovelace/resources'
        ? [{url: 'https://registered.example.test/card/main.js', type: 'module'}] : []}));
    });
  });
  await new Promise(resolve => server.listen(0, '0.0.0.0', resolve));
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ha-browser-test-'));
  try {
    const input = { url: `http://127.0.0.1:${server.address().port}`, path:'/lovelace/0', directory, access_token:'temporary-browser-token',
      resources: {files: ['/browser_mod.js'], directories: ['/custom_icons']} };
    const first = await inspect(input);
    assert.equal(first.status, 'captured', JSON.stringify(first));
    assert.deepEqual(first.errors, []);
    assert.deepEqual(first.blocked, []);
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
    assert(requests.some(url => url.startsWith('/api/calendars/calendar.fixture')));
    const worker = await new Promise((resolve, reject) => {
      const child = execFile(process.platform === 'win32' ? 'python' : 'python3',
        [path.join(__dirname, 'browser_worker_check.py')], {timeout: 130000}, (error, stdout, stderr) => {
          if (error) return reject(new Error(`${error.message}: ${stderr}`));
          try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
        });
      child.stdin.on('error', reject);
      child.stdin.end(JSON.stringify({...input, session_id: 'fixture-session'}));
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
      dns.lookup = async host => {
        assert(['registered.example.test', 'cdn.jsdelivr.net'].includes(host));
        return [{address: '8.8.8.8', family: 4}];
      };
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
    bad = true;
    const second = await inspect(input);
    assert.equal(second.status, 'issues');
    assert(second.errors.some(error => error.includes('Custom element')));
    assert(second.errors.some(error => error.includes('HTTP 404')));
    assert(second.blocked.includes('WebSocket call_service (light.turn_on)'));
    assert(second.blocked.includes('GET /unsafe-action.js'));
    assert(!requests.includes('/unsafe-action.js'));
    assert.equal(second.findings.find(item => item.message === 'Fixture custom card error').count, 2);
    assert(second.blocked.includes('Redirect /local/redirect.js'));
    assert.equal(redirectedRequests, 0);
    assert(!upstream.includes('call_service'));
    assert(!requests.some(url => url.startsWith('/api/services')));
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
