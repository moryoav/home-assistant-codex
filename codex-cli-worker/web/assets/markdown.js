"use strict";
/* global marked */
/**
 * Markdown for chat messages.
 *
 * Messages are untrusted text. The bundled marked library only splits a message
 * into tokens; the elements are built here one at a time and every piece of
 * text is added as text, so nothing in a message can become markup or script.
 */
// Unusual text, such as thousands of unclosed asterisks, can keep the parser
// busy for minutes. A message that is very long or takes too long to parse is
// shown as plain text instead.
const MARKDOWN_MAX_LENGTH = 50000;
const MARKDOWN_BUDGET_MS = 300;
let markdownDeadline = 0;
/** Stop parsing once the time is used up; otherwise let the parser carry on. */
function markdownTick() {
  if (performance.now() > markdownDeadline)
    throw new Error("Markdown parsing took too long");
  return false;
}
const markdownParser = new marked.Marked({
  gfm: true,
  // A single Enter stays a line break, as it was before messages were formatted.
  breaks: true,
  tokenizer: {
    // The first rules tried for each block and for each piece of text.
    space: markdownTick,
    escape: markdownTick,
    // HTML is shown as typed and never interpreted. Leaving it to the text
    // rules keeps the Markdown around it working.
    html() {},
    // The exception is <br>, the only way to break a line inside a table cell.
    tag(src) {
      const match = /^<br\s*\/?>/i.exec(src);
      if (match) return { type: "html", raw: match[0], text: match[0] };
    },
  },
});
const markdownInlineTags = { strong: "strong", em: "em", del: "del" };
const markdownLinkSchemes = ["http:", "https:", "mailto:"];
/** Return a link target that is safe to open, or nothing for any other URL. */
function markdownUrl(href) {
  try {
    const url = new URL(href);
    return markdownLinkSchemes.includes(url.protocol) ? url.href : "";
  } catch (_) {
    // Relative targets such as file paths would open inside the worker's own page.
    return "";
  }
}
/** Build a link that opens outside the chat without passing the page along. */
function markdownLink(href, title) {
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noreferrer noopener";
  if (title) link.title = title;
  return link;
}
/** Build the read-only tick box of a task list item. */
function markdownCheckbox(token) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(token.checked);
  box.disabled = true;
  return box;
}
/** Add the text-level parts of a block: emphasis, code, links, line breaks. */
function markdownInline(tokens, parent, inCell = false) {
  for (const token of tokens || []) {
    if (markdownInlineTags[token.type]) {
      const node = document.createElement(markdownInlineTags[token.type]);
      parent.append(node);
      markdownInline(token.tokens, node, inCell);
    } else if (token.type === "codespan") {
      const code = document.createElement("code");
      code.textContent = token.text;
      parent.append(code);
    } else if (token.type === "br" || (inCell && token.type === "html")) {
      parent.append(document.createElement("br"));
    } else if (token.type === "checkbox") {
      parent.append(markdownCheckbox(token));
    } else if (token.type === "link") {
      const href = markdownUrl(token.href);
      // A target that is not a web or mail address keeps its label only.
      const node = href
        ? markdownLink(href, token.title)
        : document.createElement("span");
      parent.append(node);
      markdownInline(token.tokens, node, inCell);
    } else if (token.type === "image") {
      // Images are never loaded from a message, because a remote one would
      // tell its server that the message was read. Offer a link instead.
      const href = parent.closest("a") ? "" : markdownUrl(token.href);
      const label = token.text || token.href;
      if (href) {
        const link = markdownLink(href, token.title);
        link.textContent = label;
        parent.append(link);
      } else parent.append(label);
    } else if (token.tokens) markdownInline(token.tokens, parent, inCell);
    else
      parent.append(
        ["text", "escape"].includes(token.type) ? token.text : token.raw,
      );
  }
}
/** Build one table cell, aligned as its column asks. */
function markdownCell(cell, tag) {
  const node = document.createElement(tag);
  if (cell.align) node.style.textAlign = cell.align;
  markdownInline(cell.tokens, node, true);
  return node;
}
/** Add the block-level parts of a message: paragraphs, lists, code, tables. */
function markdownBlocks(tokens, parent) {
  for (const token of tokens || []) {
    if (token.type === "space" || token.type === "def") continue;
    if (token.type === "paragraph") {
      const node = document.createElement("p");
      markdownInline(token.tokens, node);
      parent.append(node);
    } else if (token.type === "heading") {
      // The page already uses h1 and h2, so message headings start below them.
      const node = document.createElement(`h${Math.min(token.depth + 2, 6)}`);
      markdownInline(token.tokens, node);
      parent.append(node);
    } else if (token.type === "code") {
      const block = document.createElement("div");
      block.className = "md-code";
      const lang = (token.lang || "").trim().split(/\s+/)[0];
      if (lang) {
        const label = document.createElement("div");
        label.className = "md-lang";
        label.textContent = lang;
        block.append(label);
      }
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = token.text;
      pre.append(code);
      block.append(pre);
      parent.append(block);
    } else if (token.type === "blockquote") {
      const node = document.createElement("blockquote");
      markdownBlocks(token.tokens, node);
      parent.append(node);
    } else if (token.type === "list") {
      const list = document.createElement(token.ordered ? "ol" : "ul");
      if (token.ordered && Number(token.start) !== 1)
        list.start = Number(token.start);
      for (const item of token.items) {
        const node = document.createElement("li");
        if (item.task) node.className = "md-task";
        markdownBlocks(item.tokens, node);
        list.append(node);
      }
      parent.append(list);
    } else if (token.type === "checkbox") {
      parent.append(markdownCheckbox(token));
    } else if (token.type === "table") {
      // The wrapper scrolls a wide table sideways instead of the whole chat.
      const wrapper = document.createElement("div");
      wrapper.className = "md-table";
      const table = document.createElement("table");
      const head = document.createElement("tr");
      for (const cell of token.header) head.append(markdownCell(cell, "th"));
      table.createTHead().append(head);
      const body = table.createTBody();
      for (const cells of token.rows) {
        const row = document.createElement("tr");
        for (const cell of cells) row.append(markdownCell(cell, "td"));
        body.append(row);
      }
      wrapper.append(table);
      parent.append(wrapper);
    } else if (token.type === "hr") parent.append(document.createElement("hr"));
    // The text of a list item that has no blank lines around it.
    else if (token.tokens) markdownInline(token.tokens, parent);
    else parent.append(token.text ?? token.raw);
  }
}
// The chat is redrawn whenever it changes, so each message is parsed only once.
const markdownCache = new Map();
/** Split a message into Markdown tokens, or return null to show it as plain text. */
function markdownTokens(text) {
  if (text.length > MARKDOWN_MAX_LENGTH) return null;
  if (markdownCache.has(text)) return markdownCache.get(text);
  let tokens = null;
  markdownDeadline = performance.now() + MARKDOWN_BUDGET_MS;
  try {
    tokens = markdownParser.lexer(text);
  } catch (_) {
    // Too slow, or nested too deeply for the parser.
  }
  if (markdownCache.size >= 200) markdownCache.clear();
  markdownCache.set(text, tokens);
  return tokens;
}
/** Build a message element with its text formatted as Markdown. */
function renderMarkdown(text, className) {
  const node = document.createElement("div");
  node.className = className;
  text = String(text || "");
  const tokens = markdownTokens(text);
  if (tokens)
    try {
      markdownBlocks(tokens, node);
      node.classList.add("markdown");
      return node;
    } catch (_) {
      node.replaceChildren();
    }
  node.textContent = text;
  return node;
}
/** Return a message without its Markdown symbols, for one-line previews. */
function markdownPlain(text) {
  const node = renderMarkdown(text, "");
  for (const label of node.querySelectorAll(".md-lang")) label.remove();
  // Blocks carry no spaces between them, so keep their words apart.
  for (const part of node.querySelectorAll(
    "p, li, h3, h4, h5, h6, pre, th, td, br",
  )) {
    part.before(" ");
    part.after(" ");
  }
  return node.textContent.replace(/\s+/g, " ").trim();
}
