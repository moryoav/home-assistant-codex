// A bounded, observational HA browser. No click, evaluate, or arbitrary URL tool.
const { chromium } = require('playwright-core');

const READ_MESSAGES = new Set([
  'get_config', 'get_states', 'get_services', 'auth/current_user', 'supported_features',
  'subscribe_entities', 'unsubscribe_events', 'ping',
  'lovelace/config', 'lovelace/dashboards/list', 'lovelace/resources',
  'lovelace/resources/list', 'frontend/get_themes', 'frontend/get_user_data',
  'frontend/get_translations', 'frontend/subscribe_extra_js', 'config/entity_registry/list',
  'frontend/subscribe_user_data', 'frontend/subscribe_system_data', 'recorder/info',
  'repairs/list_issues', 'brands/access_token',
  'labs/subscribe', 'persistent_notification/subscribe', 'frontend/get_system_data', 'lovelace/info',
  'config/entity_registry/get_entries',
  'config/entity_registry/list_for_display', 'config/device_registry/list',
  'config/area_registry/list', 'config/floor_registry/list',
  'config/label_registry/list', 'config/category_registry/list',
  'history/history_during_period', 'recorder/statistics_during_period',
  'recorder/get_statistics_metadata', 'recorder/list_statistic_ids',
  'energy/get_prefs', 'energy/info', 'get_panels', 'manifest/list',
]);

function allowMessage(message) {
  if (!message || typeof message !== 'object' || Array.isArray(message)) return false;
  if (message.type === 'subscribe_events') return new Set([
    'state_changed', 'themes_updated', 'panels_updated', 'lovelace_updated',
    'entity_registry_updated', 'device_registry_updated', 'area_registry_updated',
    'floor_registry_updated', 'label_registry_updated', 'category_registry_updated',
    'services_updated', 'service_registered', 'service_removed', 'core_config_updated', 'component_loaded',
    'repairs_issue_registry_updated',
  ]).has(message.event_type);
  return READ_MESSAGES.has(message.type);
}

function allowRequest(url, origin, method, dashboardPath) {
  if (url.origin !== origin || !['GET', 'HEAD'].includes(method)) return false;
  if (/%|\\/.test(url.pathname)) return false;
  // No arbitrary API GETs: integrations can have state-changing GET handlers.
  if (url.pathname.startsWith('/api/')) {
    return /^\/api\/(onboarding|config|states(?:\/[a-z0-9_.]+)?|history\/period(?:\/[0-9T:Z.+-]+)?|camera_proxy\/[a-z0-9_.]+)$/.test(url.pathname);
  }
  return /^\/(frontend_latest|frontend_es5|static|local|hacsfiles)\//.test(url.pathname)
    || (dashboardPath && url.pathname === dashboardPath)
    || url.pathname === '/';
}

async function inspect(input) {
  const origin = new URL(input.url).origin;
  if (!/^\/[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_-]+)?\/?$/.test(input.path)) throw Error('Invalid dashboard path');
  const errors = [], blocked = [], screenshots = [];
  let stage = 'launch';
  let activePage, activeViewport;
  const add = (list, value) => { if (list.length < 30) list.push(String(value).slice(0, 500)); };
  const browser = await chromium.launch({
    executablePath: process.env.HA_BROWSER_EXECUTABLE || '/usr/bin/chromium-browser',
    headless: true,
    args: ['--disable-dev-shm-usage', '--disable-background-networking', '--renderer-process-limit=2', '--js-flags=--max-old-space-size=192'],
    // The app already runs as root in a confined container. See VERIFICATION.md.
    chromiumSandbox: false,
  });
  try {
    const context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false });
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (allowRequest(url, origin, req.method(), input.path)) return route.continue();
      add(blocked, `${req.method()} ${url.pathname}`);
      await route.abort('blockedbyclient');
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
            if (message.type === 'result' && message.success === false)
              add(errors, `Home Assistant ${commands.get(message.id) || 'request'}: ${message.error?.code || 'error'}`);
          }
        } catch (_) { /* Forward opaque frames without logging their contents. */ }
        route.send(raw);
      });
      route.onMessage(raw => {
        let message;
        try { message = JSON.parse(String(raw)); } catch { route.close(); return; }
        if (message.type === 'auth' && !authenticated) {
          authenticated = true;
          upstream.send(JSON.stringify({ type: 'auth', access_token: input.access_token }));
        } else if (authenticated && allowMessage(message)) {
          if (commands.size < 500) commands.set(message.id, message.type);
          upstream.send(raw);
        } else {
          add(blocked, `WebSocket ${String(message.type)}${message.event_type ? ` (${message.event_type})` : ''}`);
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
        getExternalAuth: raw => {
          const options = JSON.parse(raw);
          if (options.callback === 'externalAuthSetToken')
            window.externalAuthSetToken(true, { access_token: 'verification-session', expires_in: 180 });
        },
        revokeExternalAuth: () => window.externalAuthRevokeToken?.(true),
      };
    });
    // Substitute only this exact placeholder on approved same-origin REST reads.
    await context.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url());
      if (!allowRequest(url, origin, req.method(), input.path)) return route.fallback();
      const headers = { ...req.headers() };
      if (headers.authorization === 'Bearer verification-session') headers.authorization = `Bearer ${input.access_token}`;
      const response = await route.fetch({ headers, maxRedirects: 0, timeout: 15000 });
      if (response.status() >= 300 && response.status() < 400) {
        add(blocked, `Redirect ${url.pathname}`);
        return route.abort('blockedbyclient');
      }
      await route.fulfill({ response });
    });
    for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      const page = await context.newPage();
      activePage = page;
      activeViewport = {name, width, height};
      await page.setViewportSize({ width, height });
      page.on('console', msg => { if (msg.type() === 'error') add(errors, msg.text()); });
      page.on('pageerror', error => add(errors, error.message));
      page.on('response', response => {
        if (response.status() >= 400) add(errors, `HTTP ${response.status()} ${new URL(response.url()).pathname}`);
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
      await page.waitForTimeout(1500);
      const requestedPath = input.path.replace(/\/$/, '');
      const actualPath = new URL(page.url()).pathname.replace(/\/$/, '');
      if (actualPath !== requestedPath && !(requestedPath.split('/').length === 2 && actualPath.startsWith(requestedPath + '/'))) {
        add(errors, 'Home Assistant redirected to a different dashboard or view');
      }
      const layout = await page.evaluate(() => {
        const messages = [];
        function visit(root) {
          for (const el of root.querySelectorAll('*')) {
            if (el.localName === 'hui-error-card') messages.push(el.textContent.trim().slice(0, 300) || 'Dashboard error card');
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
    return { status: errors.length || blocked.length ? 'issues' : 'captured', errors, blocked, screenshots,
      message: 'Screenshots captured for visual inspection. This is not proof of correct layout or automation behavior.' };
  } catch (_) {
    if (activePage && !activePage.isClosed()) {
      try {
        const file = `${input.directory}/${activeViewport.name}.png`;
        await activePage.screenshot({path: file, timeout: 5000});
        screenshots.push({...activeViewport, file});
      } catch (_) { /* The page may already have crashed. */ }
    }
    return { status: 'unavailable', stage, errors, blocked, screenshots,
      message: `Dashboard verification could not finish during ${stage}.` };
  } finally {
    await browser.close();
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
