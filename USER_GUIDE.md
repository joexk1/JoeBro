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

- **Sidebar (left):** your chats at the top, a **Bind to a folder** button and file tree in the middle, and the list of workspace tabs (Chats, Email, Calendar, Brain, Notes, Tasks, Skills, AI Check, Deep Research) below. The footer holds the **gear** (Settings) and the **◐** button (glass / appearance tuner).
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
- **Delete:** remove it.

**Right-click a message in the conversation:**
- On your own message: **Copy**, **Resend** (run it again), **Unsend** (remove it).
- On a reply: **Copy Full Response**. A copy button also appears when you hover a reply.

Agent runs show their tool steps inline (reading a file, creating an event, running a command) so you can see exactly what happened, not just the final answer.

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

- Create a task with a prompt, a schedule (**daily**, **weekly** or **monthly**) and a time.
- Tasks can be **active** or **paused**.

**Right-click a task:**
- **Run now:** fire it immediately.
- **Edit…**
- **Pause / Resume**
- **Delete**

The agent can create and manage tasks for you with its task tool, so "remind me each Monday morning to plan the week" actually becomes a real scheduled task.

---

## Skills

JoeBro gets sharper the more you use it. When you do something often, it can turn that into a reusable **skill** with a confidence score, and pull it back up when it is relevant.

- Browse the skills it has learned, each with a short description and a confidence percentage.
- Edit, enable or remove them.

**Right-click a skill:**
- **Edit…**
- **Activate / Disable**
- **Delete**

A built-in weekly **skill audit** prunes low-confidence skills automatically, and a **memory audit** clears out stale memories. Both are normal tasks you can see and adjust in the Tasks tab.

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

## Settings

Open with the gear in the sidebar footer.

- **Email:** connect a mailbox with IMAP and SMTP (use an app-specific password for Gmail or iCloud), and choose how many messages to load.
- **Calendar:** connect macOS Calendar or a CalDAV account.
- **Server:** the local backend URL (you should not need to change this).
- **Documents:** "Ask before running commands" and "Require approval before applying the agent's document edits."
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