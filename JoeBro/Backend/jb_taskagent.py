"""Tasks that start a coding agent. A task with `coding_agent` + `coding_repo` set runs through the
Night Shift runner (worktree, branch, review card, approve) instead of a model turn: "Run now" starts it
at once, a schedule starts it at its time. The run's status is copied back onto the task row
(`coding_status`, kept in step by NightShiftMixin._night_set), and the review card is the result."""
import jb_nightworker as worker

ACTIVE = ("queued", "running")
FIELDS = ("coding_agent", "coding_repo", "coding_host", "coding_model")


def parse(body, existing):
    """The coding fields to save, validated; None when the request does not touch them."""
    if not any(k in body for k in FIELDS):
        return None
    cur = existing or {}
    get = lambda k, n: str(body.get(k, cur.get(k)) or "").strip()[:n]
    agent, repo = get("coding_agent", 20), get("coding_repo", 500)
    if agent and agent != "any" and agent not in worker.COMMANDS:
        raise ValueError("coding_agent is claude, codex, opencode, agy or any")
    if agent and not repo:
        raise ValueError("a coding task needs a folder (coding_repo)")
    if not agent:
        return ("", "", "", "")
    return (agent, repo, get("coding_host", 60), get("coding_model", 80))


def save(handler, tid, vals):
    if vals is not None:
        handler._night_table()
        handler.store.exec("UPDATE tasks SET coding_agent=?, coding_repo=?, coding_host=?, coding_model=? WHERE id=?", (*vals, tid))


def run(handler, row):
    """Start the coding run for task `row`. Returns a one-line reply (the task's 'result')."""
    from jb_core import now_iso
    handler._night_table()
    prev = row.get("coding_night_id") or ""
    if prev:
        try:
            if handler.night_task(prev)["status"] in ACTIVE:
                return "Still running from last time (Night Shift %s)." % prev
        except LookupError:
            pass
    t = handler.night_add({"repo": row["coding_repo"], "prompt": row.get("prompt") or row.get("title") or "", "title": row.get("title") or "",
                           "agent": row.get("coding_agent") or "any", "model": row.get("coding_model") or "", "host": row.get("coding_host") or ""},
                          run_now=True, task_ref=row["id"])
    handler.store.exec("UPDATE tasks SET coding_night_id=?, coding_status='queued', last_run=?, updated_at=updated_at WHERE id=?",
                       (t["id"], now_iso(), row["id"]))
    handler.night_tick()
    t = handler.night_task(t["id"])
    where = (" on " + t["host"]) if t["host"] else ""
    return "Started %s%s. Review it in Night Shift when it finishes." % (worker.NAMES.get(t["agent"], "a coding agent"), where)
