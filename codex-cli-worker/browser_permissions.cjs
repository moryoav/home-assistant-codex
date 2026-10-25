// User-granted exceptions for one Core installation and dashboard. This module
// excludes known writes, credentials, signing, and camera signaling.
// browser_permission_policy.json holds the rules the worker also enforces.
const { createHash } = require('node:crypto');
const POLICY = require('./browser_permission_policy.json');

const LIMITS = POLICY.limits;
const MAX_CANDIDATES = LIMITS.candidates;
const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const any = list => list.map(escape).join('|');
const TYPE = new RegExp(`^(?:${POLICY.type_pattern})$`);
const HTTP_PATH = new RegExp(`^(?:${POLICY.http_path_pattern})$`);
const SENSITIVE = new RegExp(any(POLICY.sensitive_names), 'i');
const CREDENTIAL = new RegExp(`bearer${POLICY.whitespace}|(?:${any(POLICY.credential_value_names)})${POLICY.whitespace}*[=:]`, 'i');
const READ = new RegExp(`(?:^|/)(?:${any(POLICY.read_prefixes)})(?:_|$)|/(?:${any(POLICY.read_suffixes)})$`);
const DENIED_TYPE = new RegExp(`^(?:${any(POLICY.denied_websocket_prefixes)})(?:/|$)`);
const WRITE_WORDS = new Set(POLICY.write_words);

/** Split a command or path into lowercase words at punctuation and camelCase. */
function words(text) {
  return text.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
}

/** Return whether any word of a command or path names a known write. */
function writes(text) {
  return words(text).some(word => WRITE_WORDS.has(word));
}

/** Serialize JSON with stable object-key order, preserving arrays and values. */
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object')
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}

/** Order strings by code unit, so a rule's fingerprint never depends on the locale. */
const byCodeUnit = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

/** Refuse credentials and excessive nesting rather than hiding approval details. */
function inspectable(value, depth = 0) {
  if (depth > LIMITS.depth) return false;
  if (typeof value === 'number') return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
  if (typeof value === 'string') return !CREDENTIAL.test(value);
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

/**
 * Build a rule only for eligible reads; an unknown handler is still a trust
 * decision. Commands the controller restricts by name or parameters (knob
 * handlers, icon sets, event subscriptions, Browser Mod) are never eligible, so
 * an approval cannot widen those restrictions.
 */
function rule(kind, value) {
  let request;
  if (kind === 'map_tiles') {
    request = { type: 'map_tiles/raster' };
  } else if (kind === 'websocket') {
    if (!value || typeof value.type !== 'string' || value.type.length > LIMITS.type_length || !TYPE.test(value.type) ||
        writes(value.type) || DENIED_TYPE.test(value.type) || POLICY.denied_websocket_types.includes(value.type) ||
        SENSITIVE.test(value.type) || !READ.test(value.type)) return null;
    request = Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'id'));
  } else if (kind === 'http') {
    if (!value || !['GET', 'HEAD'].includes(value.method) || typeof value.path !== 'string' ||
        value.path.length > LIMITS.path_length || !HTTP_PATH.test(value.path) || /(?:^|\/)\.{1,2}(?:\/|$)/.test(value.path) ||
        value.path.includes('//') || writes(value.path) || SENSITIVE.test(value.path) ||
        POLICY.denied_http_prefixes.some(prefix => value.path === prefix || value.path.startsWith(`${prefix}/`)) ||
        !Array.isArray(value.query) || value.query.length > LIMITS.query_pairs ||
        value.query.some(pair => !Array.isArray(pair) || pair.length !== 2 || pair.some(item => typeof item !== 'string') || SENSITIVE.test(pair[0]))) return null;
    request = { method: value.method, path: value.path, query: value.query };
  } else return null;
  if (!inspectable(request)) return null;
  const snapshot = canonical(request);
  if (Buffer.byteLength(snapshot) > LIMITS.request_bytes) return null;
  const id = createHash('sha256').update(`${kind}\n${snapshot}`).digest('hex');
  return { id, kind, request: snapshot };
}

/** Explain a controller refusal without treating every unrecognized read as a write. */
function websocketBlockReason(message) {
  const type = message.type;
  if (typeof type !== 'string') return 'Invalid request type. Approval is unavailable.';
  if (type === 'browser_mod/connect')
    return 'Browser Mod can update stored browser details and attach a command channel. It needs separate support; approval is unavailable.';
  if (type === 'browser_mod' || type.startsWith('browser_mod/'))
    return 'Browser Mod needs separate support for this inspection. Approval is unavailable.';
  if (type === 'subscribe_events')
    return message.event_type === 'zha_event'
      ? 'Listening is read-only, but Home Assistant restricts this event stream to administrators. User approval cannot grant administrator access.'
      : 'This event stream is outside the supported event allowlist. Listening is not a write, but approval is unavailable.';
  if (rule('websocket', message))
    return type === 'subscribe_condition'
      ? 'This condition is outside the automatic built-in read checks. Review this exact request for approval.'
      : 'This request is outside the automatic read allowlist. Review this exact request for approval.';
  if (type === 'knob_swipe_navigation/navigation_result')
    return 'Reporting navigation results updates integration state. Approval is unavailable in this observational browser.';
  if (type === 'knob_swipe_navigation/subscribe_rotations' || type === 'knob_swipe_navigation/config')
    return 'This read handler accepts only id and type. Extra parameters are unsupported; approval is unavailable.';
  if (type === 'auth' || type.startsWith('auth/'))
    return 'Authentication changes and URL signing need separate support. Approval is unavailable.';
  if (type === 'camera/stream' || type === 'camera/webrtc' || type.startsWith('camera/webrtc/'))
    return 'Camera streaming and signaling need separate support. Approval is unavailable.';
  if (writes(type)) return 'Device actions and write-like commands are not supported by this observational browser. Approval is unavailable.';
  return 'This request is outside the supported read policy or cannot be safely reviewed. Approval is unavailable; this does not establish that it is a write.';
}

/** Revalidate saved rules, including their fingerprint, at the enforcement boundary. */
function validate(saved) {
  try {
    if (!saved || typeof saved.request !== 'string' || saved.request.length > LIMITS.request_bytes) return null;
    const normalized = rule(saved.kind, JSON.parse(saved.request));
    return normalized?.id === saved.id && normalized.request === saved.request ? normalized : null;
  } catch (_) { return null; }
}

/**
 * Correlate Core responses with requests the way Core numbers them: request
 * IDs must increase on each connection, so one response never matches two
 * requests. Only unanswered requests are remembered.
 */
class CommandTracker {
  /** Start a connection with no requests and Core's initial ID. */
  constructor(limit = 1000, conditionBudget = {active: 0, limit: LIMITS.condition_subscriptions}) {
    this.lastId = 0;
    this.limit = limit;
    this.pending = new Map();
    this.conditionBudget = conditionBudget;
    this.conditions = new Set();
    this.unsubscribing = new Map();
  }

  /** Track a request, or return why it cannot be forwarded unambiguously. */
  accept(message) {
    if (!Number.isSafeInteger(message.id) || message.id <= this.lastId) return 'invalid or reused request ID';
    if (this.pending.size >= this.limit) return 'too many unanswered requests';
    if (message.type === 'subscribe_condition' && this.conditionBudget.active >= this.conditionBudget.limit)
      return `too many active condition subscriptions (limit ${this.conditionBudget.limit})`;
    this.lastId = message.id;
    this.pending.set(message.id, message.type);
    if (message.type === 'subscribe_condition') {
      this.conditions.add(message.id);
      this.conditionBudget.active++;
    }
    if (message.type === 'unsubscribe_events' && this.conditions.has(message.subscription))
      this.unsubscribing.set(message.id, message.subscription);
    return null;
  }

  /** Return the type of the request a response answers. */
  type(id) {
    return this.pending.get(id);
  }

  /** Forget a request once Core answered it; subscription events keep only the ID. */
  settle(message) {
    if (message.type !== 'result' && message.type !== 'pong') return;
    if (message.type === 'result') {
      const subscription = message.success === false ? message.id :
        message.success === true ? this.unsubscribing.get(message.id) : undefined;
      if (this.conditions.delete(subscription)) this.conditionBudget.active--;
    }
    this.pending.delete(message.id);
    this.unsubscribing.delete(message.id);
  }

  /** Release all condition slots and request records when either socket closes. */
  close() {
    this.conditionBudget.active -= this.conditions.size;
    this.conditions.clear();
    this.unsubscribing.clear();
    this.pending.clear();
  }
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
    // Saved rules plus the one-off grants an exchange's retries can carry.
    const limit = LIMITS.saved_rules + LIMITS.candidates * LIMITS.retries;
    this.grants = new Set((Array.isArray(grants) ? grants : []).slice(0, limit).map(validate).filter(Boolean).map(item => item.id));
    this.candidates = new Map();
    this.used = new Set();
    this.omitted = 0;
  }

  /** Record one eligible blocked request without its request ID; credential-like fields make it ineligible. */
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
    const query = [...url.searchParams.entries()];
    // Malformed escapes decode to U+FFFD, so one grant would match several raw queries.
    if (query.some(pair => pair.some(item => item.includes('�')))) return false;
    // Stable key sorting preserves which repeated value a handler reads first.
    query.sort((a, b) => byCodeUnit(a[0], b[0]));
    return this.check(rule('http', { method, path: url.pathname, query }), viewport);
  }

  /** Return bounded evidence without including grants or any token values. */
  evidence() {
    return { permission_candidates: [...this.candidates.values()], permissions_used: [...this.used], permission_candidates_omitted: this.omitted };
  }
}

module.exports = { BrowserPermissions, CommandTracker, LIMITS, POLICY, canonical, rule, validate, raster, writes, boundedResult, websocketBlockReason };
