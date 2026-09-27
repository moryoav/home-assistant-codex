// Resource-only compatibility. External requests use a separate, credential-free
// HTTPS client with public DNS addresses pinned for each connection.
const dns = require('node:dns').promises;
const https = require('node:https');
const net = require('node:net');
const zlib = require('node:zlib');

const ASSET = /\.(?:m?js|css|woff2?|ttf|otf|eot|svg|png|jpe?g|gif|webp|avif|ico|json)$/i;
const CSS_ASSET = /\.(?:css|woff2?|ttf|otf|eot|svg|png|jpe?g|gif|webp|avif|ico)$/i;
const RESOURCE_TYPES = new Set(['script', 'stylesheet', 'font', 'image', 'fetch', 'xhr']);
const PRIVATE_V4 = new net.BlockList();
for (const [ip, bits] of [['0.0.0.0',8], ['10.0.0.0',8], ['100.64.0.0',10], ['127.0.0.0',8],
  ['169.254.0.0',16], ['172.16.0.0',12], ['192.0.0.0',24], ['192.0.2.0',24],
  ['192.88.99.0',24], ['192.168.0.0',16], ['198.18.0.0',15], ['198.51.100.0',24],
  ['203.0.113.0',24], ['224.0.0.0',4], ['240.0.0.0',4]]) PRIVATE_V4.addSubnet(ip, bits);
const GLOBAL_V6 = new net.BlockList();
GLOBAL_V6.addSubnet('2000::', 3, 'ipv6');
const PRIVATE_V6 = new net.BlockList();
for (const [ip,bits] of [['2001::',23], ['2001:db8::',32], ['2002::',16], ['3fff::',20]]) PRIVATE_V6.addSubnet(ip,bits,'ipv6');

function publicAddress(address) {
  const family = net.isIP(address);
  return family === 4 ? !PRIVATE_V4.check(address) : family === 6 &&
    GLOBAL_V6.check(address, 'ipv6') && !PRIVATE_V6.check(address, 'ipv6');
}

function publicURL(url) {
  return url.protocol === 'https:' && !url.username && !url.password && !url.port &&
    !net.isIP(url.hostname.replace(/^\[|\]$/g, '')) && !url.hostname.endsWith('.') &&
    !/(?:^|\.)(?:localhost|local|internal|home|lan)$/.test(url.hostname);
}

function cdnAsset(url) {
  if (url.hostname === 'fonts.googleapis.com') return ['/css', '/css2'].includes(url.pathname);
  if (url.hostname === 'fonts.gstatic.com') return url.pathname.startsWith('/s/') && CSS_ASSET.test(url.pathname);
  if (['cdn.jsdelivr.net', 'fastly.jsdelivr.net'].includes(url.hostname))
    return /^\/(?:npm|gh)\//.test(url.pathname) && ASSET.test(url.pathname);
  if (url.hostname === 'cdnjs.cloudflare.com') return url.pathname.startsWith('/ajax/libs/') && ASSET.test(url.pathname);
  if (url.hostname === 'unpkg.com') return ASSET.test(url.pathname);
  return false;
}

class ResourcePolicy {
  constructor(origin, resources = {}) {
    this.origin = origin;
    this.files = new Set();
    this.directories = new Set();
    this.external = new Set();
    this.externalDirectories = new Set();
    for (const key of ['files', 'directories']) {
      for (const value of (Array.isArray(resources[key]) ? resources[key] : []).slice(0, 256)) {
        if (typeof value === 'string' && /^\/(?!\/)/.test(value) && !/[?%#\\{}]/.test(value) && value.length <= 1024)
          this[key].add(value.replace(/\/$/, ''));
      }
    }
    for (const value of (Array.isArray(resources.extra_urls) ? resources.extra_urls : []).slice(0, 256)) this.register(value);
  }

  register(value, allowDirectory = true) {
    if (typeof value !== 'string' || value.length > 2048 || this.external.size >= 256) return;
    let url;
    try { url = new URL(value, this.origin); } catch { return; }
    // Same-origin permissions come only from Core's actual static route registry.
    if (url.origin === this.origin || !publicURL(url)) return;
    url.hash = '';
    this.external.add(url.href);
    if (allowDirectory && /\.(?:m?js|css)$/i.test(url.pathname))
      this.externalDirectories.add(new URL('.', url).href);
  }

  local(url, method, type) {
    if (url.origin !== this.origin || !['GET','HEAD'].includes(method) || !RESOURCE_TYPES.has(type) ||
        /^\/api(?:\/|$)/.test(url.pathname) || /[%\\]/.test(url.pathname) || !ASSET.test(url.pathname)) return false;
    return this.files.has(url.pathname) || [...this.directories].some(prefix => prefix && url.pathname.startsWith(prefix + '/'));
  }

  remote(url, method, type) {
    if (url.origin === this.origin || !['GET','HEAD'].includes(method) || !RESOURCE_TYPES.has(type) ||
        !publicURL(url) || /[%\\]/.test(url.pathname) || /^\/(?:api|auth)(?:\/|$)/.test(url.pathname)) return false;
    const clean = new URL(url); clean.hash = '';
    return this.external.has(clean.href) || cdnAsset(clean) ||
      (ASSET.test(clean.pathname) && [...this.externalDirectories].some(prefix => clean.href.startsWith(prefix)));
  }

  cssDependencies(body, source) {
    // Only fetched stylesheets can extend the resource set, never page messages
    // or arbitrary request parameters. Each resulting URL still needs public DNS.
    const text = body.toString('utf8');
    const pattern = /url\(\s*(?:"([^"]+)"|'([^']+)'|([^\s)]+))\s*\)|@import\s+["']([^"']+)["']/gi;
    for (const match of text.matchAll(pattern)) {
      let url;
      try { url = new URL(match[1] || match[2] || match[3] || match[4], source); } catch { continue; }
      // CSS references permit only that asset, never sibling scripts or data.
      if (CSS_ASSET.test(url.pathname) || cdnAsset(url)) this.register(url.href, false);
    }
  }
}

function createExternalFetcher(policy, dependencies = {}) {
  const lookup = dependencies.lookup || ((...args) => dns.lookup(...args));
  const request = dependencies.request || ((...args) => https.request(...args));
  let requests = 0, bytes = 0;
  return async function fetchResource(initial, method, type) {
    let url = new URL(initial);
    const deadline = Date.now() + 15000;
    for (let hop = 0; hop < 4; hop++) {
      if (!policy.remote(url, method, type)) throw Error('External resource destination is not allowed');
      if (++requests > 128) throw Error('External resource request limit reached');
      let timer;
      const addresses = await Promise.race([
        lookup(url.hostname, {all: true, verbatim: true}),
        new Promise((_, reject) => { timer = setTimeout(() => reject(Error('External resource DNS timeout')), Math.max(1, deadline - Date.now())); }),
      ]).finally(() => clearTimeout(timer));
      if (!addresses.length || addresses.some(item => !publicAddress(item.address)))
        throw Error('External resource resolved to a non-public address');
      const address = addresses[0];
      const result = await new Promise((resolve, reject) => {
        const chunks = []; let size = 0;
        // No browser cookies, Authorization, Referer, Origin, or custom headers.
        const req = request(url, {method, agent: false, rejectUnauthorized: true,
          servername: url.hostname, headers: {'Accept': '*/*', 'Accept-Encoding': 'identity',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'},
          lookup: (_host, options, callback) => options.all ? callback(null, [address]) : callback(null, address.address, address.family),
        }, response => {
          response.on('error', reject);
          response.on('data', chunk => {
            size += chunk.length; bytes += chunk.length;
            if (size > 8 * 1024 * 1024 || bytes > 32 * 1024 * 1024) req.destroy(Error('External resource size limit reached'));
            else chunks.push(chunk);
          });
          response.on('end', () => resolve({status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks)}));
        });
        const timeout = setTimeout(() => req.destroy(Error('External resource timeout')), Math.max(1, deadline - Date.now()));
        req.on('error', reject);
        req.on('close', () => clearTimeout(timeout));
        req.end();
      });
      if (result.status >= 300 && result.status < 400) {
        if (!result.headers.location) throw Error('External resource redirect has no destination');
        url = new URL(result.headers.location, url);
        continue; // Recheck the policy and DNS on every hop, including same-host redirects.
      }
      const mime = String(result.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
      if (result.status < 400 && !(mime === 'text/css' || /(?:java|ecma)script/.test(mime) ||
          mime.startsWith('font/') || mime.startsWith('image/') ||
          ['application/octet-stream','application/json','application/font-woff','application/vnd.ms-fontobject','application/x-font-ttf'].includes(mime)))
        throw Error('External response is not a supported asset');
      const encoding = result.headers['content-encoding'];
      if (encoding && encoding !== 'identity' && method !== 'HEAD') {
        const decode = {gzip: zlib.gunzipSync, br: zlib.brotliDecompressSync, deflate: zlib.inflateSync}[encoding];
        if (!decode) throw Error('Unsupported external resource encoding');
        let decoded;
        try { decoded = decode(result.body, {maxOutputLength: 8 * 1024 * 1024}); }
        catch { throw Error('Invalid or oversized compressed external resource'); }
        bytes += Math.max(0, decoded.length - result.body.length);
        if (bytes > 32 * 1024 * 1024) throw Error('External resource size limit reached');
        result.body = decoded;
      }
      if (mime === 'text/css' && result.status < 400) policy.cssDependencies(result.body, url);
      // Never install remote cookies or forward transport framing into fulfillment.
      const headers = {};
      for (const key of ['content-type','access-control-allow-origin','cross-origin-resource-policy'])
        if (typeof result.headers[key] === 'string') headers[key] = result.headers[key];
      return {...result, headers};
    }
    throw Error('External resource redirect limit reached');
  };
}

module.exports = {ResourcePolicy, createExternalFetcher, publicAddress};
