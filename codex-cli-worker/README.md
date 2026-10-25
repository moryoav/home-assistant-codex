# Codex CLI Worker App

Run Codex CLI tasks against your Home Assistant configuration folder from Home Assistant.

The app provides a responsive chat UI with saved conversations, a resizable desktop sidebar and a mobile chat drawer, a token-protected local worker API for the Codex integration, device-code sign-in for Codex CLI, per-file backups of changed files, task logs, and notifications when work completes or needs input.

I recommend `danger-full-access` for working freely in Home Assistant. The name sounds scary, but the app still offers backups, configuration checks, and verification. `workspace-write` remains the default. In sandboxed modes, the chat asks you to **Allow once** or **Deny** commands and file changes that need extra access. See [sandbox modes, safeguards, and permission requests](https://github.com/moryoav/home-assistant-codex/blob/main/codex-cli-worker/DOCS.md#sandbox).

## Dashboard browser verification

**Codex can inspect your dashboards in a real browser at desktop and mobile sizes.** It can review screenshots during a task and revise its work, with images and findings kept in the conversation. Checks report missing cards, failed resources, and browser errors. Saved storage dashboards also receive automatic captures within the task's remaining time.

Browser sign-in is automatic and uses a temporary, local, read-only identity. Update both the app and integration, and leave **Enable built-in browser** on. Ask for a specific dashboard view to inspect it. See [setup and verification limits](https://github.com/moryoav/home-assistant-codex/blob/main/codex-cli-worker/VERIFICATION.md).

For setup, security notes, and action examples, see the [repository README](https://github.com/moryoav/home-assistant-codex/blob/main/README.md) and [app documentation](https://github.com/moryoav/home-assistant-codex/blob/main/codex-cli-worker/DOCS.md).

When a dashboard inspection blocks additional browser requests, open **Review blocked browser requests** in its result to approve a selected batch and retry the capture. Choose **Remember for this dashboard** to reuse those exceptions, and revoke them under **Settings > Browser permissions**. Approving a custom request is a trust decision. Known controls, writes, URL signing and camera signaling stay blocked. See [permission scopes and limits](https://github.com/moryoav/home-assistant-codex/blob/main/codex-cli-worker/VERIFICATION.md#approving-additional-browser-requests).

## History and statistics

Codex can analyze Home Assistant state history and recorder statistics through separate read-only MCP tools. They use the worker's automatic authentication and work in sandboxed modes without shell approval or the `HA_TOKEN` option. Queries support multiple IDs, explicit time windows, and five-minute, hourly, daily, weekly, and monthly statistics. Results are saved in private temporary JSON files so Codex can calculate over the data without large tool outputs being cut off. Query limits and any incomplete series are reported explicitly. See [the tools and limits](https://github.com/moryoav/home-assistant-codex/blob/main/codex-cli-worker/DOCS.md#historical-data-and-statistics).

## Local Home Assistant documentation

The app keeps a local copy of the official Home Assistant documentation, refreshed after each Home Assistant update, so Codex can look things up without a web search. Web search stays available for custom integrations and cards. See [local documentation](https://github.com/moryoav/home-assistant-codex/blob/main/codex-cli-worker/DOCS.md#local-home-assistant-documentation).

## Conversations

Open an existing chat to continue with its saved Codex context, or choose **New chat** for a separate conversation. Account controls and workspace instructions are under **Settings**.

The chat formats Markdown in your messages and in Codex's answers. Wrap code in triple backticks to send it as a code block; inline code, headings, lists, tables, and links are formatted too.

Use **Copy response** below an answer, including a saved answer, to copy its original Markdown with code blocks and YAML indentation. If automatic copying is blocked, a dialog offers selectable text for manual copying. Images, activity logs, and separate configuration, backup, and verification cards are not included.

While one chat is working, messages you send in other chats wait in a queue and start in order when it finishes. You can edit or remove a waiting message until it starts.

When Codex needs a decision, it can offer up to three answers as buttons under its question, and on the notification if you use a mobile app notify service. Pick one, or type a different answer.

Ask for an image and Codex shows the generated picture in the chat, with a full-size link and a download button. Images stay with their conversation across restarts.

Attach screenshots, photos, or design mockups to a message with the paperclip button, by pasting, or by dropping files onto the composer. Codex sees them with your message, and they stay with the conversation.

Hover over a chat and use its **⋯** button, right-click the row, or long-press it on a phone to pin, rename, or delete the conversation. Pinned chats stay at the top of the list, and deleting asks for confirmation first.

Use the model name and reasoning label below the message box to customize each conversation. Choices are saved across restarts and apply to the next message. A chat that has no model of its own names the model it runs on: the one set in the add-on options, or the one Codex picks when that option is `default`. Choosing that model in the menu, or the reasoning picker's reset button, restores the add-on default. The slider only offers reasoning levels supported by the selected model.

![Saved conversations in the desktop chat UI](https://raw.githubusercontent.com/moryoav/home-assistant-codex/v0.1.46/examples/chat-ui/desktop.png)
