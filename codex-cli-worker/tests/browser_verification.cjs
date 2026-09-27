// Protocol fixture exercising real Chromium, external auth, and network guards.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { WebSocketServer } = require('ws');
const { inspect, allowMessage, allowRequest } = require('../browser.cjs');

async function main() {
  assert(!allowMessage({ type: 'call_service' }));
  assert(!allowMessage({ type: 'lovelace/config/save' }));
  assert(!allowMessage({ type: 'subscribe_events' }));
  for (const type of ['frontend/get_icons', 'render_template', 'sensor/numeric_device_classes']) assert(allowMessage({type}));
  assert(allowMessage({ type: 'subscribe_events', event_type: 'state_changed' }));
  assert(!allowRequest(new URL('http://evil.test/local/card.js'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/webhook/action'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/custom-action/on'), 'http://ha.test', 'GET', '/lovelace/0'));
  assert(!allowRequest(new URL('http://ha.test/api/services/light/turn_on'), 'http://ha.test', 'POST'));
  assert(allowRequest(new URL('http://ha.test/api/image/serve/' + 'a'.repeat(32) + '/512x512'), 'http://ha.test', 'GET'));
  assert(!allowRequest(new URL('http://ha.test/api/image/upload'), 'http://ha.test', 'POST'));

  const upstream = [], requests = [], tokens = [];
  let redirectedRequests = 0;
  const redirectTarget = http.createServer((_request, response) => { redirectedRequests++; response.end('external'); });
  await new Promise(resolve => redirectTarget.listen(0, '127.0.0.1', resolve));
  let bad = false, reject = false;
  const server = http.createServer((request, response) => {
    requests.push(request.url);
    if (request.url.startsWith('/api/states')) {
      assert.equal(request.headers.authorization, 'Bearer temporary-browser-token');
      response.setHeader('Content-Type', 'application/json');
      response.end('{"state":"on"}'); return;
    }
    if (request.url === '/local/missing.js') { response.writeHead(404); response.end(); return; }
    if (request.url === '/local/redirect.js') {
      response.writeHead(302, {Location: `http://127.0.0.1:${redirectTarget.address().port}/external.js`});
      response.end(); return;
    }
    response.setHeader('Content-Type', 'text/html');
    response.end(`<!doctype html><html><body><home-assistant></home-assistant><script>
      const ha = document.querySelector('home-assistant');
      ha.attachShadow({mode:'open'}).innerHTML = '<hui-view><h1>Home dashboard</h1><p>Kitchen light: on</p></hui-view>';
      window.externalAuthSetToken = async (_, token) => {
        if (token.access_token !== 'verification-session') throw Error('Real token reached the page');
        const ws = new WebSocket('ws://' + location.host + '/api/websocket');
        ws.onmessage = async event => {
          const msg = JSON.parse(event.data);
          if (msg.type === 'auth_required') ws.send(JSON.stringify({type:'auth',access_token:token.access_token}));
          if (msg.type === 'auth_ok') {
            ha.hass = {connection:ws,user:{name:'Verification'}};
            ws.send(JSON.stringify({id:1,type:'get_states'}));
            await fetch('/api/states/light.kitchen', {headers:{Authorization:'Bearer '+token.access_token}});
            ${bad ? `ws.send(JSON.stringify({id:2,type:'call_service',domain:'light',service:'turn_on'}));
            fetch('/api/services/light/turn_on',{method:'POST'}).catch(()=>{});
            const script=document.createElement('script');script.src='/local/missing.js';document.body.append(script);
            const redirect=document.createElement('script');redirect.src='/local/redirect.js';document.body.append(redirect);
            ha.shadowRoot.querySelector('hui-view').innerHTML+='<hui-error-card>Custom element does not exist: missing-card</hui-error-card>';
            console.error('Fixture custom card error');` : ''}
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
      } else ws.send(JSON.stringify({id:message.id,type:'result',success:true,result:[]}));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ha-browser-test-'));
  try {
    const input = { url: `http://127.0.0.1:${server.address().port}`, path:'/lovelace/0', directory, access_token:'temporary-browser-token' };
    const first = await inspect(input);
    assert.equal(first.status, 'captured', JSON.stringify(first));
    assert.deepEqual(first.screenshots.map(s => [s.width,s.height]), [[1440,1000],[390,844]]);
    for (const shot of first.screenshots) assert(fs.statSync(shot.file).size > 100);
    assert(tokens.every(token => token === input.access_token));
    bad = true;
    const second = await inspect(input);
    assert.equal(second.status, 'issues');
    assert(second.errors.some(error => error.includes('Custom element')));
    assert(second.errors.some(error => error.includes('HTTP 404')));
    assert(second.blocked.includes('WebSocket call_service'));
    assert(second.blocked.includes('Redirect /local/redirect.js'));
    assert.equal(redirectedRequests, 0);
    assert(!upstream.includes('call_service'));
    assert(!requests.some(url => url.startsWith('/api/services')));
    reject = true;
    assert.equal((await inspect(input)).status, 'unavailable');
    console.log('Dashboard browser checks passed: two viewports, external auth, readback, blocked writes, missing custom card/resource, auth failure, cleanup.');
  } finally {
    for (const client of wss.clients) client.terminate();
    await new Promise(resolve => wss.close(resolve));
    await new Promise(resolve => server.close(resolve));
    await new Promise(resolve => redirectTarget.close(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
