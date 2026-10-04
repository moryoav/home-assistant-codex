// A bounded, observational HA browser. No click, evaluate, or arbitrary URL tool.
const { chromium } = require('playwright-core');
const { ResourcePolicy, createExternalFetcher } = require('./browser_resources.cjs');

const READ_MESSAGES = new Set([
  'get_config', 'get_states', 'get_services', 'auth/current_user', 'supported_features',
  'subscribe_entities', 'unsubscribe_events', 'ping',
  'lovelace/config', 'lovelace/dashboards/list', 'lovelace/resources',
  'lovelace/resources/list', 'frontend/get_themes', 'frontend/get_user_data',
  'frontend/get_translations', 'frontend/subscribe_extra_js', 'config/entity_registry/list',
  'frontend/subscribe_user_data', 'frontend/subscribe_system_data', 'recorder/info',
  'repairs/list_issues', 'brands/access_token', 'frontend/get_icons',
  'render_template', 'sensor/numeric_device_classes', 'weather/subscribe_forecast',
  'labs/subscribe', 'persistent_notification/subscribe', 'frontend/get_system_data', 'lovelace/info',
  'config/entity_registry/get_entries',
  'config/entity_registry/list_for_display', 'config/device_registry/list',
  'config/area_registry/list', 'config/floor_registry/list',
  'config/label_registry/list', 'config/category_registry/list',
  'history/history_during_period', 'recorder/statistics_during_period',
  'recorder/get_statistics_metadata', 'recorder/list_statistic_ids',
  'energy/get_prefs', 'energy/info', 'get_panels', 'manifest/list',
]);

/**
 * Return whether a WebSocket message from the dashboard may be forwarded to
 * Home Assistant. Only the read-only commands listed here and subscriptions
 * to a fixed set of event types pass.
 */
function allowMessage(message) {
  if (!message || typeof message !== 'object' || Array.isArray(message)) return false;
  // This handler only returns frontend configuration. Do not subscribe this
  // capture browser to physical knob events or report navigation results.
  if (message.type === 'knob_swipe_navigation/config')
    return Object.keys(message).every(key => ['id', 'type'].includes(key));
  // These custom_icons handlers only return active sets, icon lists, cached
  // icons, or one icon. Selection/download handlers remain blocked. Bound names
  // before forwarding because local icon handlers construct filesystem paths.
  if (message.type === 'custom_icons/activesets')
    return Object.keys(message).every(key => ['id', 'type'].includes(key));
  if (['custom_icons/list', 'custom_icons/icon_cache', 'custom_icons/icon'].includes(message.type)) {
    const icon = message.type === 'custom_icons/icon';
    return Object.keys(message).every(key => ['id', 'type', 'set', ...(icon ? ['icon'] : [])].includes(key)) &&
      typeof message.set === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(message.set) &&
      (!icon || (typeof message.icon === 'string' && message.icon.length <= 256 && /^[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_-]+)*$/.test(message.icon)));
  }
  if (message.type === 'subscribe_events') return new Set([
    'state_changed', 'themes_updated', 'panels_updated', 'lovelace_updated',
    'entity_registry_updated', 'device_registry_updated', 'area_registry_updated',
    'floor_registry_updated', 'label_registry_updated', 'category_registry_updated',
    'services_updated', 'service_registered', 'service_removed', 'core_config_updated', 'component_loaded',
    'repairs_issue_registry_updated',
  ]).has(message.event_type);
  return READ_MESSAGES.has(message.type);
}

/**
 * Return whether the dashboard may send an HTTP request to Home Assistant: a
 * same-origin GET or HEAD for the frontend's files, files under /local or
 * /hacsfiles, the dashboard page itself, or one of a few read-only API paths.
 */
function allowRequest(url, origin, method, dashboardPath) {
  if (url.origin !== origin || !['GET', 'HEAD'].includes(method)) return false;
  if (/%|\\/.test(url.pathname)) return false;
  // No arbitrary API GETs: integrations can have state-changing GET handlers.
  if (url.pathname.startsWith('/api/')) {
    return /^\/api\/(onboarding|config|states(?:\/[a-z0-9_.]+)?|calendars\/calendar\.[a-z0-9_]+|history\/period(?:\/[0-9T:Z.+-]+)?|camera_proxy\/[a-z0-9_.]+|image\/serve\/[a-f0-9]{32}\/(?:256x256|512x512|original))$/.test(url.pathname);
  }
  return /^\/(frontend_latest|frontend_es5|static|local|hacsfiles)\//.test(url.pathname)
    || (dashboardPath && url.pathname === dashboardPath)
    || url.pathname === '/';
}

/**
 * Load a Home Assistant dashboard in headless Chromium at a desktop and a
 * mobile size and save a screenshot of each. Requests and WebSocket messages
 * from the page pass only if the rules above and the resource policy allow
 * them, and the real access token never reaches the page scripts. Returns the
 * screenshots with the errors, blocked items, and findings; the status is
 * unavailable, with the stage reached, when the capture could not finish.
 * Throws for an invalid origin or dashboard path.
 */
async function inspect(input) {
  const target = new URL(input.url);
  const origin = target.origin;
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password || origin.includes(','))
    throw Error('Invalid Home Assistant origin');
  if (!/^\/[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_-]+)?\/?$/.test(input.path)) throw Error('Invalid dashboard path');
  const errors = [], blocked = [], screenshots = [];
  const findings = new Map();
  let omitted = 0;
  const policy = new ResourcePolicy(origin, input.resources);
  const fetchExternal = createExternalFetcher(policy);
  const blockedURLs = new Set();
  /** Name a resource by its path, preceded by the host when it is external. */
  const resourceLabel = url => `${url.origin === origin ? '' : url.host}${url.pathname}`;
  /** Turn an error text that says nothing by itself into a readable message. */
  const scriptError = text => text.length <= 3 || text === 'Object' ? `Unspecified dashboard script error (${text})` : text;
  let stage = 'launch';
  let browser, activePage, activeViewport;
  /**
   * Record a finding, counting repeats and the viewports it was seen in, and
   * add its message to the errors or blocked list when one is given. Messages
   * are cut to 300 characters; past 40 different findings, new ones are only
   * counted as omitted.
   */
  const add = (list, value, kind = list === errors ? 'dashboard' : 'policy') => {
    const message = String(value).slice(0, 300), key = `${kind}:${message}`;
    let finding = findings.get(key);
    if (!finding) {
      if (findings.size >= 40) { omitted++; return; }
      finding = {kind, message, count: 0, viewports: []}; findings.set(key, finding);
      if (list && !list.includes(message)) list.push(message);
    }
    finding.count++;
    if (activeViewport && !finding.viewports.includes(activeViewport.name)) finding.viewports.push(activeViewport.name);
  };
  /** Return the errors, blocked items, and findings gathered so far. */
  const evidence = () => ({errors, blocked, findings: [...findings.values()], findings_omitted: omitted});
  try {
    browser = await chromium.launch({
      executablePath: process.env.HA_BROWSER_EXECUTABLE || '/usr/bin/chromium-browser',
      headless: true,
      args: ['--disable-dev-shm-usage', '--disable-background-networking', '--renderer-process-limit=2', '--js-flags=--max-old-space-size=192',
        // Intercepted pages need local-network permission for their WebSocket.
        // Chromium only grants it to secure contexts. Scope the HTTP exception
        // to the broker-selected Core origin, never all sites or network checks.
        ...(target.protocol === 'http:' ? [`--unsafely-treat-insecure-origin-as-secure=${origin}`] : [])],
      // The app already runs as root in a confined container. See VERIFICATION.md.
      chromiumSandbox: false,
    });
    stage = 'context';
    const context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false });
    // Intercepted documents need explicit local access on recent Chromium.
    // Authenticated HTTP and WebSocket traffic remains restricted to Core.
    stage = 'permissions';
    await context.grantPermissions(['local-network-access'], { origin });
    stage = 'routing';
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (policy.remote(url, req.method(), req.resourceType())) {
        try {
          const response = await fetchExternal(url, req.method(), req.resourceType());
          return await route.fulfill(response);
        } catch (error) {
          add(blocked, `External resource ${resourceLabel(url)}: ${error.message}`, 'resource');
          if (blockedURLs.size < 256) blockedURLs.add(url.href);
          return route.abort('blockedbyclient').catch(() => {});
        }
      }
      if (!allowRequest(url, origin, req.method(), input.path) && !policy.local(url, req.method(), req.resourceType())) {
        add(blocked, `${req.method()} ${resourceLabel(url)}`, 'resource');
        if (blockedURLs.size < 256) blockedURLs.add(url.href);
        return route.abort('blockedbyclient').catch(() => {});
      }
      // Fetch without redirects: Playwright does not re-route redirect hops.
      const headers = { ...req.headers() };
      if (url.pathname.startsWith('/api/') && headers.authorization === 'Bearer verification-session')
        headers.authorization = `Bearer ${input.access_token}`;
      let response;
      try {
        response = await route.fetch({ headers, maxRedirects: 0, timeout: 15000 });
        if (response.status() >= 300 && response.status() < 400) {
          add(blocked, `Redirect ${url.pathname}`, 'resource');
          if (blockedURLs.size < 256) blockedURLs.add(url.href);
          return await route.abort('blockedbyclient');
        }
        if ((response.headers()['content-type'] || '').startsWith('text/css'))
          policy.cssDependencies(await response.body(), url);
        return await route.fulfill({ response });
      } catch (_) {
        add(errors, `Request failed ${url.pathname}`, 'resource');
        await route.abort('failed').catch(() => {});
      } finally {
        // Playwright retains fetched bodies until context close unless disposed.
        // Wait for delivery before releasing them, including failed/redirected requests.
        await response?.dispose().catch(() => {});
      }
    });
    await context.routeWebSocket('**/*', route => {
      const url = new URL(route.url());
      if (url.host !== new URL(origin).host || url.pathname !== '/api/websocket' ||
          url.protocol !== (origin.startsWith('https:') ? 'wss:' : 'ws:')) {
        route.close(); return;
      }
      const upstream = route.connectToServer();
      let authenticated = false;
      const commands = new Map();
      upstream.onMessage(raw => {
        try {
          const parsed = JSON.parse(String(raw));
          for (const message of (Array.isArray(parsed) ? parsed : [parsed])) {
            if (message.type === 'result' && message.success &&
                ['lovelace/resources', 'lovelace/resources/list'].includes(commands.get(message.id)) && Array.isArray(message.result))
              for (const resource of message.result.slice(0, 256)) policy.register(resource?.url);
            if (message.type === 'result' && message.success === false)
              add(errors, `Home Assistant ${commands.get(message.id) || 'request'}: ${message.error?.code || 'error'}`, 'home_assistant');
          }
        } catch (_) { /* Forward opaque frames without logging their contents. */ }
        route.send(raw);
      });
      route.onMessage(raw => {
        let message;
        try { message = JSON.parse(String(raw)); } catch { route.close(); return; }
        if (!message || typeof message !== 'object' || Array.isArray(message)) { route.close(); return; }
        if (message.type === 'auth' && !authenticated) {
          authenticated = true;
          upstream.send(JSON.stringify({ type: 'auth', access_token: input.access_token }));
        } else if (authenticated && allowMessage(message)) {
          if (commands.size < 500) commands.set(message.id, message.type);
          upstream.send(raw);
        } else {
          const service = message.type === 'call_service' && /^[a-z_]+$/.test(message.domain) && /^[a-z_]+$/.test(message.service)
            ? `${message.domain}.${message.service}` : '';
          const diagnostic = ['system_log.write', 'persistent_notification.create'].includes(service);
          add(diagnostic ? null : blocked, `WebSocket ${String(message.type)}${service ? ` (${service})` : message.event_type ? ` (${message.event_type})` : ''}`,
            diagnostic ? 'diagnostic' : 'policy');
          if (Number.isInteger(message.id)) route.send(JSON.stringify({
            id: message.id, type: 'result', success: false,
            error: { code: 'unauthorized', message: 'Blocked by dashboard verification' },
          }));
        }
      });
    });
    // The frontend sees only a placeholder. The controller substitutes the real
    // short-lived token on the authenticated WebSocket, never in page scripts.
    await context.addInitScript(() => {
      window.externalApp = {
        /** Answer the frontend's token request with the placeholder token. */
        getExternalAuth: raw => {
          const options = JSON.parse(raw);
          if (options.callback === 'externalAuthSetToken')
            window.externalAuthSetToken(true, { access_token: 'verification-session', expires_in: 180 });
        },
        /** Confirm a token revocation to the frontend. */
        revokeExternalAuth: () => window.externalAuthRevokeToken?.(true),
      };
    });
    for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      const page = await context.newPage();
      activePage = page;
      activeViewport = {name, width, height};
      await page.setViewportSize({ width, height });
      page.on('console', msg => {
        if (msg.type() !== 'error') return;
        const text = msg.text();
        // HTTP and failed-request events below provide the URL/status instead.
        if (text.startsWith('Failed to load resource:')) return;
        if (/Blocked by dashboard verification/.test(text) && /system log|notification/i.test(text))
          add(null, 'Dashboard diagnostic logging or notification was blocked', 'diagnostic');
        else add(errors, scriptError(text));
      });
      page.on('pageerror', error => add(errors, scriptError(error.message)));
      page.on('requestfailed', req => {
        if (!blockedURLs.has(req.url()))
          add(errors, `Resource failed ${resourceLabel(new URL(req.url()))}: ${req.failure()?.errorText || 'network error'}`, 'resource');
      });
      page.on('response', response => {
        if (response.status() >= 400) {
          const url = new URL(response.url());
          add(errors, `HTTP ${response.status()} ${resourceLabel(url)}`, url.origin === origin && url.pathname.startsWith('/api/') ? 'home_assistant' : 'resource');
        }
      });
      stage = 'navigation';
      await page.goto(`${origin}${input.path}?external_auth=1`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      stage = 'authentication';
      await page.waitForFunction(() => {
        const ha = document.querySelector('home-assistant');
        return Boolean(ha?.hass?.connection && ha?.hass?.user);
      }, { }, { timeout: 20000 });
      stage = 'dashboard';
      await page.locator('hui-view, hui-panel-view, hui-sections-view, hui-masonry-view').first().waitFor({ timeout: 20000 });
      await page.waitForLoadState('networkidle', {timeout: 6000}).catch(() => {});
      await page.evaluate(() => Promise.race([document.fonts.ready, new Promise(resolve => setTimeout(resolve, 3000))]));
      await page.waitForTimeout(1500);
      const requestedPath = input.path.replace(/\/$/, '');
      const actualPath = new URL(page.url()).pathname.replace(/\/$/, '');
      if (actualPath !== requestedPath && !(requestedPath.split('/').length === 2 && actualPath.startsWith(requestedPath + '/'))) {
        add(errors, 'Home Assistant redirected to a different dashboard or view');
      }
      const layout = await page.evaluate(() => {
        const messages = [];
        /** Collect visible error card messages, shadow roots included. */
        function visit(root) {
          for (const el of root.querySelectorAll('*')) {
            if (el.localName === 'hui-error-card' && el.checkVisibility({visibilityProperty: true, opacityProperty: true}))
              messages.push(String(el._config?.message || el._config?.error || el.textContent.trim() || 'Dashboard error card').slice(0, 300));
            if (el.shadowRoot) visit(el.shadowRoot);
          }
        }
        visit(document);
        return { error_cards: messages.slice(0, 20), document_overflow: document.documentElement.scrollWidth > innerWidth };
      });
      for (const error of layout.error_cards) add(errors, error);
      const file = `${input.directory}/${name}.png`;
      await page.screenshot({ path: file, fullPage: false, timeout: 10000 });
      screenshots.push({ name, file, width, height, ...layout });
      await page.close();
    }
    await context.close();
    return { status: errors.length || blocked.length || omitted ? 'issues' : 'captured', ...evidence(), screenshots,
      message: 'Screenshots captured for visual inspection. This is not proof of correct layout or automation behavior.' };
  } catch (_) {
    if (activePage && !activePage.isClosed()) {
      try {
        const file = `${input.directory}/${activeViewport.name}.png`;
        await activePage.screenshot({path: file, timeout: 5000});
        screenshots.push({...activeViewport, file});
      } catch (_) { /* The page may already have crashed. */ }
    }
    return { status: 'unavailable', stage, ...evidence(), screenshots,
      message: `Dashboard verification could not finish during ${stage}.` };
  } finally {
    await browser?.close();
  }
}

if (require.main === module) {
  let input = '';
  process.stdin.on('data', data => { input += data; if (input.length > 65536) process.exit(2); });
  process.stdin.on('end', async () => {
    try { console.log(JSON.stringify(await inspect(JSON.parse(input)))); }
    catch (_) { console.log(JSON.stringify({ status: 'unavailable', message: 'Browser verification could not load the dashboard. Check local access, authentication, browser availability, and dashboard permissions.' })); process.exitCode = 1; }
  });
}
module.exports = { allowRequest, allowMessage, inspect };
