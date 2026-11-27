// Exercise real Ingress permission routes, persistence, retry progress and Settings.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const base = 'http://127.0.0.1:9137/preview/';

/** Send one simulated inspection and expose its grouped permission controls. */
async function inspection(page) {
  await page.locator('#new-chat').click();
  await page.locator('#message').fill('Inspect with blocked browser requests');
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  const panel = page.locator('.browser-permission-review').last();
  await panel.locator('..').locator('summary').first().click();
  await panel.getByRole('checkbox', {name: 'Select all resource requests', exact: true}).waitFor();
  return panel;
}

/** Compare persisted UI decisions with the real worker's stored evidence. */
async function task(page) {
  const id = await page.evaluate(() => localStorage.getItem('codex-last-chat'));
  return (await (await page.request.get(new URL(`tasks/${id}`, base).href)).json()).task;
}

(async () => {
  const output = process.env.CODEX_CHAT_QA_DIR || path.join(os.tmpdir(), 'codex-chat-qa');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true, ...(process.env.CODEX_CHAT_BROWSER ? {channel: process.env.CODEX_CHAT_BROWSER} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.locator('.chat-row').first().waitFor();
    const panel = await inspection(page);
    const allow = panel.getByRole('button', {name: 'Allow selected and retry', exact: true});
    assert.equal(await allow.isDisabled(), true);
    assert.equal(await panel.getByText(/every matching request on desktop and mobile, including later retries/).isVisible(), true);
    await page.locator('#message').fill('Keep my unsent draft');
    await panel.getByRole('checkbox', {name: 'Select all resource requests', exact: true}).check();
    const module = panel.getByRole('checkbox', {name: 'WebSocket bubble_card_tools/read_module', exact: true});
    const preference = panel.getByRole('checkbox', {name: 'WebSocket frontend/set_user_data', exact: true});
    const diagnostic = panel.getByText('Optional diagnostic requests', {exact: true});
    assert.equal(await diagnostic.locator('..').evaluate(node => node.open), false);
    await diagnostic.click();
    const action = panel.getByRole('checkbox', {name: 'WebSocket call_service', exact: true});
    assert.equal(await module.isChecked(), false);
    assert.equal(await preference.isChecked(), false);
    assert.equal(await action.isChecked(), false);
    assert.equal(await panel.getByText('Potentially dangerous.', {exact: false}).isVisible(), true);
    assert.equal(await panel.getByText(/"name":"config\.yaml"/).isVisible(), true);
    await module.check();
    await preference.check();
    await action.check();
    await panel.getByRole('checkbox', {name: 'Remember for this dashboard', exact: true}).check();
    assert.equal(await allow.isEnabled(), true);
    const old = await task(page);
    const source = old.verification[0];
    assert.match(await panel.getByText(/<img src=x onerror/).textContent(), /<img src=x onerror/);
    assert.equal(await panel.locator('img').count(), 0);
    await panel.scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(output, 'browser-permissions-desktop.png'), fullPage: true, animations: 'disabled'});
    await page.setViewportSize({width: 390, height: 844});
    await page.emulateMedia({colorScheme: 'dark'});
    if (await page.locator('body').evaluate(node => node.classList.contains('sidebar-open')))
      await page.locator('#close-sidebar').click();
    await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 0);
    await allow.scrollIntoViewIfNeeded();
    assert.equal(await panel.evaluate(node => node.scrollWidth <= node.clientWidth), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.join(output, 'browser-permissions-mobile.png'), fullPage: true, animations: 'disabled'});
    await allow.click();
    await page.getByText('Remembered for this dashboard:', {exact: false}).waitFor();
    assert.equal(await page.locator('#message').inputValue(), 'Keep my unsent draft');
    const updated = await task(page);
    assert.equal(updated.status, 'completed');
    assert.equal(updated.summary, old.summary);
    assert.equal(updated.verification.length, 2);
    assert.equal(updated.verification[1].status, 'captured');
    assert.equal(updated.verification[1].permission_decision.approved.length, 4);
    const saved = await (await page.request.get(new URL('browser-permissions', base).href)).json();
    assert.equal(saved.permissions.length, 4);
    const replay = await page.request.post(new URL(`tasks/${updated.task_id}/browser-permissions`, base).href, {
      data: {turn_id: updated.current_turn_id, check_id: source.check_id, candidate_ids: source.permission_candidates.map(item => item.id), remember: true},
    });
    assert.equal(replay.status(), 409);
    // Saved permissions remain discoverable after navigation and a full reload.
    await page.reload();
    await page.setViewportSize({width: 1440, height: 1000});
    await page.getByRole('button', {name: 'Open settings', exact: true}).click();
    const list = page.locator('#browser-permissions');
    await list.getByRole('button').first().waitFor();
    assert.equal(await list.getByRole('button').count(), 4);
    assert.equal(await list.getByText('Potentially dangerous.', {exact: false}).isVisible(), true);
    for (let remaining = 4; remaining > 0; remaining--) {
      await list.getByRole('button').first().click();
      await page.waitForFunction(count => document.querySelectorAll('#browser-permissions button').length === count, remaining - 1);
    }
    await list.getByText('No browser permissions remembered.', {exact: true}).waitFor();
    assert.equal(await list.evaluate(node => node === document.activeElement), true);
    // An unreadable permissions file can be removed from Settings instead of blocking everything.
    await page.request.post(new URL('fixture/browser-permissions/corrupt', base).href);
    await page.getByRole('button', {name: 'Close settings', exact: true}).click();
    await page.getByRole('button', {name: 'Open settings', exact: true}).click();
    const clear = list.getByRole('button', {name: 'Remove all saved permissions', exact: true});
    await clear.waitFor();
    assert.match(await list.textContent(), /could not be read/);
    await clear.click();
    await list.getByText('No browser permissions remembered.', {exact: true}).waitFor();
    assert.equal(await page.locator('#settings-result').textContent(), 'Saved browser permissions removed.');
    await page.getByRole('button', {name: 'Close settings', exact: true}).click();
    // An unselected request stays blocked and one-off approval creates no saved rule.
    const once = await inspection(page);
    await once.getByRole('checkbox', {name: 'Select all resource requests', exact: true}).check();
    await once.getByRole('checkbox', {name: 'WebSocket bubble_card_tools/read_module', exact: true}).check();
    await once.getByRole('checkbox', {name: 'WebSocket frontend/set_user_data', exact: true}).check();
    await once.getByRole('button', {name: 'Allow selected and retry', exact: true}).click();
    await page.getByText('Allowed for this inspection and its retries:', {exact: false}).waitFor();
    const single = await task(page);
    assert.equal(single.verification.at(-1).status, 'captured');
    assert.equal(single.verification.at(-1).permission_candidates.length, 1);
    const clean = page.locator('.verification-result.is-captured').last();
    assert.equal(await clean.locator('.browser-permission-review').evaluate(node => node.open), false);
    assert.equal(await clean.getByText('Optional diagnostic requests', {exact: true}).isVisible(), true);
    assert.equal(await clean.getByText('Review blocked browser requests', {exact: true}).count(), 0);
    assert.equal((await (await page.request.get(new URL('browser-permissions', base).href)).json()).permissions.length, 0);
    assert.deepEqual(errors, []);
    console.log('PASS: batch selection, remembered/once decisions, fresh retry evidence, stale rejection, safe details, drafts, saved permissions after reload, revocation, mobile dark mode, and no page errors.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
