"""agent_reach: read a URL's content for the model (fetch-only, never posts).

Web pages become readable text, Reddit threads include comments (public .json
view, with a pullpush fallback), YouTube / Bilibili links go to the optional
`agent-reach` CLI for a transcript (AGENT_REACH_BIN or ~/.local/bin/agent-reach).
Gated by the agent_reach plugin switch (see jb_plugins)."""
import json
import os
import re
import subprocess
import urllib.request

AGENT_REACH_BIN = os.environ.get("AGENT_REACH_BIN") or os.path.expanduser("~/.local/bin/agent-reach")
AGENT_REACH_TOOL = {"type": "function", "function": {
    "name": "agent_reach",
    "description": ("Read the content of a URL from across the internet: web pages and articles "
                    "(readable text; Reddit threads include the comments), and YouTube / Bilibili "
                    "videos (transcript). Fetch-only, never posts. Pass the full URL as `url`. Use "
                    "this to read/summarise a specific link the user shares or you find."),
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "description": "the full URL to read (https://...)"}},
        "required": ["url"]}}}


def _html_to_text(page):
    """Crude readability pass: strip scripts/nav/boilerplate, keep the title,
    headings and paragraph text as light Markdown. ponytail: regex stripper —
    the Jina Reader fallback covers JS-rendered pages this can't."""
    import html as _entities
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    if m:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    page = re.sub(r"(?is)<(script|style|noscript|svg|iframe|template)\b.*?</\1>", " ", page)
    page = re.sub(r"(?is)<(header|footer|nav|aside|form)\b.*?</\1>", " ", page)
    m = re.search(r"(?is)<(article|main)\b[^>]*>(.*)</\1>", page)
    if m:
        page = m.group(2)
    page = re.sub(r"(?i)<br\s*/?>", "\n", page)
    page = re.sub(r"(?is)<h([1-6])[^>]*>", lambda h: "\n" + "#" * int(h.group(1)) + " ", page)
    page = re.sub(r"(?i)<li[^>]*>", "\n- ", page)
    page = re.sub(r"(?is)</(p|div|li|h[1-6]|tr|blockquote)>", "\n", page)
    text = _entities.unescape(re.sub(r"<[^>]+>", " ", page))
    text = "\n".join(re.sub(r"[ \t]+", " ", l).strip() for l in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return (("# " + title + "\n\n") if title else "") + text


class ReachMixin:
    def run_agent_reach(self, f, params):
        """Foreground plugin: read a URL's content. Video -> `agent-reach
        transcribe`. Reddit -> its public .json view. Everything else -> direct
        fetch with browser headers (a home or office IP often gets through
        where Jina Reader's datacenter IPs are blocked), stripped to readable
        text, with Jina Reader kept as the fallback for JS-rendered pages."""
        url = ((params or {}).get("url") or (params or {}).get("query") or (params or {}).get("command") or "").strip()
        if not url:
            return {"tool": "agent_reach", "command": "agent_reach", "output": "Pass a `url` to read.", "exit_code": 1}
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return {"tool": "agent_reach", "command": "agent_reach " + url,
                    "output": "Pass a full URL (https://...).", "exit_code": 1}
        if any(h in low for h in ("youtube.com", "youtu.be", "bilibili.com")):
            label = "transcribe " + url
            try:
                proc = subprocess.run([AGENT_REACH_BIN, "transcribe", url],
                                      capture_output=True, text=True, timeout=120)
                out = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "(no output)"
                return {"tool": "agent_reach", "command": label, "output": out[:20000], "exit_code": proc.returncode}
            except FileNotFoundError:
                return {"tool": "agent_reach", "command": label,
                        "output": "Agent Reach is not installed on the backend host.", "exit_code": 1}
            except Exception as exc:
                return {"tool": "agent_reach", "command": label, "output": str(exc), "exit_code": 1}
        label = "read " + url
        try:
            return {"tool": "agent_reach", "command": label,
                    "output": self._reach_read(url)[:20000], "exit_code": 0}
        except Exception as exc:
            return {"tool": "agent_reach", "command": label, "output": str(exc), "exit_code": 1}

    _REACH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
    _REACH_WALLS = ("just a moment", "enable javascript and cookies", "access denied",
                    "you've been blocked", "verify you are human")

    def _reach_read(self, url):
        """Page text for a URL: reddit's .json view when applicable, else a
        direct browser-headed fetch stripped to text, else Jina Reader."""
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.endswith("reddit.com") or host == "redd.it":
            text = self._reach_reddit(url)
            if text:
                return text
        try:
            raw, ctype = self._reach_fetch(url)
            if "html" in ctype:
                text = _html_to_text(raw)
                if len(text) > 200 and not any(s in text.lower() for s in self._REACH_WALLS):
                    return text
            elif raw.strip():
                return raw.strip()
        except Exception:
            pass
        proc = subprocess.run(["curl", "-sL", "--max-time", "45", "https://r.jina.ai/" + url],
                              capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            return out
        raise RuntimeError("Couldn't read the page directly or via Jina Reader.")

    def _reach_fetch(self, url, accept="text/html,application/xhtml+xml,*/*;q=0.8"):
        req = urllib.request.Request(url, headers={
            "User-Agent": self._REACH_UA, "Accept": accept,
            "Accept-Language": "en-GB,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (resp.read(2_000_000).decode("utf-8", errors="replace"),
                    resp.headers.get("Content-Type", ""))

    def _reach_reddit(self, url):
        """Reddit content as text: the official .json view when reachable, else
        the pullpush.io archive — reddit TLS-fingerprints non-browser clients
        and 403s them; pullpush mirrors threads with a small lag. Returns '' to
        let the caller fall through to the generic paths."""
        u = urllib.parse.urlparse(url)
        path = u.path.rstrip("/")
        text = self._reddit_json_view(path) if path else ""
        if text:
            return text
        m = re.search(r"/comments/([a-z0-9]+)", path)
        if not m and u.netloc.lower() == "redd.it":
            m = re.match(r"/([a-z0-9]+)$", path)
        if m:
            return self._reddit_pullpush_thread(m.group(1))
        m = re.search(r"/r/([A-Za-z0-9_]+)", path)
        if m:
            return self._reddit_pullpush_sub(m.group(1))
        return ""

    def _reddit_json_view(self, path):
        try:
            raw, _ = self._reach_fetch(
                f"https://www.reddit.com{path}.json?limit=40&raw_json=1",
                accept="application/json")
            data = json.loads(raw)
        except Exception:
            return ""
        lines = []
        if isinstance(data, list) and data:   # [post, comments] = a thread
            p = data[0]["data"]["children"][0]["data"]
            lines += [f"# {p.get('title', '')}",
                      f"r/{p.get('subreddit', '')} · u/{p.get('author', '')} · "
                      f"{p.get('score', 0)} points · {p.get('num_comments', 0)} comments"]
            if p.get("selftext"):
                lines.append(p["selftext"])
            elif p.get("url_overridden_by_dest"):
                lines.append("Link: " + p["url_overridden_by_dest"])

            def walk(children, depth):
                for c in children:
                    if c.get("kind") != "t1":
                        continue
                    d = c["data"]
                    body = (d.get("body") or "").strip()
                    if body:
                        lines.append("  " * depth + f"- u/{d.get('author', '?')} "
                                     f"({d.get('score', 0)}): " + body.replace("\n", " "))
                    replies = d.get("replies")
                    if isinstance(replies, dict) and depth < 2:
                        walk(replies["data"]["children"], depth + 1)
            if len(data) > 1:
                lines.append("## Comments")
                walk(data[1]["data"]["children"], 0)
        elif isinstance(data, dict) and (data.get("data") or {}).get("children"):
            for c in data["data"]["children"][:25]:   # subreddit/user listing
                if c.get("kind") == "t3":
                    d = c["data"]
                    lines.append(f"- {d.get('title', '')} ({d.get('score', 0)} pts, "
                                 f"{d.get('num_comments', 0)} comments) — "
                                 f"https://www.reddit.com{d.get('permalink', '')}")
        return "\n\n".join(l for l in lines if l).strip()

    def _reddit_pullpush_thread(self, tid):
        try:
            raw, _ = self._reach_fetch(
                f"https://api.pullpush.io/reddit/search/submission/?ids={tid}",
                accept="application/json")
            posts = json.loads(raw).get("data") or []
            if not posts:
                return ""
            p = posts[0]
            lines = [f"# {p.get('title', '')}",
                     f"r/{p.get('subreddit', '')} · u/{p.get('author', '')} · "
                     f"{p.get('score', 0)} points · {p.get('num_comments', 0)} comments"]
            if p.get("selftext"):
                lines.append(p["selftext"])
            elif p.get("url"):
                lines.append("Link: " + p["url"])
            raw, _ = self._reach_fetch(
                f"https://api.pullpush.io/reddit/search/comment/?link_id=t3_{tid}"
                "&size=30&sort_type=score&sort=desc", accept="application/json")
            comments = json.loads(raw).get("data") or []
            if comments:
                lines.append("## Comments")
                for c in comments:
                    body = (c.get("body") or "").strip()
                    if body:
                        lines.append(f"- u/{c.get('author', '?')} ({c.get('score', 0)}): "
                                     + body.replace("\n", " "))
            return "\n\n".join(l for l in lines if l).strip()
        except Exception:
            return ""

    def _reddit_pullpush_sub(self, sub):
        try:
            raw, _ = self._reach_fetch(
                f"https://api.pullpush.io/reddit/search/submission/?subreddit={sub}"
                "&size=25&sort=desc&sort_type=created_utc", accept="application/json")
            posts = json.loads(raw).get("data") or []
            lines = [f"- {p.get('title', '')} ({p.get('score', 0)} pts, "
                     f"{p.get('num_comments', 0)} comments) — "
                     f"https://www.reddit.com{p.get('permalink', '')}"
                     for p in posts if p.get("title")]
            return (f"# r/{sub} — recent posts\n\n" + "\n".join(lines)).strip() if lines else ""
        except Exception:
            return ""
