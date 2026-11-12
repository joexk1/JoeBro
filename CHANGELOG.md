# Changelog

## Unreleased

### Added

- **Night Shift.** Queue coding tasks. They run overnight, each in its own git branch and worktree, on whichever linked machine has the repo, with Claude Code, Codex or OpenCode. Morning review cards show the diff, the test result and a summary; approve (keep or merge locally), reject or ask for a follow-up, from the Night Shift panel or the iPhone lock screen. It never pushes.
- **Shared Memory.** One project memory that every agent reads and writes over MCP. `joebro-agents memory import --write` brings in CLAUDE.md and AGENTS.md once and leaves a short pointer. The session brief is capped at about 400 tokens; in testing a 1,460-token CLAUDE.md became 454 tokens per session. Facts strengthen when used and fade when not; nothing is deleted. Personal facts never reach a repo or a coding agent.
- **One Memory screen.** JoeBro's own memory (Brain and phone memory) is now part of Shared Memory: scope filter, search, strength, provenance, pin, edit, archive and history.
- **Live subagents.** Subagents appear nested under their parent in the Agents panel, with a count. Claude Code is verified; Codex and OpenCode are best effort.
- **Push-to-start** for the Dynamic Island, so new runs and subagents appear while the phone app is in the background.
- **Resume.** The personal agent can list and resume recent coding sessions ("pick up what I was working on yesterday and add tests"). Recent sessions have a Resume button in the Night Shift panel.
- **Morning briefing and proactive autonomy** are now part of the shipped app, including review cards in the briefing.
- **Demo mode** for screenshots: launch with `-JoeBroDemo`.
- **Long documents in parts.** `create_document` takes `append=true`, so a long file is written in several calls at the same path. Each part is saved, and shows in the editor, as it lands. If a tool call is cut off at the model's output limit, JoeBro now says so and the model retries in smaller parts instead of failing with a misleading error.
- **Safer edits.** `edit_document` find/replace refuses an ambiguous match, says how many it found and where, and takes `replace_all` or `occurrence`.
- **Paged reads.** `read_file` takes `lines` and `read_pdf` takes `pages`. A long file or PDF comes back one window at a time with a footer saying what was shown and how to ask for the next.
- **Word text.** Word files are read with their tables, lists, headers, footers, footnotes, endnotes and comments. A Word document attached to a chat shows its first part in the turn, and the new `read_attachment` tool reads the rest.
- A reply that ran several tools shows one **steps** row with a plain-language summary. Expand it to see each call.

### Changed

- The documents list returns every document unless a limit is asked for. It used to stop at 50.

## 1.4.0 - 2026-11-03

Upgrading: nothing to do. Proactive mode is off, and nothing changes until you link an agent or install a plugin.

### Added

- **Coding agents.** Link Claude Code, Codex and opencode with [joebro-plugins](https://github.com/joexk1/joebro-plugins). They show up in the new **Agents** panel, on the phone and in the Dynamic Island: working, waiting for you, finished. JoeBro can check on them and queue a follow-up that they pick up when their turn ends.
- **Approve from anywhere.** An agent waiting on a permission prompt gets **Approve** and **Deny** in the Agents panel and on the iPhone lock screen (Face ID to approve). This needs the agent to run in kitty or tmux.
- **Pings.** Any linked agent can ping your phone when it's done or stuck: at most one a minute and 30 a day per agent.
- **Plugins (`joebro.plugin/1`).** A plugin is a folder with a manifest that can add tools, a guardrail and its own panel in the new **Plugins** tab. Installs are checked and staged, the previous version is kept for rollback, and a new plugin arrives switched off. Panels have no network access. Your coding agents can build them through the joebro MCP server. The standard is in `JoeBro/Backend/PLUGINS.md`.
- **Proactive mode** (**off** by default, then read only or full). JoeBro reads your email subjects, recent documents and calendar in the background. In read only it tells you about confirmed plans; in full it adds them to your calendar, with undo. Email is read without marking anything as read.
- **Shopping list.** Only you, or JoeBro from your own words, add to it.
- **Apple Health.** The phone can send daily totals (steps, sleep, heart rate, workouts) to your own backend, with a Sunday-evening recap while proactive mode is on.
- The morning briefing starter mentions your agents when any are linked.

### Changed

- **Computer use is now Agent Desktop.** With Vercel's agent-desktop installed (`npm i -g agent-desktop`), JoeBro reads apps as an accessibility tree and acts without moving your mouse or stealing focus. Without it, the built-in osascript version is used as before. Blocked apps stay blocked.
- Tools shows each plugin's version, tools, panels and any install error.

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
