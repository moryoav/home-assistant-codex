// Deterministic network-policy tests. No requests leave this process.
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {ResourcePolicy, createExternalFetcher, publicAddress} = require('../browser_resources.cjs');

/**
 * Check which local and external resources the policy allows, then run the
 * external fetcher against fake DNS and HTTPS: public addresses only,
 * redirects, content types, compression, and the request and size limits.
 */
async function main() {
  const policy = new ResourcePolicy('http://ha.test:8123', {
    files: ['/browser_mod.js', '/api/action.js', '//unsafe.test/card.js'],
    directories: ['/custom_icons', '/oref_alert_internal_static'],
    extra_urls: ['https://assets.example.test/cards/main.js'],
  });
  /** Ask the policy whether a path on the Home Assistant origin may load. */
  const local = (path, method = 'GET', type = 'script') => policy.local(new URL(path, policy.origin), method, type);
  /** Ask the policy whether an external URL may load. */
  const remote = (url, method = 'GET', type = 'script') => policy.remote(new URL(url), method, type);
  assert(local('/browser_mod.js?v=3'));
  assert(local('/custom_icons/nested/icons.json', 'GET', 'fetch'));
  for (const path of ['/unregistered.js', '/api/action.js', '/custom_icons_evil/card.js',
    '/custom_icons/action', '/custom_icons/../action.js', '/custom_icons/%2e%2e/action.js']) assert(!local(path), path);
  assert(!local('/browser_mod.js', 'POST'));
  assert(!local('/browser_mod.js', 'GET', 'document'));
  assert(!local('https://evil.test/browser_mod.js'));
  assert(remote('https://assets.example.test/cards/main.js'));
  assert(remote('https://assets.example.test/cards/chunk.js?v=2'));
  assert(!remote('https://assets.example.test/other.js'));
  assert(!remote('https://assets.example.test/cards/action'));
  for (const url of ['https://fonts.googleapis.com/css?family=Raleway',
    'https://fonts.gstatic.com/s/raleway/font.woff2',
    'https://cdn.jsdelivr.net/npm/daisyui@latest/dist/full.css',
    'https://fastly.jsdelivr.net/npm/daisyui@latest/dist/full.css',
    'https://cdnjs.cloudflare.com/ajax/libs/library/1/index.js',
    'https://unpkg.com/package/card.js']) assert(remote(url), url);
  for (const host of ['cdn.jsdelivr.net', 'fastly.jsdelivr.net']) {
    for (const file of ['lit@3.2.1/+esm', '@lit/reactive-element@2.0.4/+esm',
      'lit-element@4.1.0/lit-element.js/+esm'])
      assert(remote(`https://${host}/npm/${file}`, 'GET', 'script'), file);
    const esm = `https://${host}/npm/lit@3.2.1/+esm`;
    assert(!remote(esm, 'POST', 'script'));
    assert(!remote(esm, 'GET', 'document'));
    for (const pathname of ['/gh/lit/dist@3/+esm', '/npm/+esm', '/npm/lit@3.2.1/+esm/extra',
      '/npm/lit@3.2.1/+esm-extra', '/npm/lit@3.2.1/%2besm'])
      assert(!remote(`https://${host}${pathname}`, 'GET', 'script'), pathname);
  }
  assert(!remote('https://other.example.test/npm/lit@3.2.1/+esm', 'GET', 'script'));
  for (const url of ['http://cdn.jsdelivr.net/npm/card.js', 'https://cdn.jsdelivr.net:8443/npm/card.js',
    'https://user:password@cdn.jsdelivr.net/npm/card.js', 'https://cdn.jsdelivr.net.evil.test/npm/card.js',
    'https://cdn.jsdelivr.net/npm/card.js%2faction', 'https://cdn.jsdelivr.net/api/action.js',
    'https://cdn.jsdelivr.net/auth/card.js', 'https://cdn.jsdelivr.net/npm/card.js']) {
    if (url.endsWith('/npm/card.js') && url === 'https://cdn.jsdelivr.net/npm/card.js') {
      assert(!remote(url, 'POST')); assert(!remote(url, 'GET', 'document'));
    } else assert(!remote(url), url);
  }
  for (const url of ['https://127.0.0.1/a.js', 'https://[::1]/a.js', 'https://device.local/a.js', 'https://ha.lan/a.js']) {
    policy.register(url); assert(!remote(url), url);
  }
  policy.cssDependencies(Buffer.from('@import "https://styles.example.test/theme.css"; @font-face {src:url(https://fonts.example.test/a.woff2)} .x {background:url(https://evil.test/api/action)}'), 'https://assets.example.test/cards/main.css');
  assert(remote('https://fonts.example.test/a.woff2', 'GET', 'font'));
  assert(remote('https://styles.example.test/theme.css', 'GET', 'stylesheet'));
  // Imported CSS grants only the referenced URL, not its directory.
  for (const [file, type] of [['script.js', 'script'], ['data.json', 'fetch'],
    ['data.json', 'xhr'], ['other.css', 'stylesheet']])
    assert(!remote(`https://styles.example.test/${file}`, 'GET', type), file);
  policy.cssDependencies(Buffer.from('@import "nested/colors.css"; .x {background:url(images/icon.svg)} @font-face {src:url(fonts/main.woff2)}'), 'https://styles.example.test/theme.css');
  assert(remote('https://styles.example.test/nested/colors.css', 'GET', 'stylesheet'));
  assert(remote('https://styles.example.test/images/icon.svg', 'GET', 'image'));
  assert(remote('https://styles.example.test/fonts/main.woff2', 'GET', 'font'));
  assert(!remote('https://styles.example.test/nested/script.js', 'GET', 'script'));
  // Explicit Core/Lovelace registrations retain their documented directory access.
  policy.register('https://registered-styles.example.test/theme.css');
  assert(remote('https://registered-styles.example.test/related.css', 'GET', 'stylesheet'));
  assert(remote('https://assets.example.test/cards/chunk.js', 'GET', 'script'));
  assert(!remote('https://evil.test/api/action'));
  for (const ip of ['127.0.0.1', '10.1.2.3', '192.168.1.2', '172.30.33.6', '169.254.169.254',
    '100.64.0.1', '0.0.0.0', '224.0.0.1', '::1', '::ffff:8.8.8.8', 'fc00::1', 'fe80::1', '2001:db8::1', '2002:7f00:1::', '3fff::1']) assert(!publicAddress(ip), ip);
  assert(publicAddress('8.8.8.8')); assert(publicAddress('2606:4700:4700::1111'));

  const calls = [];
  let answers = [{address: '8.8.8.8', family: 4}], responses = [], dnsCalls = 0;
  const dependencies = {
    /** Count the DNS lookup and answer with the addresses the test set. */
    lookup: async () => { dnsCalls++; return answers; },
    /**
     * Stand in for https.request: check that the fetcher verifies
     * certificates, sends only its own plain headers, and pins the resolved
     * address, then answer with the next queued response.
     */
    request: (url, options, callback) => {
      calls.push({url: url.href, options});
      assert.equal(options.rejectUnauthorized, true);
      assert.equal(options.agent, false);
      assert.deepEqual(Object.keys(options.headers).sort(), ['Accept', 'Accept-Encoding', 'User-Agent']);
      options.lookup(url.hostname, {}, (error, address, family) => {
        assert.equal(error, null); assert.equal(address, '8.8.8.8'); assert.equal(family, 4);
      });
      const req = new EventEmitter();
      req.destroy = error => { req.emit('error', error); req.emit('close'); };
      req.end = () => queueMicrotask(() => {
        const spec = responses.shift(); assert(spec, 'Unexpected request');
        const response = new EventEmitter();
        response.statusCode = spec.status || 200; response.headers = spec.headers || {'content-type': 'text/css'};
        callback(response);
        response.emit('data', spec.body || Buffer.from('body{color:red}'));
        response.emit('end'); req.emit('close');
      });
      return req;
    },
  };
  let fetch = createExternalFetcher(policy, dependencies);
  responses = [{headers: {'content-type': 'text/css', 'set-cookie': 'secret=1'},
    body: Buffer.from('@font-face{src:url(https://font2.example.test/f.woff2)}')}];
  const result = await fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet');
  assert.equal(result.status, 200); assert.equal(result.headers['set-cookie'], undefined);
  assert(remote('https://font2.example.test/f.woff2', 'GET', 'font'));
  responses = [{headers: {'content-type': 'text/css', 'content-encoding': 'gzip'},
    body: require('node:zlib').gzipSync(Buffer.from('@font-face{src:url(https://font3.example.test/f.woff2)}'))}];
  const compressed = await fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet');
  assert.equal(compressed.headers['content-encoding'], undefined);
  assert(remote('https://font3.example.test/f.woff2', 'GET', 'font'));
  answers = [{address: '127.0.0.1', family: 4}];
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /non-public/);
  assert.equal(calls.length, 2);
  answers = [{address: '8.8.8.8', family: 4}, {address: '192.168.1.1', family: 4}];
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /non-public/);
  answers = [{address: '8.8.8.8', family: 4}];
  responses = [{status: 302, headers: {location: 'http://127.0.0.1/private.js'}}];
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /not allowed/);
  const before = dnsCalls;
  responses = [{status: 302, headers: {location: 'https://fonts.googleapis.com/css2'}}, {}];
  await fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet');
  assert.equal(dnsCalls - before, 2, 'Redirects must resolve and validate DNS again');
  responses = [{headers: {'content-type': 'text/html'}}];
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /supported asset/);
  responses = [{body: Buffer.alloc(8 * 1024 * 1024 + 1)}];
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /size limit/);
  responses = Array.from({length: 4}, () => ({status: 302, headers: {location: 'https://fonts.googleapis.com/css'}}));
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /redirect limit/);
  fetch = createExternalFetcher(policy, {...dependencies, lookup: async () => {
    if (responses.length === 1) return [{address: '127.0.0.1', family: 4}];
    return [{address: '8.8.8.8', family: 4}];
  }});
  responses = [{status: 302, headers: {location: 'https://fonts.googleapis.com/css2'}}, {}];
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /non-public/);
  assert.equal(responses.length, 1, 'A DNS rebinding redirect must never connect');
  fetch = createExternalFetcher(policy, dependencies);
  responses = Array.from({length: 128}, () => ({}));
  for (let i = 0; i < 128; i++) await fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet');
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /request limit/);
  fetch = createExternalFetcher(policy, dependencies);
  responses = Array.from({length: 5}, () => ({headers: {'content-type':'image/png'}, body: Buffer.alloc(8 * 1024 * 1024)}));
  for (let i = 0; i < 4; i++) await fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet');
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /size limit/);
  fetch = createExternalFetcher(policy, dependencies);
  responses = [{headers: {'content-type':'text/css','content-encoding':'gzip'}, body: require('node:zlib').gzipSync(Buffer.alloc(8 * 1024 * 1024 + 1))}];
  await assert.rejects(fetch('https://fonts.googleapis.com/css', 'GET', 'stylesheet'), /oversized compressed/);
  console.log('Resource policy and credential-free HTTPS transport checks passed.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
