# JoeBro User Guide

The full reference: every tab, every control in the composer, every right-click menu, and every permission mode. For the 60-second setup see **[Getting Started](GETTING_STARTED.md)**.

---

## First run

1. Launch JoeBro. The local backend starts with it automatically.
2. Open **Settings** (the gear icon in the bottom-left of the sidebar) and add a model so JoeBro has something to think with:
   - **Local:** point it at an Ollama server (or anything OpenAI-compatible) on your machine or network.
   - **Cloud:** paste an API key for DeepSeek, OpenAI, Anthropic, Groq, Gemini, OpenRouter, etc.
3. Close Settings, type in the composer at the bottom of a chat, and press Return.

Optional but worth it: connect your **email** (IMAP) and **calendar** in Settings so the agent can actually work with them.

---

## The window at a glance

- **Sidebar (left):** your chats at the top, a **Bind to a folder** button and file tree in the middle, and the list of workspace tabs (Chats, Email, Calendar, Brain, Notes, Tasks, Skills, Tools, AI Check, Deep Research) below. The footer holds the **gear** (Settings) and the **◐** button (glass / appearance tuner).
- **Main panel (right):** whichever tab is selected. For Chats this is the conversation plus the composer.
- **Editor:** when a document is open it sits beside the chat. You can also pop it out into its own window from the editor's top bar.
- **New chat:** the compose icon at the top of the sidebar, or `Cmd+N`.
- **Search chats:** the search field at the top of the sidebar.

A small dot appears next to **Email** when you have unread mail, and a spinner next to **Deep Research** while a report is running ,and a dot once it completes.

---

## The composer (chat controls)

Everything you need to steer a message lives in the bar at the bottom of a chat.

- **Model picker:** choose which model answers. Each shows its provider badge. You can switch model between messages in the same chat and the context carries over.
- **Think / Fast:** extended thinking for harder questions, or fast for quick replies.
- **Agent / Chat:** *Chat* is a plain conversation. *Agent* lets the model use tools (files, terminal, web, calendar, email).
- **Web search** (magnifying glass): let the agent search the web. Available in Agent mode.
- **Terminal** (terminal icon): let the agent run shell commands. Available in Agent mode, and gated by the lock setting below.
- **Lock menu** (the padlock): sets how much file access the agent has. See "Permission modes" near the end.
- **Attach:** add files to a message.
- **Mic / voice:** dictate instead of typing.
- **Send:** Return, or the send button.

---

## Chats

Your conversations. Click one in the sidebar to open it; click the compose icon for a new one.

**Right-click a chat in the sidebar:**
- **Pin / Unpin:** keep important chats at the top.
- **Rename:** give it a title.
- **Add to Project Folder:** file it under an existing folder, or make a new one.
- **Remove from Folder:** pop it back out into the loose list.
- **Delete:** remove it.

**Organising your chats**

Once you have a few conversations going, you can tidy them up:

- **Group them into project folders.** Right-click a chat → *Add to Project Folder* and either pick a folder or create one. Folders show up in the sidebar with the chats tucked underneath. Click a folder's name to **collapse or expand** it.
- **Drag and drop.** Grab a chat and drop it **onto a folder** to file it there, drop it **back in the loose list** to take it out, or drop it **onto another chat** to reorder — the order you set sticks between launches.
- **Pin a whole folder.** Right-click a folder's name → *Pin Folder* to float it to the top of the list (with a little pin marker). *Unpin Folder* puts it back in alphabetical order.

Pinned chats always stay on top regardless of how you reorder things, so pinning and drag-ordering never fight each other.

**Right-click a message in the conversation:**
- On your own message: **Copy**, **Resend** (run it again), **Unsend** (remove it).
- On a reply: **Copy Full Response**. A copy button also appears when you hover a reply.

Agent runs show their tool steps inline (reading a file, creating an event, running a command) so you can see exactly what happened, not just the final answer. When a reply was shaped by something it knows about you, a small **Memories used** / **Plugins used** line sits at the bottom of the message (just above the tokens-per-second readout), listing the memories and background guardrail plugins that fed into that answer.

---

## Files and the document editor

JoeBro can work inside a real folder on your Mac.

- **Bind to a folder:** click it in the sidebar and choose a project folder. Its files appear as a tree.
- **Open a file:** click it. Text and Markdown open in the editor; PDFs and images open in a viewer; Word `.doc` and `.docx` open in the rich editor.
- **Right-click a file in the tree:** **Delete**.

**The editor** sits beside the chat (or pop it out with the window button in its top bar):
- Toolbar with **bold**, **italic**, **bullet list**, **font** and **text size** for rich documents.
- An **Edit / Preview** toggle (the eye) to see the rendered result.
- Word documents keep their formatting, render images inline, and show any header and footer.
- Your edits autosave. When the agent edits a document, you can require approval before its version is applied (see Settings).

When a chat is bound to a folder, the agent can read and write files there, and you will see those edits stream into the editor live.

---

## Email

Connect a mailbox over IMAP in Settings, then manage it here.

- Switch folders, filter **All / Unread / Flagged**, and search.
- Click a message to read it; click again or use the composer to reply, forward or write a new one.
- Select multiple messages for bulk actions: **Mark read**, **Mark unread**, **Archive**, **Delete**.

**Right-click a message:**
- **Select / Deselect**
- **Mark Read / Mark Unread**
- **Archive**
- **Delete**

In Agent mode you can simply ask: "summarise everything since last night and flag anything that needs a reply," and it will use the email tools rather than guessing.

---

## Calendar

A month view backed by your real calendar (macOS Calendar or CalDAV, set in Settings).

- **Natural-language quick add:** type something like "lunch with Sam Tuesday 1pm" and it creates the event.
- Click a day or event to see details.

**Right-click an event:**
- **Edit**
- **Delete**

The agent can create and manage events too, which is how "add the flight from this boarding pass to my calendar" works end to end.

---

## Brain (memory)

JoeBro's long-term memory about you, kept locally. Facts, preferences and project context that make the answers fit you.

- Add a memory with the quick-add box, or let the agent remember things for you as you chat.
- Search and pin the ones that matter.

**Right-click a memory:**
- **Edit…**
- **Pin / Unpin**
- **Delete**

You can also select several and remove them together.

---

## Tasks

Scheduled automations that run on their own, like a morning email summary or a weekly review.

- Create a task with a prompt, a schedule (**daily**, **weekly** or **monthly**) and a time. For a **weekly** task, pick which **day** it fires.
- Set the task's **Agent access** (Bound folder / Read-only / Full access). Tasks run as an agent on their own, so this sets how much of your files they can touch when they fire. Both the new-task and edit-task sheets have it.
- Tasks can be **active** or **paused**.

Two built-in housekeeping tasks ship by default: a weekly **skill audit** (prunes low-confidence skills) and a **memory audit** (clears stale memories). They're normal tasks, so you can view or adjust them here like any other.

**Right-click a task:**
- **Run now:** fire it immediately.
- **Edit…**
- **Pause / Resume**
- **Delete**

The agent can create and manage tasks for you with its task tool, so "remind me each Monday morning to plan the week" actually becomes a real scheduled task.

---

## Skills

JoeBro gets sharper the more you use it. When you do something often, it can turn that into a reusable **skill** with a confidence score, and pull it back up when it's relevant. Each skill has a name, a one-line description, a *when to use* trigger, and a step-by-step procedure.

- Browse the skills it has learned, each with its description and a confidence percentage.
- Edit, enable or remove them.

**Add a skill yourself.** The **+** menu at the top of the tab gives you three ways:
- **Write manually:** the editor opens with every field (name, description, category, *when to use*, and the procedure in Markdown). It's the same editor whether you're making a new skill or changing an existing one, so nothing is missing either way.
- **Upload markdown:** pick a `.md` file. Its first heading becomes the description and the body becomes the procedure.
- **Generate with AI:** describe the skill in a sentence and your default model drafts the whole thing. The quick-add box at the top of the tab does the same.

**Right-click a skill:**
- **Edit…**
- **Activate / Disable**
- **Delete**

A built-in weekly **skill audit** prunes low-confidence skills automatically, and a **memory audit** clears out stale memories. Both are normal tasks you can see and adjust in the Tasks tab.

---

## Tools (custom API tools, MCP servers & plugins)

The **Tools** tab is where you extend what the agent can do. Everything you add here becomes available to the model in **Agent mode**, and it decides when to reach for each one based on the descriptions you give it. There are three kinds, each in its own box.

### API Tools: connect any web API

Turn any JSON/REST API into a tool the agent can call.

1. Click **Add API Tool…**.
2. Fill in:
   - **Name:** e.g. *Company Lookup*. Keep it short and clear, the model sees it.
   - **Base URL:** the endpoint, e.g. `https://api.example.com/v2/search`. Put `{query}` anywhere in the URL to drop the model's input in exactly (`…/search?name={query}`). Leave it out and JoeBro appends the input as `?query=…`.
   - **API key** *(optional)*: stored locally and sent as an `Authorization: Bearer <key>` header. Leave blank for open APIs. When editing, leave it blank to keep the existing key. A key icon on the row marks tools that carry one.
   - **Method:** GET or POST.
   - **Description:** the most important field. The model reads this to decide *when* and *how* to call the tool, so describe what it returns and when it's useful, like *"Looks up a company's funding history by name. Use when the user asks about a startup's investors."*
3. **Add**. Toggle a tool on or off any time. The pencil edits it, the trash removes it.

Now in Agent mode you can ask something the tool answers and the model calls it instead of guessing.

### MCP Servers: plug into the wider tool ecosystem

**MCP (Model Context Protocol)** is an open standard for giving models tools, with a large ecosystem of ready-made servers (filesystem, GitHub, Slack, databases, web scrapers and more). JoeBro speaks MCP over **stdio**: you give it a launch command, it starts the server, discovers its tools, and offers them to the model.

1. Click **Add MCP Server…**.
2. Fill in:
   - **Name:** e.g. *Filesystem*.
   - **Command:** the executable that launches the server, e.g. `npx`.
   - **Arguments:** everything after the command, e.g. `-y @modelcontextprotocol/server-filesystem /Users/me/Documents`.
3. **Add**. JoeBro launches it and lists the tools it discovered under the server's name. The first launch can take a moment if `npx` has to download the package, and if something's wrong the error shows in red on the row.

Toggle a server off to hide its tools without deleting it. The pencil edits its command and args, and the agent re-discovers the tools on its next run.

> MCP servers run real programs on your Mac with whatever access the command has. Only add servers you trust, and scope their arguments (point a filesystem server at one folder, not your whole home directory).

### Plugins: richer, bundled capabilities

Plugins are drop-in capability folders. There are two kinds, marked **FOREGROUND** or **BACKGROUND** on each row:

- **Foreground (active tool):** the plugin adds a tool the agent can call, and it appears among the agent's tools like any other. The bundled **JoeBro macOS Use** plugin is one of these. It lets the agent see and control your Mac (open apps, click, type, screenshot) for real computer-use.
- **Background (guardrail):** always-on guidance that's injected into the agent's instructions on every run, with no calling required. Use these for rules and house style the agent should always follow. When a background plugin shaped a reply, it's listed in the **"Plugins used"** line at the bottom of that message, right alongside any memories used.

**Add a plugin:**
1. Click **Add Plugin…**.
2. Choose the folder containing the plugin's repo.
3. In the same dialog, pick **Foreground (active tool)** or **Background (guardrail)**, then **Add Plugin**.

**Edit a plugin** (pencil) to change its **Type** and its **Agent access**, which is the file-access level the agent must be in for the plugin to be usable. Computer-use style plugins need **Full access**. Toggle any plugin on or off with its switch. The bundled JoeBro macOS Use plugin can be re-permissioned and toggled, but not deleted.

> Foreground plugins appear and behave as tools. Background plugins never show up as tools: they quietly shape every answer and report themselves in the "Plugins used" footer.

---

## Deep Research

Ask a question and JoeBro reads many sources, then writes a structured report with citations and relevant images.

- Start a run from the search box. A spinner shows progress, and the run keeps going even if you switch tabs.
- Open any past report from the list to read it.

**Right-click a report:**
- **Copy Report:** the whole thing to your clipboard.
- **Download Markdown:** save it as a `.md` file.
- **Delete**.

Select several reports to delete them in one go. You can also kick off research from a chat by asking the agent to "research X," and it will use the research tool rather than a quick web search.

---

## AI Check

Paste text and get an estimate of how AI-written it reads, with the suspect sentences flagged. Useful for a quick sanity check on your own drafts.

---

## Message Bot (Telegram)

Control JoeBro from your phone over Telegram. The bot is your orchestrator: it manages and drives all your chats and agents, and delegates the hands-on work to them.

**What it can do:**
- Search and read across every chat, and summarise what they concluded.
- Create a new chat, bind it to a folder, and set its mode (chat or agent) and file-access level.
- Send a message into any chat and report back its reply. This is how it gets work done, including reading or editing the files bound to that chat.
- Use your email, calendar, memory, web search and deep research directly.

It never writes code or edits files itself. For any coding or file task it picks or creates a chat, sets it up, and delegates.

**Set it up** in Settings > Message Bot:
1. In Telegram, open @BotFather, send `/newbot`, and copy the token it gives you.
2. Paste the token into **Bot token** and turn on **Enable Message Bot**.
3. Message your new bot once. If you are not yet on the allow list it replies with your numeric Telegram ID. Paste that into **Allowed Telegram IDs** (comma separated). Leaving it blank lets anyone who finds the bot talk to it, which is not recommended.
4. Pick the **Model** the bot should use (grouped by endpoint, like the composer's picker). "Default model" uses your app default.

**Options:**
- **Ask before commands and edits:** when on, anything the bot delegates that needs Full Access (a command) or edits an open document asks you to approve it in Telegram. Reply `y` or `n`.
- **Show in replies:** toggle whether each reply lists the skills, memories, tool calls and plugins it used.
- **System prompt:** extra instructions appended to the bot's built-in orchestrator prompt. Leave blank for the default.

**Commands:**
- **/chats** lists all your chats.
- **/compact** summarises and shrinks the bot conversation.
- **/help** shows what the bot can do.

Replies stream in live and render Markdown (bold, lists, code, links). The bot conversation never appears in your sidebar. Because the backend is bundled in the app, the bot works whenever JoeBro is running on your Mac.

---

## Settings

Open with the gear in the sidebar footer.

- **Email:** connect a mailbox with IMAP and SMTP (use an app-specific password for Gmail or iCloud), and choose how many messages to load.
- **Calendar:** connect macOS Calendar or a CalDAV account.
- **Server:** the local backend URL (you should not need to change this).
- **Documents:** "Ask before running commands" and "Require approval before applying the agent's document edits."
- **Message Bot:** connect a Telegram bot to run JoeBro from your phone (see Message Bot above).
- **Glass / Background / Wallpaper:** appearance (see Theming).

---

## Theming

Make it yours.

- **◐ button** (sidebar footer): the glass tuner. Adjust how translucent the panels are. This lives on the main window because macOS renders glass differently while the Settings window has focus.
- **Background preset:** the backdrop the glass refracts when no wallpaper is set.
- **Wallpaper:** drop in any image. The whole UI is glass over your picture, or pick a solid theme. Your accent colour follows the system.

---

## Permission modes (how much the agent can touch)

The padlock in the composer sets the agent's file access:

- **Bound folder** (default, grey): the agent only reads and writes inside the folder you bound to the chat.
- **Read-only** (orange): the agent can read but never change files.
- **Full access** (red): the agent can reach files outside the bound folder. Use deliberately.

Two extra safety nets in Settings:
- **Ask before running commands:** the agent pauses for your approval before any terminal command.
- **Require approval before applying document edits:** the agent's changes to an open document wait for you to accept them.

The terminal toggle in the composer must also be on before the agent can run shell commands at all, and that only applies in Agent mode.

---

That's the whole app. Bind a folder, connect a model, and ask it to do something real.