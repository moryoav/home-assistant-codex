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
    assert.deepEqual(errors, []);
    console.log(
      "PASS: saved model/reasoning choices, model compatibility, keyboard/reset controls, history, pagination, continuation, new chats, drafts, safe text, settings, resize, mobile and dark mode. Screenshots: " +
        output,
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
