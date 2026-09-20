// Run against web_fixture.py with Playwright available on NODE_PATH.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

(async () => {
  const output =
    process.env.CODEX_CHAT_QA_DIR || path.join(os.tmpdir(), "codex-chat-qa");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CODEX_CHAT_BROWSER
      ? { channel: process.env.CODEX_CHAT_BROWSER }
      : {}),
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 950 },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("http://127.0.0.1:9137/preview/");
    await page.locator(".chat-row").first().waitFor();
    assert.equal(await page.locator(".chat-row").count(), 20);
    await page.getByRole("button", { name: "Load older chats" }).click();
    await page.waitForFunction(
      () => document.querySelectorAll(".chat-row").length === 25,
    );
    await page.locator('[data-task-id="preview-00"]').click();
    await page
      .locator("#messages")
      .getByText("Your evening routine looks good.", { exact: false })
      .waitFor();
    await page.locator("#message").fill("Draft that should stay in this chat");
    await page.locator('[data-task-id="preview-01"]').click();
    await page
      .locator("#messages")
      .getByText("The dashboard configuration is valid.", { exact: true })
      .waitFor();
    await page.locator('[data-task-id="preview-00"]').click();
    assert.equal(
      await page.locator("#message").inputValue(),
      "Draft that should stay in this chat",
    );
    await page
      .locator("#messages")
      .getByText("Your evening routine looks good.", { exact: false })
      .waitFor();
    await page.locator("#message").fill("Apply the first suggestion.");
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page
      .locator("#messages")
      .getByText("Preview response: Apply the first suggestion.", {
        exact: true,
      })
      .waitFor();
    assert.equal(await page.locator(".message.user").count(), 2);
    assert.equal(await page.locator(".answer").count(), 2);
    const divider = await page.locator("#divider").boundingBox();
    await page.mouse.move(divider.x + 2, 300);
    await page.mouse.down();
    await page.mouse.move(355, 300);
    await page.mouse.up();
    assert.equal(
      await page.locator("#divider").getAttribute("aria-valuenow"),
      "355",
    );
    await page.locator("#divider").focus();
    await page.keyboard.press("ArrowLeft");
    assert.equal(
      await page.locator("#divider").getAttribute("aria-valuenow"),
      "335",
    );
    await page.locator("#message").focus();
    await page.screenshot({
      path: path.join(output, "desktop.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    assert.equal(await page.locator(".answer").count(), 0);
    const hostile = '<img src=x onerror="window.untrustedRan=true">';
    await page.locator("#message").fill(hostile);
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page
      .locator("#messages")
      .getByText("Preview response: " + hostile, { exact: true })
      .waitFor();
    assert.equal(await page.locator("#messages img").count(), 0);
    assert.equal(await page.evaluate(() => window.untrustedRan), undefined);
    await page.getByRole("button", { name: "Open settings" }).click();
    await page
      .getByText("Signed in (local preview)", { exact: true })
      .waitFor();
    await page.locator("#agents").fill("Keep edits focused.");
    await page.getByRole("button", { name: "Save instructions" }).click();
    await page.getByText("Instructions saved.").waitFor();
    await page.getByRole("button", { name: "Close settings" }).click();
    await page.reload();
    await page.locator(".chat-row").first().waitFor();
    assert.equal(
      await page.locator("#divider").getAttribute("aria-valuenow"),
      "335",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
      await page.locator("#sidebar").evaluate((el) => el.inert),
      true,
    );
    await page
      .getByRole("button", { name: "Open conversations", exact: true })
      .click();
    await page.locator('[data-task-id="preview-00"]').click();
    await page
      .locator("#messages")
      .getByText("Preview response: Apply the first suggestion.", {
        exact: true,
      })
      .waitFor();
    assert.equal(await page.locator("#scrim").isHidden(), true);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.screenshot({
      path: path.join(output, "mobile.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page
      .getByRole("button", { name: "Open conversations", exact: true })
      .click();
    await page.screenshot({
      path: path.join(output, "mobile-sidebar.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page
      .getByRole("button", { name: "Close conversations", exact: true })
      .last()
      .click();
    await page.emulateMedia({ colorScheme: "dark" });
    await page.screenshot({
      path: path.join(output, "mobile-dark.png"),
      fullPage: true,
      animations: "disabled",
    });
    assert.deepEqual(errors, []);
    console.log(
      "PASS: history, pagination, continuation, new chats, drafts, safe text, settings, resize, mobile and dark mode. Screenshots: " +
        output,
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
