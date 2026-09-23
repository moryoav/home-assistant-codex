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
    // Chat actions: hovering a row reveals its menu button; pin, rename, delete.
    await page.locator('.chat-row:has([data-task-id="preview-05"])').hover();
    await page.locator('[data-menu-for="preview-05"]').click();
    await page.locator("#chat-menu").waitFor({ state: "visible" });
    assert.equal(
      await page.locator('#chat-menu [data-action="close"]').isVisible(),
      false,
    );
    await page.screenshot({
      path: path.join(output, "chat-menu.png"),
      animations: "disabled",
    });
    await page.getByRole("menuitem", { name: "Pin chat" }).click();
    await page.waitForFunction(
      () =>
        document.querySelector(".list-group")?.textContent === "Pinned" &&
        document.querySelector(".row-main").dataset.taskId === "preview-05",
    );
    assert.equal(await page.locator(".pin-icon").count(), 1);
    // Keyboard: Shift+F10 opens the menu, arrows move, Enter activates.
    await page.locator('[data-task-id="preview-06"]').focus();
    await page.keyboard.press("Shift+F10");
    await page.locator("#chat-menu").waitFor({ state: "visible" });
    await page.keyboard.press("ArrowDown");
    assert.equal(
      await page.evaluate(() => document.activeElement.dataset.action),
      "rename",
    );
    await page.keyboard.press("Enter");
    await page.locator("#rename-dialog").waitFor({ state: "visible" });
    await page.locator("#rename-input").fill("  Porch   lights ");
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () =>
        document.querySelector('[data-task-id="preview-06"] .row-title')
          .textContent === "Porch lights",
    );
    // Right-click opens the same menu; Delete asks for confirmation first.
    await page
      .locator('[data-task-id="preview-07"]')
      .click({ button: "right" });
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page.locator("#delete-dialog").waitFor({ state: "visible" });
    await page.locator("#delete-cancel").click();
    assert.equal(await page.locator(".chat-row").count(), 25);
    await page
      .locator('[data-task-id="preview-07"]')
      .click({ button: "right" });
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page.locator("#delete-confirm").click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".chat-row").length === 24 &&
        !document.querySelector('[data-task-id="preview-07"]'),
    );
    const managed = await page.evaluate(async () => ({
      listing: (
        await (
          await fetch("tasks?summary=true&order=pinned_first&limit=3")
        ).json()
      ).tasks,
      deleted: (await fetch("tasks/preview-07")).status,
      renamed: (await (await fetch("tasks/preview-06")).json()).task.title,
    }));
    assert.equal(managed.listing[0].task_id, "preview-05");
    assert.equal(managed.listing[0].pinned, true);
    assert.equal(managed.deleted, 404);
    assert.equal(managed.renamed, "Porch lights");
    await page.locator('[data-task-id="preview-02"]').click();
    const image = page.locator("#messages img.attachment-image");
    await image.waitFor();
    assert.equal(await image.count(), 1);
    await page.waitForFunction(() => {
      const img = document.querySelector("#messages img.attachment-image");
      return img && img.complete && img.naturalWidth > 0;
    });
    assert.match(
      await image.getAttribute("src"),
      /\/preview\/tasks\/preview-02\/attachments\/[0-9a-f]{32}$/,
    );
    assert.match(
      await page
        .locator("#messages .attachment figcaption a")
        .getAttribute("href"),
      /\?download=1$/,
    );
    const served = await page.evaluate(async () => {
      const src = document.querySelector("#messages img.attachment-image").src;
      const ok = await fetch(src);
      const missing = await fetch(
        "tasks/preview-02/attachments/" + "0".repeat(32),
      );
      const download = await fetch(src + "?download=1");
      return {
        status: ok.status,
        type: ok.headers.get("content-type"),
        nosniff: ok.headers.get("x-content-type-options"),
        disposition: download.headers.get("content-disposition"),
        missing: missing.status,
      };
    });
    assert.equal(served.status, 200);
    assert.equal(served.type, "image/png");
    assert.equal(served.nosniff, "nosniff");
    assert.match(served.disposition, /^attachment;/);
    assert.equal(served.missing, 404);
    await page.screenshot({
      path: path.join(output, "generated-image.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.locator('[data-task-id="preview-00"]').click();
    await page
      .locator("#messages")
      .getByText("Your evening routine looks good.", { exact: false })
      .waitFor();
    await page.locator("#model-button").click();
    await page
      .getByRole("button", { name: "GPT-6 Astra", exact: false })
      .click();
    await page.waitForFunction(
      () =>
        document.querySelector("#model-button").textContent === "GPT-6 Astra" &&
        !document.querySelector("#model-button").disabled,
    );
    await page.locator("#effort-button").click();
    assert.equal(await page.locator("#effort-slider").getAttribute("max"), "5");
    await page.locator("#effort-slider").fill("3");
    await page.waitForFunction(
      () =>
        document.querySelector("#effort-label").textContent === "Extra High" &&
        !document.querySelector("#effort-button").disabled,
    );
    await page.locator("#model-button").click();
    await page.screenshot({
      path: path.join(output, "model-picker.png"),
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    assert.equal(
      await page.locator("#model-button").getAttribute("aria-expanded"),
      "false",
    );
    await page.locator("#effort-button").click();
    await page.screenshot({
      path: path.join(output, "reasoning-picker.png"),
      fullPage: true,
    });
    await page.keyboard.press("Escape");
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
    const saved = await page.evaluate(
      async () => (await (await fetch("tasks/preview-00")).json()).task,
    );
    assert.deepEqual(saved.chat_settings, {
      model: "gpt-6-astra",
      reasoning_effort: "xhigh",
    });
    assert.deepEqual(
      saved.turns.at(-1).execution_settings,
      saved.chat_settings,
    );
    await page.locator("#effort-button").click();
    await page.locator("#effort-slider").press("End");
    await page.waitForFunction(
      () =>
        document.querySelector("#effort-label").textContent === "Ultra" &&
        !document.querySelector("#effort-button").disabled,
    );
    await page.locator("#model-button").click();
    await page.getByRole("button", { name: "GPT-5.5", exact: true }).click();
    await page.waitForFunction(
      () =>
        document.querySelector("#model-button").textContent === "GPT-5.5" &&
        !document.querySelector("#model-button").disabled,
    );
    assert.equal(await page.locator("#effort-label").textContent(), "Medium");
    await page.locator("#effort-button").click();
    assert.equal(await page.locator("#effort-slider").getAttribute("max"), "3");
    await page.locator("#effort-slider").fill("2");
    await page.waitForFunction(
      () =>
        document.querySelector("#effort-label").textContent === "High" &&
        !document.querySelector("#effort-button").disabled,
    );
    await page.locator("#effort-button").click();
    await page
      .getByRole("button", {
        name: "Use add-on default reasoning",
        exact: true,
      })
      .click();
    await page.waitForFunction(
      () =>
        document.querySelector("#effort-label").textContent === "Medium" &&
        !document.querySelector("#effort-button").disabled,
    );
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
    assert.equal(await page.locator("#model-button").textContent(), "Default");
    await page.locator("#model-button").click();
    await page
      .getByRole("button", { name: "GPT-5.6 Luna", exact: true })
      .click();
    await page.locator("#effort-button").click();
    await page.locator("#effort-slider").fill("4");
    assert.equal(await page.locator("#effort-label").textContent(), "Max");
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
    assert.equal(
      await page.locator("#model-button").textContent(),
      "GPT-5.6 Luna",
    );
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
    assert.equal(await page.locator("#model-button").textContent(), "GPT-5.5");
    assert.equal(await page.locator("#effort-label").textContent(), "Medium");
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
    await page.locator("#effort-button").click();
    await page.screenshot({
      path: path.join(output, "mobile-reasoning.png"),
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 320, height: 568 });
    await page.locator("#model-button").click();
    const popover = await page.locator("#model-popover").boundingBox();
    assert(
      popover.x >= 0 && popover.y >= 0 && popover.x + popover.width <= 320,
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 390, height: 844 });
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
    await page.locator("#effort-button").click();
    await page.screenshot({
      path: path.join(output, "mobile-dark.png"),
      fullPage: true,
      animations: "disabled",
    });
    // Long press on a phone opens the actions sheet without selecting the chat.
    const touch = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    });
    const phone = await touch.newPage();
    phone.on("pageerror", (error) => errors.push(error.message));
    await phone.goto("http://127.0.0.1:9137/preview/");
    await phone.locator(".chat-row").first().waitFor({ state: "attached" });
    await phone
      .getByRole("button", { name: "Open conversations", exact: true })
      .click();
    await phone.waitForFunction(
      () => document.querySelector("#sidebar").getBoundingClientRect().x >= 0,
    );
    const input = await touch.newCDPSession(phone);
    const press = async (hold) => {
      const row = phone.locator('[data-task-id="preview-01"]');
      await row.scrollIntoViewIfNeeded();
      const box = await row.boundingBox();
      const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      await input.send("Input.dispatchTouchEvent", {
        type: "touchStart",
        touchPoints: [point],
      });
      await phone.waitForTimeout(hold);
      await input.send("Input.dispatchTouchEvent", {
        type: "touchEnd",
        touchPoints: [],
      });
    };
    await press(700);
    await phone.locator("#chat-menu.sheet").waitFor({ state: "visible" });
    assert.equal(await phone.locator("#chat-title").textContent(), "New chat");
    assert.equal(
      await phone.locator("#chat-menu-title").textContent(),
      "Check the energy dashboard",
    );
    await phone.screenshot({
      path: path.join(output, "mobile-chat-menu.png"),
      animations: "disabled",
    });
    await phone.getByRole("menuitem", { name: "Cancel" }).click();
    assert.equal(await phone.locator("#chat-menu").isHidden(), true);
    await press(50);
    await phone
      .locator("#messages")
      .getByText("The dashboard configuration is valid.", { exact: true })
      .waitFor();
    await touch.close();
    assert.deepEqual(errors, []);
    console.log(
      "PASS: chat actions (pin, rename, delete, long press), generated image attachments, saved model/reasoning choices, model compatibility, keyboard/reset controls, history, pagination, continuation, new chats, drafts, safe text, settings, resize, mobile and dark mode. Screenshots: " +
        output,
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
