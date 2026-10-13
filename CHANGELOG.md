# Changelog

## 1.3.0 - 2026-10-13

Upgrading: nothing to do. Mobile access is off until you turn it on.

### Added

- **Phone access.** Pair an iPhone with a QR code in **Settings > Mobile**, over Tailscale or your Wi-Fi, with a token. The iPhone app is [JoeBro Mobile](https://github.com/joexk1/JoeBro-Mobile).
- **The phone agent.** One chat that follows your default model and hands hands-on work to chats bound to folders.
- **Phone memory.** Plain Markdown files, editable under **Brain > Phone memory**. `about-me` is always in context, the rest by relevance.
- **Phone agent tasks.** A **Phone agent** list in **Tasks**. They run silently and only post the notification text. Morning and evening starters, created only when you press them.
- **Background agents** and an agent registry, so research, tasks and spawned agents can be listed and cancelled.
- **Inbox tab** for notices from the agent, tasks and agents.
- **Heartbeat, pulse and reminders.** All off by default. The heartbeat only calls a model when something changed, with quiet hours and a daily cap.
- **Push notifications.** Optional. Bring your own Apple key and upload it in **Settings > Mobile**.
- **Weather and location.** Opt-in. Uses open-meteo.com and nominatim.openstreetmap.org, and only while it's on.
- **File moves** between your folders through a staging area, with a confirmation before anything is sent out.
- **Image captions** for text-only models. Opt-in, and you pick the model.

### Changed

- Tasks gain a **Phone agent** list.
- The stop button works.

### Security

- The phone listener is separate from the local one. It needs the token on every request and only starts if there is one.
- The phone can't add MCP servers or plugins.
- No MCP server is ever added for you.

## 1.2.1

- Flashcards as real Anki `.apkg` decks, Hindsight-style memory, Cloak Mode.
- Security: closed an unauthenticated API hole, masked secrets, removed file-write through the document route, and stopped the model from setting its own permission mode.
- Think and Fast modes and Compact fixes. Releases are now notarized.
- The agent loop no longer dies when reasoning uses up the reply budget.

## 1.2

- The Brain map, a live graph of your memories.
- Computer Use.
