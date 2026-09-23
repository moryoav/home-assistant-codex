# Codex CLI Worker App

Run Codex CLI tasks against your Home Assistant configuration folder from Home Assistant.

The app provides a responsive chat UI with saved conversations, a resizable desktop sidebar and a mobile chat drawer, a token-protected local worker API for the Codex integration, device-code sign-in for Codex CLI, task snapshots, task logs, and notifications when work completes or needs input.

For setup, security notes, and action examples, see the repository root `README.md` and this app's `DOCS.md`.

Open an existing chat to continue with its saved Codex context, or choose **New chat** for a separate conversation. Account controls and workspace instructions are under **Settings**.

Ask for an image and Codex shows the generated picture in the chat, with a full-size link and a download button. Images stay with their conversation across restarts.

Attach screenshots, photos, or design mockups to a message with the paperclip button, by pasting, or by dropping files onto the composer. Codex sees them with your message, and they stay with the conversation.

Hover over a chat and use its **⋯** button, right-click the row, or long-press it on a phone to pin, rename, or delete the conversation. Pinned chats stay at the top of the list, and deleting asks for confirmation first.

Use the model name and reasoning label below the message box to customize each conversation. Choices are saved across restarts and apply to the next message. The model menu's **Default** option and the reasoning picker's reset button restore the add-on defaults. The slider only offers reasoning levels supported by the selected model.

![Saved conversations in the desktop chat UI](https://raw.githubusercontent.com/moryoav/home-assistant-codex/v0.1.46/examples/chat-ui/desktop.png)
