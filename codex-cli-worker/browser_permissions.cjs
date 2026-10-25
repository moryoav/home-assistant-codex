// User-granted exceptions for one Core installation and dashboard. This module
// excludes known writes, credentials, signing, and camera signaling.
const { createHash } = require('node:crypto');
const MAX_CANDIDATES = 16;
const MAX_REQUEST = 1200;
const TYPE = /^[a-z][a-z0-9_]*(?:\/[a-z][a-z0-9_]*){0,5}$/;
const SENSITIVE = /token|secret|password|credential|authorization|signature|api_?key|authsig/i;
const WRITE = /(?:^|[_/])(?:set|save|create|update|delete|remove|add|install|download|execute|exec|run|call|fire|start|stop|restart|reload|reboot|register|connect|disconnect|press|trigger|toggle|turn|send|offer|candidate|publish|write)(?:$|[_/])/i;

/** Serialize JSON with stable object-key order, preserving arrays and values. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object')
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}

/** Refuse credentials and excessive nesting rather than hiding approval details. */
function inspectable(value, depth = 0) {
  if (depth > 12) return false;
  if (typeof value === 'number') return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
  if (typeof value === 'string') return !/bearer\s|(?:token|password|secret|authsig)\s*[=:]/i.test(value);
  if (!value || typeof value !== 'object') return true;
  return Object.entries(value).every(([key, item]) => !SENSITIVE.test(key) && inspectable(item, depth + 1));
}

/** Accept only bounded raster coordinates and the map-specific token parameter. */
function raster(url) {
  const match = /^\/api\/map_tiles\/raster\/(\d{1,2})\/(\d{1,7})\/(\d{1,7})\.png$/.exec(url.pathname);
  return !!match && Number(match[1]) <= 19 && Number(match[2]) < 2 ** Number(match[1]) &&
    Number(match[3]) < 2 ** Number(match[1]) && [...url.searchParams.keys()].every(key => key === 'token') &&
    url.searchParams.getAll('token').length <= 1;
}

/** Build a rule only for eligible reads; an unknown handler is still a trust decision. */
function rule(kind, value) {
  let request;
  if (kind === 'map_tiles') {
    request = { type: 'map_tiles/raster' };
  } else if (kind === 'websocket') {
    if (!value || typeof value.type !== 'string' || !TYPE.test(value.type) || value.type.length > 160 || WRITE.test(value.type) ||
        /^(auth|config|hassio|cloud|stream|backup|person|automation|script)(?:\/|$)/.test(value.type) ||
        /^camera\/(?:webrtc|stream)(?:\/|$)/.test(value.type) ||
        SENSITIVE.test(value.type) || !/(?:^|\/)(?:get|list|subscribe)(?:_|$)|\/(?:config|info|capabilities)$/.test(value.type)) return null;
    request = Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'id'));
  } else if (kind === 'http') {
    if (!value || !['GET', 'HEAD'].includes(value.method) || typeof value.path !== 'string' ||
        !/^\/[a-zA-Z0-9_./-]+$/.test(value.path) || /(?:^|\/)\.{1,2}(?:\/|$)/.test(value.path) ||
        value.path.includes('//') || value.path.length > 512 || WRITE.test(value.path) || SENSITIVE.test(value.path) ||
        /^\/(?:auth|api\/(?:auth|services|events|config|hassio|webhook|camera|codex_cli|stream|backup|map_tiles))(?:\/|$)/.test(value.path) ||
        !Array.isArray(value.query) || value.query.length > 12 ||
        value.query.some(pair => !Array.isArray(pair) || pair.length !== 2 || pair.some(item => typeof item !== 'string') || SENSITIVE.test(pair[0]))) return null;
    request = { method: value.method, path: value.path, query: value.query };
  } else return null;
  if (!inspectable(request)) return null;
  const snapshot = canonical(request);
  if (Buffer.byteLength(snapshot) > MAX_REQUEST) return null;
  const id = createHash('sha256').update(`${kind}\n${snapshot}`).digest('hex');
  return { id, kind, request: snapshot };
}

/** Revalidate saved rules, including their fingerprint, at the enforcement boundary. */
function validate(saved) {
  try {
    if (!saved || typeof saved.request !== 'string' || saved.request.length > MAX_REQUEST) return null;
    const normalized = rule(saved.kind, JSON.parse(saved.request));
    return normalized?.id === saved.id && normalized.request === saved.request ? normalized : null;
  } catch (_) { return null; }
}

/** Bound metadata while always correlating token requests and rejecting reused tracked IDs. */
function trackCommand(commands, message) {
  const tokenRequest = message.type === 'map_tiles/access_token';
  if (!Number.isSafeInteger(message.id) || message.id < 0 || commands.has(message.id) ||
      (tokenRequest && commands.size >= 500)) return false;
  if (commands.size < 500) commands.set(message.id, message.type);
  return true;
}

/** Keep complete capture output within the worker's limit, dropping duplicates first. */
function boundedResult(result) {
  const fits = () => Buffer.byteLength(JSON.stringify(result)) < 60 * 1024;
  if (fits()) return result;
  // These arrays duplicate structured findings; screenshots also duplicate cards.
  result.errors = [];
  result.blocked = [];
  for (const shot of result.screenshots || []) delete shot.error_cards;
  while (!fits() && result.permission_candidates?.length) {
    result.permission_candidates.pop();
    result.permission_candidates_omitted = (result.permission_candidates_omitted || 0) + 1;
  }
  while (!fits() && result.findings?.length) {
    result.findings.pop();
    result.findings_omitted = (result.findings_omitted || 0) + 1;
  }
  if (!fits()) return {status: 'unavailable', message: 'Dashboard evidence exceeded its output limit.', screenshots: []};
  return result;
}

/** Match grants and aggregate blocked candidates across both viewport captures. */
class BrowserPermissions {
  /** Load only validated grants and start a bounded, empty evidence collection. */
  constructor(grants = []) {
    this.grants = new Set((Array.isArray(grants) ? grants : []).slice(0, 128).map(validate).filter(Boolean).map(item => item.id));
    this.candidates = new Map();
    this.used = new Set();
    this.omitted = 0;
  }

  /** Record one eligible blocked request, without persisting tokens or request IDs. */
  check(item, viewport) {
    if (!item) return false;
    if (this.grants.has(item.id)) { this.used.add(item.id); return true; }
    if (!this.candidates.has(item.id)) {
      if (this.candidates.size < MAX_CANDIDATES)
        this.candidates.set(item.id, { id: item.id, kind: item.kind, request: item.request, count: 0, viewports: [] });
      else this.omitted++;
    }
    const candidate = this.candidates.get(item.id);
    if (candidate) {
      candidate.count++;
      if (viewport && !candidate.viewports.includes(viewport)) candidate.viewports.push(viewport);
    }
    return false;
  }

  /** Authorize one exact WebSocket read or the bounded map capability. */
  message(message, viewport) {
    if (!message || typeof message !== 'object' || Array.isArray(message)) return false;
    const map = message.type === 'map_tiles/access_token' && Object.keys(message).every(key => ['id', 'type'].includes(key));
    return this.check(map ? rule('map_tiles') : rule('websocket', message), viewport);
  }

  /** Authorize same-origin GET/HEAD requests only; external and signed URLs stay blocked. */
  http(url, origin, method, viewport) {
    if (url.origin !== origin || !['GET', 'HEAD'].includes(method) || /%|\\/.test(url.pathname)) return false;
    if (raster(url)) return this.check(rule('map_tiles'), viewport);
    if (url.pathname.startsWith('/api/map_tiles/')) return false;
    const query = [...url.searchParams.entries()].sort((a, b) => canonical(a).localeCompare(canonical(b)));
    return this.check(rule('http', { method, path: url.pathname, query }), viewport);
  }

  /** Return bounded evidence without including grants or any token values. */
  evidence() {
    return { permission_candidates: [...this.candidates.values()], permissions_used: [...this.used], permission_candidates_omitted: this.omitted };
  }
}

module.exports = { BrowserPermissions, canonical, rule, validate, raster, trackCommand, boundedResult };
