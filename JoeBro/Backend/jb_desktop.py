"""Agent Desktop: computer use through Vercel's agent-desktop (accessibility tree,
JSON, headless by default). Replaces the old osascript "JoeBro macOS Use" when
agent-desktop is installed (`npm i -g agent-desktop`); without it the original
macos_use tool is still offered, so computer use never just disappears.

The backend runs on the user's Mac, so agent-desktop runs locally. It needs
Accessibility permission for JoeBro (System Settings > Privacy & Security)."""
import os
import re
import shutil
import subprocess

DESKTOP_USE_TOOL = {"type": "function", "function": {
    "name": "desktop_use",
    "description": (
        "Computer use on this Mac via agent-desktop (accessibility tree, JSON). Loop: `snapshot` an app -> act "
        "on a ref from THAT snapshot (refs look like @s8f3k2p9:e8) -> `snapshot` again to verify. Actions are "
        "headless by default (no mouse moves, no focus stealing). Actions: list_apps, launch (app), snapshot "
        "(app; interactive-only and compact), find (app + text/role), click (ref), type (ref + text), set_value "
        "(ref + text), press (combo e.g. 'cmd+s', optional app), scroll (ref + direction), screenshot (needs "
        "Screen Recording permission)."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list_apps", "launch", "snapshot", "find", "click", "type",
                                               "set_value", "press", "scroll", "screenshot"]},
        "app": {"type": "string", "description": "application name, e.g. 'Notes' (launch/snapshot/find/press)"},
        "ref": {"type": "string", "description": "qualified ref from the latest snapshot, e.g. @s8f3k2p9:e8"},
        "text": {"type": "string", "description": "text to type / value to set / text to find"},
        "role": {"type": "string", "description": "find: accessibility role, e.g. button, textfield"},
        "combo": {"type": "string", "description": "press: key combo, e.g. return, cmd+c"},
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "full_tree": {"type": "boolean", "description": "snapshot: include non-interactive elements (bigger)"}},
        "required": ["action"]}}}

_SEARCH = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.npm-global/bin"),
           os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.bun/bin"))


def agent_desktop_bin():
    """Path to agent-desktop, or None (then the legacy macos_use tool is used)."""
    found = shutil.which("agent-desktop")
    if found:
        return found
    for d in _SEARCH:
        p = os.path.join(d, "agent-desktop")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


class DesktopUseMixin:
    _DESKTOP_REFS = {}   # snapshot id -> app it came from (for the blocked-app gate)

    def desktop_use_available(self):
        return agent_desktop_bin() is not None

    def desktop_use_argv(self, args, blocked=(), binary=None):
        """Validated agent-desktop argv. Blocked apps can't be launched, snapshotted,
        found or typed to, and refs taken from their snapshots are refused."""
        action = str(args.get("action") or "").strip().lower()
        app = str(args.get("app") or "").strip()
        blocked_l = {b.lower() for b in blocked}
        if app and app.lower() in blocked_l:
            raise ValueError("'%s' is on the blocked-apps list; JoeBro may not touch it" % app)
        ref = str(args.get("ref") or "").strip()
        A = [binary or agent_desktop_bin() or "agent-desktop"]
        if action in ("click", "type", "set_value", "scroll"):
            m = re.fullmatch(r"@([A-Za-z0-9]+):e\d+", ref)
            if not m:
                raise ValueError("ref must be a qualified ref from your latest snapshot, e.g. @s8f3k2p9:e8")
            snap_app = self._DESKTOP_REFS.get(m.group(1))
            if snap_app is None:
                raise ValueError("unknown ref — take a snapshot first and use a ref from it")
            if snap_app.lower() in blocked_l:
                raise ValueError("that ref belongs to a blocked app")
        if action == "list_apps":
            return A + ["list-apps"]
        if action == "launch":
            if not app:
                raise ValueError("launch needs app")
            return A + ["launch", app]
        if action == "snapshot":
            if not app:
                raise ValueError("snapshot needs app (call list_apps to see names)")
            return A + ["snapshot", "--app", app, "--compact"] + ([] if args.get("full_tree") else ["-i"])
        if action == "find":
            if not app:
                raise ValueError("find needs app")
            argv = A + ["find", "--app", app]
            if args.get("role"):
                argv += ["--role", str(args["role"])]
            if args.get("text"):
                argv += ["--text", str(args["text"])]
            return argv
        if action == "click":
            return A + ["click", ref]
        if action in ("type", "set_value"):
            if args.get("text") is None:
                raise ValueError(action + " needs text")
            return A + [("type" if action == "type" else "set-value"), ref, str(args["text"])]
        if action == "scroll":
            return A + ["scroll", ref, "--direction", str(args.get("direction") or "down")]
        if action == "press":
            combo = str(args.get("combo") or args.get("text") or "").strip()
            if not combo:
                raise ValueError("press needs combo")
            return A + ["press", combo] + (["--app", app] if app else [])
        if action == "screenshot":
            return A + ["screenshot"] + (["--app", app] if app else [])
        raise ValueError("unknown desktop_use action: " + action)

    def run_desktop_use(self, form, args):
        args = args if isinstance(args, dict) else {}
        label = "desktop_use " + str(args.get("action") or "")
        binary = agent_desktop_bin()
        if not binary:
            return {"tool": "desktop_use", "command": label, "exit_code": 1,
                    "output": "agent-desktop isn't installed. Tell the user: run `npm i -g agent-desktop`, then allow JoeBro under Accessibility."}
        blocked = self._macos_blocked_apps() if hasattr(self, "_macos_blocked_apps") else []
        try:
            argv = self.desktop_use_argv(args, blocked, binary)
        except ValueError as exc:
            return {"tool": "desktop_use", "command": label, "output": str(exc), "exit_code": 1}
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            out, code = (p.stdout or p.stderr or "").strip(), p.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"tool": "desktop_use", "command": label, "output": "agent-desktop failed: " + str(exc), "exit_code": 1}
        if args.get("action") in ("snapshot", "find") and args.get("app"):
            for sid in set(re.findall(r"@([A-Za-z0-9]+):e\d+", out)):
                self._DESKTOP_REFS[sid] = str(args["app"])
        return {"tool": "desktop_use", "command": label, "output": out[:24000] or "(no output)", "exit_code": code}
