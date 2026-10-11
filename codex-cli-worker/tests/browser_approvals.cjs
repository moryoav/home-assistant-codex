// Exercise the chat permission UI against web_fixture.py.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

/** Check allow, deny, stale replies, cancellation, safe text, drafts, and narrow screens. */
async function testApprovals(browser, output, errors) {
  // Native permission decisions stay inside the live exchange and preserve drafts.
  const approvalPage = await browser.newPage({viewport: {width: 1440, height: 950}});
  approvalPage.on("pageerror", error => errors.push(error.message));
  await approvalPage.goto("http://127.0.0.1:9137/preview/");
  await approvalPage.locator(".chat-row").first().waitFor();
  await approvalPage.locator("#new-chat").click();
  await approvalPage.locator("#message").fill("Request permission to read sensor history");
  await approvalPage.getByRole("button", {name: "Send message", exact: true}).click();
  await approvalPage.getByRole("button", {name: "Allow once", exact: true}).waitFor();
  const permissionTask = await approvalPage.evaluate(() => localStorage.getItem("codex-last-chat"));
  const permissionRecord = (await (await approvalPage.request.get(`http://127.0.0.1:9137/preview/tasks/${permissionTask}`)).json()).task.approvals[0];
  assert.equal(await approvalPage.locator(".permission-card img").count(), 0);
  assert.match(await approvalPage.locator(".permission-command").textContent(), /<img src=x onerror=alert\(1\)>/);
  assert.match(await approvalPage.locator(".permission-scope").textContent(), /outside the configured sandbox/);
  assert.equal(await approvalPage.locator("#cancel").isVisible(), true);
  await approvalPage.locator("#message").fill("Keep this draft while permission is answered");
  await approvalPage.screenshot({path: path.join(output, "permission-desktop.png"), fullPage: true});
  await approvalPage.getByRole("button", {name: "Allow once", exact: true}).click();
  await approvalPage.locator(".permission-card strong").filter({hasText: "Allowed once"}).waitFor();
  assert.equal(await approvalPage.getByRole("button", {name: "Allow once", exact: true}).count(), 0);
  assert.equal(await approvalPage.locator("#message").inputValue(), "Keep this draft while permission is answered");
  const replayed = await approvalPage.request.post(`http://127.0.0.1:9137/preview/tasks/${permissionTask}/approvals/${permissionRecord.approval_id}`, {
    data: {turn_id: permissionRecord.turn_id, decision: "allow"},
  });
  assert.equal(replayed.status(), 409);
  await approvalPage.locator("#new-chat").click();
  await approvalPage.locator("#message").fill("Request permission to test a denial");
  await approvalPage.getByRole("button", {name: "Send message", exact: true}).click();
  await approvalPage.getByRole("button", {name: "Deny", exact: true}).waitFor();
  await approvalPage.setViewportSize({width: 390, height: 844});
  if (await approvalPage.locator("body").evaluate(body => body.classList.contains("sidebar-open")))
    await approvalPage.locator("#close-sidebar").click();
  await approvalPage.waitForFunction(() => document.querySelector("#sidebar").getBoundingClientRect().right <= 0);
  const permissionBounds = await approvalPage.locator(".permission-card").evaluate(element => ({
    right: element.getBoundingClientRect().right, width: innerWidth,
    scroll: element.scrollWidth, client: element.clientWidth,
  }));
  assert.ok(permissionBounds.right <= permissionBounds.width, JSON.stringify(permissionBounds));
  assert.ok(permissionBounds.scroll <= permissionBounds.client, JSON.stringify(permissionBounds));
  await approvalPage.screenshot({path: path.join(output, "permission-mobile.png"), fullPage: true});
  assert.equal(await approvalPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await approvalPage.getByRole("button", {name: "Deny", exact: true}).click();
  await approvalPage.locator(".permission-card strong").filter({hasText: /^Denied$/}).waitFor();
  assert.equal(await approvalPage.locator(".permission-action").count(), 0);
  await approvalPage.setViewportSize({width: 1440, height: 950});
  await approvalPage.locator("#new-chat").click();
  await approvalPage.locator("#message").fill("Request permission then cancel");
  await approvalPage.getByRole("button", {name: "Send message", exact: true}).click();
  await approvalPage.getByRole("button", {name: "Allow once", exact: true}).waitFor();
  await approvalPage.locator("#cancel").click();
  await approvalPage.locator(".permission-card strong").filter({hasText: /^Expired$/}).waitFor();
  assert.equal(await approvalPage.locator(".permission-action").count(), 0);
  await approvalPage.close();
}
module.exports = testApprovals;

/** Run these focused checks on their own when diagnosing the approval UI. */
async function main() {
  const output = process.env.CODEX_CHAT_QA_DIR || path.join(os.tmpdir(), "codex-chat-qa");
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true, ...(process.env.CODEX_CHAT_BROWSER ? {channel: process.env.CODEX_CHAT_BROWSER} : {})});
  try {
    const errors = [];
    await testApprovals(browser, output, errors);
    assert.deepEqual(errors, []);
    console.log("PASS: permission UI on desktop and mobile, allow/deny, stale replies, cancellation, drafts, and safe text.");
  } finally {
    await browser.close();
  }
}
if (require.main === module) main().catch(error => { console.error(error); process.exit(1); });
