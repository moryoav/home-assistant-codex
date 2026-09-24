// Run against web_fixture.py with Playwright available on NODE_PATH.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const zlib = require("node:zlib");

/** Build a tiny valid PNG so the upload checks need no binary fixture. */
function pngBuffer(width = 8, height = 6) {
  const table = [...Array(256)].map((_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = (buf) => {
    let c = 0xffffffff;
    for (const b of buf) c = table[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (tag, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(tag), data]);
    const sum = Buffer.alloc(4);
    sum.writeUInt32BE(crc(body));
    return Buffer.concat([len, body, sum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, 0x80)]);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(Buffer.concat(Array(height).fill(row)))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

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
    // A finished exchange keeps its steps behind a collapsed toggle.
    const storedToggle = page.locator("#activity .activity-toggle");
    await storedToggle.waitFor();
    assert.equal(await storedToggle.textContent(), "Show activity (4 steps)");
    assert.equal(await page.locator("#activity-steps").isVisible(), false);
    await storedToggle.click();
    assert.equal(await page.locator("#activity .step").count(), 4);
    assert.equal(
      await page.locator("#activity .step-command .step-text").textContent(),
      "cat /config/automations.yaml",
    );
    await page.getByRole("button", { name: "Show output" }).click();
    await page.locator("#activity .step-output").waitFor();
    await page.screenshot({
      path: path.join(output, "activity-finished.png"),
      fullPage: true,
    });
    await storedToggle.click();
    assert.equal(await page.locator("#activity-steps").isVisible(), false);
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
    assert.equal(
      await page.locator("#messages .check.check-valid").textContent(),
      "✓Home Assistant configuration check passed",
    );
    await page.locator('[data-task-id="preview-03"]').click();
    await page.locator("#messages .check.check-invalid").waitFor();
    assert.match(
      await page.locator("#messages .check-invalid .check-detail").textContent(),
      /required key 'trigger'/,
    );
    await page.screenshot({
      path: path.join(output, "config-check-failed.png"),
      fullPage: true,
    });
    await page.locator('[data-task-id="preview-01"]').click();
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
    // Steps appear one by one while the simulated run works, expanded by default.
    await page
      .locator("#activity.running .step-command.is-running")
      .waitFor({ timeout: 15000 });
    assert.equal(await page.locator("#activity-steps").isVisible(), true);
    await page.screenshot({
      path: path.join(output, "activity-running.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page
      .locator("#messages")
      .getByText("Preview response: Apply the first suggestion.", {
        exact: true,
      })
      .waitFor();
    await page.waitForFunction(
      () =>
        document.querySelector("#activity .activity-toggle")?.textContent ===
        "Show activity (4 steps)",
    );
    assert.equal(await page.locator("#activity").count(), 1);
    assert.equal(await page.locator("#activity .step.is-running").count(), 0);
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
    // The reasoning chip uses an icon chevron, not a text glyph.
    assert.equal(
      await page.locator("#effort-button svg.picker-chevron").count(),
      1,
    );
    // Attach an image to a new chat: pending strip, remove, re-add, send.
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    const shot = pngBuffer();
    await page.locator("#file-input").setInputFiles([
      { name: "dashboard shot.png", mimeType: "image/png", buffer: shot },
      {
        name: "notes.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("not an image"),
      },
    ]);
    await page.locator(".pending-file").waitFor();
    assert.equal(await page.locator(".pending-file").count(), 1);
    assert.match(
      await page.locator("#error").textContent(),
      /notes\.txt is not a PNG/,
    );
    await page
      .getByRole("button", { name: "Remove dashboard shot.png" })
      .click();
    assert.equal(await page.locator(".pending-file").count(), 0);
    assert.equal(await page.locator("#pending-files").isHidden(), true);
    await page.locator("#file-input").setInputFiles({
      name: "dashboard shot.png",
      mimeType: "image/png",
      buffer: shot,
    });
    await page.locator(".pending-file").waitFor();
    await page.locator("#message").fill("Why does this card look wrong?");
    await page.locator("#composer").screenshot({
      path: path.join(output, "attach-pending.png"),
      animations: "disabled",
    });
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page
      .locator("#messages")
      .getByText("Preview response: Why does this card look wrong?", {
        exact: true,
      })
      .waitFor();
    const sentImage = page.locator(
      "#messages .user-attachments img.attachment-image",
    );
    await sentImage.waitFor();
    assert.equal(await sentImage.count(), 1);
    assert.equal(await page.locator(".pending-file").count(), 0);
    assert.match(
      await page
        .locator("#messages .user-attachments figcaption")
        .textContent(),
      /dashboard shot\.png/,
    );
    assert.deepEqual(
      await page.evaluate(async () => {
        const src = document.querySelector(
          "#messages .user-attachments img",
        ).src;
        const response = await fetch(src);
        return [response.status, response.headers.get("content-type")];
      }),
      [200, "image/png"],
    );
    // Timing regressions, made deterministic by gating image decoding: a
    // batch stays with the chat it was picked in, and Send waits for images
    // that are still being prepared.
    await page.evaluate(() => {
      const original = window.createImageBitmap.bind(window);
      window.__bitmapGates = [];
      window.__restoreBitmap = () => {
        window.createImageBitmap = original;
      };
      window.createImageBitmap = (...args) =>
        new Promise((resolve) => {
          window.__bitmapGates.push(() => resolve(original(...args)));
        });
    });
    const releaseDecode = async () => {
      await page.waitForFunction(() => window.__bitmapGates.length > 0);
      await page.evaluate(() => window.__bitmapGates.shift()());
    };
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page.locator("#file-input").setInputFiles([
      { name: "first.png", mimeType: "image/png", buffer: shot },
      { name: "second.png", mimeType: "image/png", buffer: shot },
    ]);
    await page.waitForFunction(() => window.__bitmapGates.length === 1);
    assert.equal(await page.locator(".pending-file.preparing").count(), 1);
    // Switch chats while the first image is still decoding.
    await page.locator('[data-task-id="preview-00"]').click();
    await page
      .locator("#messages")
      .getByText("Your evening routine looks good.", { exact: false })
      .first()
      .waitFor();
    assert.equal(await page.locator(".pending-file").count(), 0);
    await releaseDecode();
    await releaseDecode();
    assert.equal(await page.locator(".pending-file").count(), 0);
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".pending-file:not(.preparing)").length ===
          2 &&
        document.querySelectorAll(".pending-file.preparing").length === 0,
    );
    // Send stays disabled until the third image is ready, then sends all three.
    await page.locator("#message").fill("Compare these three.");
    await page.locator("#file-input").setInputFiles({
      name: "third.png",
      mimeType: "image/png",
      buffer: shot,
    });
    await page.waitForFunction(() => window.__bitmapGates.length === 1);
    assert.equal(await page.locator(".pending-file.preparing").count(), 1);
    assert.equal(await page.locator("#send").isDisabled(), true);
    await page.locator("#message").press("Enter");
    assert.equal(await page.locator(".answer").count(), 0);
    await releaseDecode();
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".pending-file:not(.preparing)").length ===
        3,
    );
    assert.equal(await page.locator("#send").isDisabled(), false);
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page
      .locator("#messages")
      .getByText("Preview response: Compare these three.", { exact: true })
      .waitFor();
    assert.equal(
      await page.locator("#messages .user-attachments img").count(),
      3,
    );
    assert.equal(await page.locator(".pending-file").count(), 0);
    await page.evaluate(() => window.__restoreBitmap());
    // An image without a MIME type, as some drop and clipboard sources
    // deliver, is accepted; the worker checks the bytes.
    await page.evaluate(async (bytes) => {
      await addUploads([
        new File([new Uint8Array(bytes)], "untyped-shot", { type: "" }),
      ]);
    }, [...shot]);
    assert.equal(await page.locator(".pending-file").count(), 1);
    assert.equal(
      await page.locator(".pending-name").textContent(),
      "untyped-shot",
    );
    await page.getByRole("button", { name: "Remove untyped-shot" }).click();
    // A file the picker cannot deliver is reported instead of vanishing.
    await page.evaluate(async () => {
      await acceptFiles([new File([], "cloud-photo.jpg", { type: "image/jpeg" })]);
    });
    assert.equal(await page.locator(".pending-file").count(), 0);
    assert.match(
      await page.locator("#error").textContent(),
      /cloud-photo\.jpg is empty or could not be read/,
    );
    await page.getByRole("button", { name: "Open settings" }).click();
    await page
      .getByText("Signed in (local preview)", { exact: true })
      .waitFor();
    await page.locator("#agents").fill("Keep edits focused.");
    await page.getByRole("button", { name: "Save instructions" }).click();
    await page.getByText("Instructions saved.").waitFor();
    await page.getByRole("button", { name: "Close settings" }).click();
    // The open chat, here the one created by sending a message above, is
    // remembered across a reload of the panel.
    const openTitle = await page.locator("#chat-title").textContent();
    assert.notEqual(openTitle, "New chat");
    await page.reload();
    // The remembered chat is opened by the boot script itself, so the
    // welcome screen is never shown first.
    assert.notEqual(await page.locator("#chat-title").textContent(), "New chat");
    await page.locator(".chat-row").first().waitFor();
    assert.equal(
      await page.locator("#divider").getAttribute("aria-valuenow"),
      "335",
    );
    await page.locator("#chat-title").getByText(openTitle).waitFor();
    await page.locator('[data-task-id="preview-03"]').click();
    await page.locator("#chat-title").getByText("Earlier chat 3").waitFor();
    await page.reload();
    assert.notEqual(await page.locator("#chat-title").textContent(), "New chat");
    await page.locator("#chat-title").getByText("Earlier chat 3").waitFor();
    assert.equal(
      await page
        .locator('[data-task-id="preview-03"]')
        .getAttribute("aria-current"),
      "true",
    );
    assert.equal(await page.locator("#error").isHidden(), true);
    // A remembered chat that no longer exists falls back to a new chat quietly.
    await page.evaluate(() =>
      localStorage.setItem("codex-last-chat", "preview-gone"),
    );
    await page.reload();
    await page.locator(".chat-row").first().waitFor();
    assert.equal(await page.locator("#chat-title").textContent(), "New chat");
    assert.equal(await page.locator("#error").isHidden(), true);
    assert.equal(
      await page.evaluate(() => localStorage.getItem("codex-last-chat")),
      null,
    );
    // Choosing New chat is remembered too.
    await page.locator('[data-task-id="preview-03"]').click();
    await page.locator("#chat-title").getByText("Earlier chat 3").waitFor();
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page.reload();
    await page.locator(".chat-row").first().waitFor();
    assert.equal(await page.locator("#chat-title").textContent(), "New chat");
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
    await phone.getByRole("menuitem", { name: "Cancel" }).click();
    assert.equal(await phone.locator("#chat-menu").isHidden(), true);
    await press(50);
    await phone
      .locator("#messages")
      .getByText("The dashboard configuration is valid.", { exact: true })
      .waitFor();
    // The screenshot the user attached shows with their message.
    assert.equal(
      await phone.locator("#messages .user-attachments img").count(),
      1,
    );
    await touch.close();
    // Android WebViews get a single-select file input; everyone else keeps multiple.
    assert.equal(await page.locator("#file-input").getAttribute("multiple"), "");
    const webview = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
      userAgent:
        "Mozilla/5.0 (Linux; Android 14; NE2213 Build/UKQ1.230924.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/128.0.0.0 Mobile Safari/537.36 Home Assistant/2025.9.1 (Android 14; NE2213)",
    });
    const app = await webview.newPage();
    app.on("pageerror", (error) => errors.push(error.message));
    await app.goto("http://127.0.0.1:9137/preview/");
    await app.locator(".chat-row").first().waitFor({ state: "attached" });
    assert.equal(await app.locator("#file-input").getAttribute("multiple"), null);
    await app.locator("#file-input").setInputFiles({
      name: "IMG_20260924.jpg",
      mimeType: "image/jpeg",
      buffer: shot,
    });
    await app.locator(".pending-file").waitFor();
    await webview.close();
    assert.deepEqual(errors, []);
    console.log(
      "PASS: attached images (pick, reject, remove, send, render, batch stays with its chat, send waits for decoding), chat actions (pin, rename, delete, long press), generated image attachments, saved model/reasoning choices, model compatibility, keyboard/reset controls, history, pagination, continuation, new chats, drafts, reopening the last chat, safe text, settings, resize, mobile and dark mode. Screenshots: " +
        output,
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
