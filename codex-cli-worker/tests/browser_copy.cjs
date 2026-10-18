// Run against web_fixture.py with Playwright available on NODE_PATH.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const base = "http://127.0.0.1:9137/preview/";
const first =
  '**Done.**\n\n```yaml\ntrigger:\n  - platform: sun\n    offset: "-00:20:00"\n# Più luce ☀\n```\n\nRead the [documentation](https://example.com/).';
const second = "A different answer.";
const long =
  "## Long answer\n\n```yaml\n" + "  key: value  \n".repeat(4000) + "```\n";
const question =
  "  Keep this indentation?\n\n</textarea><script>window.copyInjected = true</script>\n";

/** Serve controlled answers through the same task endpoint as a saved conversation. */
async function openAnswers(page, url = base) {
  await page.route("**/preview/tasks/preview-04", async (route) => {
    const response = await route.fetch({
      url: new URL("tasks/preview-04", base).href,
    });
    const payload = await response.json();
    const saved = payload.task.turns[0];
    payload.task.turns = [
      {
        ...saved,
        turn_id: "first",
        summary: "**Done.**",
        details:
          '```yaml\ntrigger:\n  - platform: sun\n    offset: "-00:20:00"\n# Più luce ☀\n```',
        question: "Read the [documentation](https://example.com/).",
      },
      {
        ...saved,
        turn_id: "second",
        summary: second,
        details: second,
        question: second,
      },
      { ...saved, turn_id: "long", summary: "", details: long, question: "" },
      {
        ...saved,
        turn_id: "image-only",
        summary: "",
        details: "",
        question: "",
        attachments: [
          {
            attachment_id: "5f1d3c9e8b7a4c2d9e0f1a2b3c4d5e6f",
            mime_type: "image/png",
            url: "/tasks/preview-02/attachments/5f1d3c9e8b7a4c2d9e0f1a2b3c4d5e6f",
            name: "image.png",
          },
        ],
      },
      {
        ...saved,
        turn_id: "question-only",
        summary: "",
        details: "",
        question,
      },
    ];
    await route.fulfill({ json: payload });
  });
  await page.goto(url);
  await page.locator(".chat-row").first().waitFor();
  // The smoke suite can leave many newer chats ahead of this saved example.
  while (!(await page.locator('[data-task-id="preview-04"]').count())) {
    const older = page.getByRole("button", { name: "Load older chats" });
    assert.equal(
      await older.isVisible(),
      true,
      "the saved Markdown example must still exist",
    );
    const count = await page.locator(".chat-row").count();
    await older.click();
    await page.waitForFunction(
      (count) => document.querySelectorAll(".chat-row").length > count,
      count,
    );
  }
  await page.locator('[data-task-id="preview-04"]').click();
  await page
    .getByRole("button", { name: "Copy response", exact: true })
    .first()
    .waitFor();
}

/** Windows and Unix clipboards can expose different newline conventions. */
const lines = (text) => text.replace(/\r\n/g, "\n");

/** Wait for this action's clipboard write before checking the clipboard. */
async function copyAnswer(button) {
  await button.click();
  await button.locator("..").getByText("Copied!", { exact: true }).waitFor();
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
    args: [
      "--host-resolver-rules=MAP codex-copy.test 127.0.0.1",
      "--no-proxy-server",
    ],
  });
  try {
    const context = await browser.newContext({
      permissions: ["clipboard-read", "clipboard-write"],
      viewport: { width: 1440, height: 1000 },
    });
    const errors = [];
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await openAnswers(page);
    const copy = page.getByRole("button", {
      name: "Copy response",
      exact: true,
    });
    assert.equal(
      await copy.count(),
      4,
      "image-only answers have no text-copy action",
    );
    await page.evaluate(() => {
      window.nativeCopyCalls = 0;
      const native = navigator.clipboard.writeText.bind(navigator.clipboard);
      navigator.clipboard.writeText = (text) => {
        window.nativeCopyCalls++;
        return native(text);
      };
    });
    await copyAnswer(copy.nth(0));
    assert.equal(
      lines(await page.evaluate(() => navigator.clipboard.readText())),
      first,
    );
    assert.equal(await page.evaluate(() => window.nativeCopyCalls), 1);
    assert.equal(await page.locator(".copy-buffer").count(), 0);
    await copy.nth(0).scrollIntoViewIfNeeded();
    await page.screenshot({
      path: path.join(output, "copy-response-desktop.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.waitForFunction(
      () =>
        ![...document.querySelectorAll(".copy-status")].some(
          (node) => node.textContent,
        ),
    );
    // Each old response keeps its own text; identical summary/details/question appear once.
    await copyAnswer(copy.nth(1));
    assert.equal(
      lines(await page.evaluate(() => navigator.clipboard.readText())),
      second,
    );
    await copyAnswer(copy.nth(2));
    assert.equal(
      lines(await page.evaluate(() => navigator.clipboard.readText())),
      long,
    );
    await copyAnswer(copy.nth(0));
    assert.equal(
      lines(await page.evaluate(() => navigator.clipboard.readText())),
      first,
    );
    // Reloading the saved conversation must retain the copy actions and their content.
    await page.reload();
    await copyAnswer(copy.nth(1));
    assert.equal(
      lines(await page.evaluate(() => navigator.clipboard.readText())),
      second,
    );
    // A denied modern API falls through to real selection-based copying.
    await page.evaluate(() => {
      window.nativeClipboard = navigator.clipboard;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: () =>
            Promise.reject(new DOMException("Denied", "NotAllowedError")),
        },
      });
    });
    await copyAnswer(copy.nth(0));
    assert.equal(
      lines(await page.evaluate(() => window.nativeClipboard.readText())),
      first,
    );
    assert.equal(await page.locator(".copy-buffer").count(), 0);
    assert.equal(
      await copy.nth(0).evaluate((node) => node === document.activeElement),
      true,
    );
    // A missing Clipboard API also falls through, without losing indentation.
    await page.evaluate(() =>
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: undefined,
      }),
    );
    await copyAnswer(copy.nth(1));
    assert.equal(
      lines(await page.evaluate(() => window.nativeClipboard.readText())),
      second,
    );
    // When both paths fail there is no false success and the full text is selected.
    await page.evaluate(() => {
      document.execCommand = () => false;
    });
    await copy.nth(0).click();
    const dialog = page.getByRole("dialog", {
      name: "Copy response",
      exact: true,
    });
    await dialog.waitFor();
    const markdown = dialog.getByRole("textbox", { name: "Response Markdown" });
    assert.equal(await markdown.inputValue(), first);
    assert.deepEqual(
      await markdown.evaluate((node) => [
        node.readOnly,
        node.selectionStart,
        node.selectionEnd,
      ]),
      [true, 0, first.length],
    );
    assert.equal(await page.locator(".copy-status").first().textContent(), "");
    assert.equal(
      lines(await page.evaluate(() => window.nativeClipboard.readText())),
      second,
    );
    await markdown.evaluate((node) => node.setSelectionRange(2, 4));
    await dialog.getByRole("button", { name: "Select all" }).click();
    assert.deepEqual(
      await markdown.evaluate((node) => [
        node.selectionStart,
        node.selectionEnd,
      ]),
      [0, first.length],
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: "dark" });
    assert.equal(
      await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
      true,
    );
    await page.screenshot({
      path: path.join(output, "copy-response-manual-mobile.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "hidden" });
    await page.waitForFunction(
      () => document.getElementById("copy-text").value === "",
    );
    assert.equal(await page.locator("#copy-text").inputValue(), "");
    assert.equal(
      await copy.nth(0).evaluate((node) => node === document.activeElement),
      true,
    );
    await copy.nth(2).click();
    assert.equal(await markdown.inputValue(), long);
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    // A WebView may throw instead of returning false for the legacy command.
    await page.evaluate(() => {
      document.execCommand = () => {
        throw Error("Unsupported");
      };
    });
    await copy.nth(1).click();
    await dialog.waitFor();
    assert.equal(await markdown.inputValue(), second);
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    assert.equal(await page.locator(".copy-buffer").count(), 0);
    // Question-only responses preserve whitespace and treat HTML as literal text.
    await copy.nth(3).click();
    await dialog.waitFor();
    assert.equal(await markdown.inputValue(), question);
    assert.equal(await page.evaluate(() => window.copyInjected), undefined);
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    // A delayed permission result must not open an old response over a new chat.
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: () =>
            new Promise((resolve, reject) => {
              window.rejectCopy = reject;
            }),
        },
      });
    });
    await copy.nth(0).click();
    assert.equal(await copy.nth(0).isDisabled(), true);
    await page.getByRole("button", { name: "New chat", exact: true }).click();
    await page.evaluate(() =>
      window.rejectCopy(new DOMException("Denied", "NotAllowedError")),
    );
    // Drain the promise, including the legacy fallback, without an arbitrary delay.
    await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 0)));
    assert.equal(await dialog.isVisible(), false);
    assert.equal(await page.locator(".copy-buffer").count(), 0);
    // Actual non-loopback HTTP: no secure-context Clipboard API is exposed.
    const http = await context.newPage();
    http.on("pageerror", (error) => errors.push(error.message));
    await openAnswers(http, "http://codex-copy.test:9137/preview/");
    assert.equal(await http.evaluate(() => window.isSecureContext), false);
    assert.equal(
      await http.evaluate(() => typeof navigator.clipboard),
      "undefined",
    );
    await http
      .getByRole("button", { name: "Copy response", exact: true })
      .nth(0)
      .click();
    await http.getByText("Copied!", { exact: true }).waitFor();
    await page.bringToFront();
    assert.equal(
      lines(await page.evaluate(() => window.nativeClipboard.readText())),
      first,
    );
    // Home Assistant embeds the app in a same-origin iframe through Ingress.
    const embedded = await context.newPage();
    embedded.on("pageerror", (error) => errors.push(error.message));
    await openAnswers(embedded);
    await embedded.route("**/ingress-preview", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: '<!doctype html><title>Ingress test</title><iframe title="Codex" src="/preview/" style="width:100%;height:900px;border:0"></iframe>',
      }),
    );
    await embedded.goto(new URL("ingress-preview", base).href);
    await embedded
      .frameLocator("iframe")
      .getByRole("button", { name: "Copy response", exact: true })
      .first()
      .click();
    await embedded
      .frameLocator("iframe")
      .getByText("Copied!", { exact: true })
      .waitFor();
    assert.equal(
      lines(await embedded.evaluate(() => navigator.clipboard.readText())),
      first,
    );
    assert.deepEqual(errors, []);
    console.log(
      "PASS: whole Markdown/YAML, Unicode, old answers, duplicate fields, long answers, reload, native clipboard, denied/missing API, real HTTP, Ingress-style iframe, manual fallback, selection, focus, mobile dark mode, cleanup, and no page errors. Screenshots: " +
        output,
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
