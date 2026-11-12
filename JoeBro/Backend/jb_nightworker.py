"""Night Shift worker: the git and process side of one unattended coding task.

Standalone (stdlib only, imports nothing from the backend) so the same file can run
on any linked computer that has the repo. It never pushes: every agent runs with
push URLs rewritten to an invalid scheme, and nothing here calls `git push`.
The only destructive commands are in approve() and reject(), which run only on an
explicit user decision."""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

BRANCH_PREFIX = "joebro/night/"
SHOT = os.path.join(".joebro", "night", "screenshot.png")
NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
# Headless commands of the agents' own CLIs (the user's subscriptions; no JoeBro model calls).
COMMANDS = {
    "claude": ["claude", "-p", "{prompt}", "--output-format", "json", "--permission-mode", "acceptEdits"],
    "codex": ["codex", "exec", "-s", "workspace-write", "{prompt}"],   # --full-auto was removed in codex 0.167
    "opencode": ["opencode", "run", "{prompt}"],
}
MODEL_FLAG = {"claude": "--model", "codex": "-m", "opencode": "-m"}
BUDGET_FLAG = {"claude": "--max-budget-usd"}
RESUME = {"claude": ["--resume", "{id}"], "codex": ["resume", "{id}"], "opencode": ["--session", "{id}"]}
INTERACTIVE = {"claude": ["claude"], "codex": ["codex"], "opencode": ["opencode"]}


def no_push_env():
    """Git config (through the environment) that makes every push from the agent fail."""
    prefixes = ["https://", "http://", "ssh://", "git://", "git@", "file://", "/"]
    env = {"GIT_CONFIG_COUNT": str(len(prefixes))}
    for i, p in enumerate(prefixes):
        env["GIT_CONFIG_KEY_%d" % i] = "url.night-shift-never-pushes://blocked/.pushInsteadOf"
        env["GIT_CONFIG_VALUE_%d" % i] = p
    return env


def git(repo, *args, timeout=120, check=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise ValueError("git %s failed: %s" % (args[0], (p.stderr or p.stdout).strip()[:300]))
    return p.returncode, p.stdout.strip()


def slug(text, n=32):
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:n].strip("-") or "task"


def installed(agent, commands=None):
    argv = (commands or {}).get(agent) or COMMANDS.get(agent)
    return bool(argv and shutil.which(argv[0]))


def prepare(repo, task_id, title, root, branch="", worktree=""):
    """Create (or reuse, for a follow-up) the task's own worktree and branch."""
    code, top = git(repo, "rev-parse", "--show-toplevel")
    if code != 0:
        raise ValueError("not a git repository: " + repo)
    if worktree and os.path.isdir(worktree) and branch:
        return {"repo": top, "branch": branch, "worktree": worktree, "reused": True}
    _, base_commit = git(top, "rev-parse", "HEAD", check=True)
    _, base_branch = git(top, "symbolic-ref", "--short", "-q", "HEAD")
    branch = "%s%s-%s" % (BRANCH_PREFIX, task_id, slug(title))
    worktree = os.path.join(root, task_id)
    os.makedirs(root, exist_ok=True)
    git(top, "worktree", "add", "-b", branch, worktree, base_commit, check=True)
    return {"repo": top, "branch": branch, "worktree": worktree, "base_branch": base_branch, "base_commit": base_commit, "reused": False}


def agent_argv(agent, prompt, model="", budget=0, commands=None, resume_id=""):
    argv = list((commands or {}).get(agent) or COMMANDS.get(agent) or [])
    if not argv:
        raise ValueError("unknown agent: %s" % agent)
    custom = agent in (commands or {})
    argv = [a.replace("{prompt}", prompt).replace("{model}", model or "").replace("{budget}", str(budget or "")) for a in argv]
    if not custom:
        if model and agent in MODEL_FLAG:
            argv += [MODEL_FLAG[agent], model]
        if budget and agent in BUDGET_FLAG:
            argv += [BUDGET_FLAG[agent], str(budget)]
        if resume_id and agent == "claude":
            argv += ["--resume", resume_id]
        elif resume_id and agent == "opencode":
            argv += ["--session", resume_id]
    return argv


def session_argv(agent, prompt="", model="", resume_id="", commands=None):
    """An interactive session (for tmux/kitty): native resume when an id is known."""
    argv = list((commands or {}).get(agent + "_session") or INTERACTIVE.get(agent) or [])
    if not argv:
        raise ValueError("unknown agent: %s" % agent)
    if resume_id:
        argv += [a.replace("{id}", resume_id) for a in RESUME[agent]] if agent in RESUME else []
    if model and agent in MODEL_FLAG:
        argv += [MODEL_FLAG[agent], model]
    if prompt:
        argv += (["--prompt", prompt] if agent == "opencode" else [prompt])
    return argv


def run_agent(argv, cwd, max_minutes=60, env=None, stop=None, log_path=""):
    """Run the agent to completion, or kill its whole process group at the time cap."""
    started = time.time()
    full_env = dict(os.environ, **(env or {}))
    out_path = log_path or os.path.join(cwd, ".git-night-output.tmp")
    with open(out_path, "w+b") as out:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=full_env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as exc:
            return {"exit": 127, "output": "could not start %s: %s" % (argv[0], exc), "timed_out": False, "stopped": False, "duration_s": 0}
        deadline = started + max(0.001, float(max_minutes)) * 60
        timed_out = stopped = False
        while proc.poll() is None:
            if time.time() > deadline:
                timed_out = True
            elif stop is not None and stop.is_set():
                stopped = True
            if timed_out or stopped:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=5)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                break
            time.sleep(0.2)
        out.seek(0)
        text = out.read().decode("utf-8", "replace")
    if not log_path:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return {"exit": proc.returncode if proc.returncode is not None else -9, "output": text[-200000:], "timed_out": timed_out,
            "stopped": stopped, "duration_s": int(time.time() - started)}


def parse_output(output):
    """Summary, learned facts and cost from the agent's final output. Text rules only."""
    text, cost, session = output or "", None, ""
    try:
        data = json.loads(text.strip().splitlines()[-1]) if text.strip().startswith("{") or "\n{" in text else None
        if isinstance(data, dict) and "result" in data:
            text, cost, session = str(data.get("result") or ""), data.get("total_cost_usd"), str(data.get("session_id") or "")
    except (ValueError, IndexError):
        pass
    learned = [re.sub(r"\s+", " ", m).strip()[:400] for m in re.findall(r"^\s*LEARNED:\s*(.+)$", text, flags=re.M)][:3]
    body = re.sub(r"^\s*LEARNED:.*$", "", text, flags=re.M).strip()
    m = list(re.finditer(r"^\s*SUMMARY:\s*", body, flags=re.M))
    summary = body[m[-1].end():] if m else body[-1200:]
    return {"summary": summary.strip()[:1500], "learned": [x for x in learned if len(x) >= 12], "cost_usd": cost, "session_id": session}


def collect(worktree, base_commit, title, test_command="", test_timeout=900, shot_dir=""):
    """Commit what the agent left, then diff stats, optional tests, optional screenshot."""
    res = {"screenshot": False, "tests": None}
    shot = os.path.join(worktree, SHOT)
    if os.path.isfile(shot) and shot_dir and os.path.getsize(shot) < 8_000_000:
        os.makedirs(shot_dir, exist_ok=True)
        shutil.copyfile(shot, os.path.join(shot_dir, "screenshot.png"))
        res["screenshot"] = True
    shutil.rmtree(os.path.join(worktree, ".joebro", "night"), ignore_errors=True)
    _, dirty = git(worktree, "status", "--porcelain")
    if dirty:
        git(worktree, "add", "-A")
        code, _ = git(worktree, "commit", "-m", "Night Shift: " + title[:60])
        if code != 0:       # no identity configured on this machine
            git(worktree, "-c", "user.name=JoeBro Night Shift", "-c", "user.email=night-shift@localhost", "commit", "-m", "Night Shift: " + title[:60])
    _, head = git(worktree, "rev-parse", "HEAD")
    _, count = git(worktree, "rev-list", "--count", "%s..HEAD" % base_commit)
    files = []
    _, numstat = git(worktree, "diff", "--numstat", base_commit, "HEAD")
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            files.append({"path": parts[2], "added": int(parts[0]) if parts[0].isdigit() else 0,
                          "removed": int(parts[1]) if parts[1].isdigit() else 0})
    res.update(head_commit=head, commits=int(count or 0),
               diff={"files": len(files), "insertions": sum(f["added"] for f in files), "deletions": sum(f["removed"] for f in files),
                     "file_list": files[:60]})
    if test_command and res["commits"]:
        t0 = time.time()
        try:
            p = subprocess.run(test_command, shell=True, cwd=worktree, capture_output=True, text=True, timeout=test_timeout,
                               env=dict(os.environ, **no_push_env()))
            code, out = p.returncode, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            code, out = 124, "test command timed out"
        res["tests"] = {"command": test_command, "passed": code == 0, "exit_code": code, "duration_s": int(time.time() - t0),
                        "output_tail": out[-1500:]}
        git(worktree, "checkout", "--", ".")          # tests must not leave the committed work dirty
    return res


def diff_text(repo, base_commit, head, cap=200_000):
    _, out = git(repo, "diff", base_commit, head)
    return out[:cap], len(out) > cap


def approve(repo, branch, worktree, base_branch, mode="keep", title=""):
    """The user approved: optionally merge locally, remove the worktree, keep the branch. Never pushes."""
    merged, detail = False, "Branch %s kept." % branch
    if mode == "merge":
        _, current = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
        _, dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
        if not base_branch:
            detail = "Not merged (the repo was on a detached HEAD). " + detail
        elif current == base_branch and dirty:
            detail = "Not merged: %s has uncommitted changes. %s" % (base_branch, detail)
        elif current == base_branch:
            code, out = git(repo, "merge", "--no-ff", "--no-edit", "-m", "Merge Night Shift: " + (title or branch), branch)
            if code == 0:
                merged, detail = True, "Merged into %s (local only, not pushed)." % base_branch
            else:
                git(repo, "merge", "--abort")
                detail = "Not merged (conflicts with %s). %s" % (base_branch, detail)
        else:
            code, _ = git(repo, "fetch", ".", "%s:%s" % (branch, base_branch))     # fast-forward only
            if code == 0:
                merged, detail = True, "Fast-forwarded %s (local only, not pushed)." % base_branch
            else:
                detail = "Not merged (%s has moved on; merge it by hand). %s" % (base_branch, detail)
    if worktree and os.path.isdir(worktree):
        # Everything the agent did is committed; untracked leftovers are build/test by-products.
        _, changed = git(worktree, "status", "--porcelain", "--untracked-files=no")
        code, _ = git(repo, "worktree", "remove", worktree) if changed else git(repo, "worktree", "remove", "--force", worktree)
        if code != 0:
            detail += " The worktree has uncommitted changes and was left at %s." % worktree
    git(repo, "worktree", "prune")
    return {"merged": merged, "detail": detail}


def reject(repo, branch, worktree):
    """The user rejected: discard the worktree and the branch."""
    if worktree and os.path.isdir(worktree):
        git(repo, "worktree", "remove", "--force", worktree)
    git(repo, "worktree", "prune")
    if branch.startswith(BRANCH_PREFIX):
        git(repo, "branch", "-D", branch)
    return {"detail": "Worktree and branch discarded."}


def tmux_launch(name, cwd, argv, env=None):
    """Start an interactive agent in a detached tmux session (works headless on any node)."""
    tmux = shutil.which("tmux")
    if not tmux:
        raise ValueError("tmux is not installed on this computer")
    cmd = [tmux, "new-session", "-d", "-s", name, "-c", cwd]
    for k, v in (env or {}).items():
        cmd += ["-e", "%s=%s" % (k, v)]
    p = subprocess.run(cmd + argv, capture_output=True, text=True, timeout=20)
    if p.returncode != 0:
        raise ValueError("tmux could not start the session: " + (p.stderr or p.stdout).strip()[:200])
    return {"tmux": name, "attach": "tmux attach -t " + name}


def run_agent_tmux(name, argv, cwd, max_minutes=60, env=None, stop=None, log_path=""):
    """Like run_agent, but the agent runs inside a detached tmux session (`tmux attach -t name` to watch).
    Output is tee'd to log_path; the exit code comes back through a file. Falls back to run_agent without tmux."""
    import shlex
    tmux = shutil.which("tmux")
    if not tmux:
        return run_agent(argv, cwd, max_minutes, env, stop, log_path)
    started = time.time()
    tmp = tempfile.mkdtemp(prefix="jb-node-")
    log = log_path or os.path.join(tmp, "out.log")
    exitf, pidf, script = os.path.join(tmp, "exit"), os.path.join(tmp, "pid"), os.path.join(tmp, "run.sh")
    with open(script, "w") as f:
        f.write("#!/bin/bash\necho $$ > %s\n%s </dev/null 2>&1 | tee %s\necho ${PIPESTATUS[0]} > %s\n"
                % (shlex.quote(pidf), " ".join(shlex.quote(a) for a in argv), shlex.quote(log), shlex.quote(exitf)))
    cmd = [tmux, "new-session", "-d", "-s", name, "-c", cwd]
    for k, v in (env or {}).items():
        cmd += ["-e", "%s=%s" % (k, v)]
    p = subprocess.run(cmd + ["bash", script], capture_output=True, text=True, timeout=20)
    if p.returncode != 0:
        return {"exit": 127, "output": "tmux could not start: " + (p.stderr or p.stdout).strip()[:200], "timed_out": False,
                "stopped": False, "duration_s": 0}
    timed_out = stopped = False
    deadline = started + max(0.001, float(max_minutes)) * 60
    while not os.path.exists(exitf):
        if time.time() > deadline:
            timed_out = True
        elif stop is not None and stop.is_set():
            stopped = True
        if timed_out or stopped:
            try:
                os.killpg(int(open(pidf).read().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
            break
        if subprocess.run([tmux, "has-session", "-t", name], capture_output=True).returncode != 0 and not os.path.exists(exitf):
            time.sleep(0.5)             # the session ended without an exit file (killed by hand)
            break
        time.sleep(0.3)
    subprocess.run([tmux, "kill-session", "-t", name], capture_output=True)
    try:
        code = int(open(exitf).read().strip())
    except (OSError, ValueError):
        code = -9
    try:
        text = open(log, "rb").read().decode("utf-8", "replace")
    except OSError:
        text = ""
    shutil.rmtree(tmp, ignore_errors=True)
    return {"exit": code, "output": text[-200000:], "timed_out": timed_out, "stopped": stopped, "duration_s": int(time.time() - started)}
