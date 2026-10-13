#!/usr/bin/env python3
"""Daily janitor for JoeBro's scratch files. Allowlisted folders only, never follows symlinks.

Runs as a thread inside the backend (janitor_loop), once a day, and only when the
user turned deletion on (pref `janitor_enabled`; without it nothing is deleted).
Removes: artifacts unused for 7 days, transfer scratch and /tmp/joebro-* files
unused for 7 days, vision captions untouched for 30 days. With pref
`janitor_vacuum` on it also VACUUMs the database, at most monthly.
NEVER touched: the database's content, muse, uploads, documents, anything outside
the allowlist.
  python3 jb_janitor.py [--dry-run]   (acts on the default data folder)
"""
import argparse, fnmatch, json, os, re, shutil, sqlite3, stat, sys, time
from pathlib import Path

DAY = 86400


def default_config(data=None):
    from jb_core import data_dir
    data = Path(data) if data else data_dir()
    return {
        "data": data, "tmp": Path("/tmp"),
        "artifacts": data / "artifacts",
        "scratch": [data / "transfer-tmp"],
        "vision": data / "vision_cache",
        "tmp_patterns": ["joebro-*", "joebro_screenshot_*"],
        "db": data / "joebro.sqlite3",
        "state": data / "janitor_state.json",
    }


def real_dir(p):
    """Allowlisted root must exist, be a real dir (not a symlink)."""
    p = Path(p)
    return p.is_dir() and not p.is_symlink()


def newest(path):
    """Latest mtime/atime of a path (files only, symlinks not followed; dirs: newest inside)."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return max(st.st_mtime, st.st_atime)
    best = st.st_mtime  # dir atime is bumped by our own listing, so only mtime counts for dirs
    for dp, dns, fns in os.walk(path, followlinks=False):
        for n in dns + fns:
            try:
                s = os.lstat(os.path.join(dp, n))
                best = max(best, s.st_mtime, s.st_atime if n in fns else 0)
            except OSError:
                pass
    return best


def tree_size(path):
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    for dp, dns, fns in os.walk(path, followlinks=False):
        for n in fns:
            try:
                total += os.lstat(os.path.join(dp, n)).st_size
            except OSError:
                pass
    return total


class Janitor:
    def __init__(self, cfg, dry_run=False, now=None):
        self.cfg, self.dry, self.now = cfg, dry_run, now or time.time()
        self.freed = 0
        self.removed = []

    def _rm(self, path, why):
        """Remove a file, or a dir tree containing no symlinks. Symlinks are never followed or removed."""
        path = Path(path)
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                return
            size = tree_size(path)
            if stat.S_ISDIR(st.st_mode):
                if any(os.path.islink(os.path.join(dp, n)) for dp, dns, fns in os.walk(path, followlinks=False) for n in dns + fns):
                    return
                if not self.dry:
                    shutil.rmtree(path)
            elif stat.S_ISREG(st.st_mode):
                if not self.dry:
                    os.unlink(path)
            else:
                return
        except OSError as e:
            self.removed.append(f"SKIP {path} ({e})")
            return
        self.freed += size
        self.removed.append(f"{'WOULD DELETE' if self.dry else 'DELETED'} {path} {size}B {why}")

    def _stale_entries(self, root, days, pattern=None):
        if not real_dir(root):
            return
        cutoff = self.now - days * DAY
        for name in sorted(os.listdir(root)):
            if pattern and not any(fnmatch.fnmatch(name, p) for p in pattern):
                continue
            p = Path(root) / name
            try:
                if os.path.islink(p):
                    continue
                if newest(p) < cutoff:
                    yield p
            except OSError:
                continue

    def artifacts(self):
        root = Path(self.cfg["artifacts"])
        if not real_dir(root):
            return
        cutoff = self.now - 7 * DAY
        ids = {}
        for name in os.listdir(root):
            m = re.fullmatch(r"([a-f0-9]{32})\.(blob|json)", name)
            if m:
                ids.setdefault(m.group(1), []).append(root / name)
            elif name.startswith(".artifact-") or name.startswith(".joebro-artifact-"):
                p = root / name
                if not p.is_symlink() and newest(p) < cutoff:
                    self._rm(p, "stale artifact temp")
        for aid, files in ids.items():
            files = [f for f in files if not f.is_symlink()]
            try:
                rec = json.loads((root / f"{aid}.json").read_text())
            except (OSError, ValueError):
                rec = {}
            if rec.get("moved_to"):
                continue  # delivered into a user folder: metadata only, keep
            # mtime only: reading the json bumps atime, and resolve_artifact() bumps blob mtime on every use
            if files and max(os.lstat(f).st_mtime for f in files) < cutoff:
                for f in files:
                    self._rm(f, "artifact unused 7d")

    def scratch(self):
        cutoff = self.now - 7 * DAY
        for d in self.cfg["scratch"]:
            if not real_dir(d):
                continue
            for dp, dns, fns in os.walk(d, followlinks=False):  # per-file; symlinked dirs are listed in dns but never entered
                for n in fns:
                    p = os.path.join(dp, n)
                    try:
                        if not os.path.islink(p) and newest(p) < cutoff:
                            self._rm(p, "scratch unused 7d")
                    except OSError:
                        pass
            if not self.dry:
                for dp, dns, fns in os.walk(d, topdown=False, followlinks=False):  # prune emptied subdirs
                    if dp != str(d) and not os.path.islink(dp):
                        try:
                            os.rmdir(dp)
                        except OSError:
                            pass
        for p in self._stale_entries(self.cfg["tmp"], 7, self.cfg["tmp_patterns"]):
            if os.lstat(p).st_uid == os.getuid():
                self._rm(p, "backend /tmp unused 7d")

    def vision(self):
        for p in self._stale_entries(self.cfg["vision"], 30):
            self._rm(p, "vision cache untouched 30d")

    def vacuum(self, state):
        if self.now - state.get("last_vacuum", 0) < 30 * DAY:
            return
        db = Path(self.cfg["db"])
        if not db.is_file() or db.is_symlink():
            return
        if self.dry:
            self.removed.append(f"WOULD VACUUM {db}")
            return
        before = db.stat().st_size
        try:
            con = sqlite3.connect(str(db), timeout=60)
            con.execute("VACUUM")
            con.close()
        except sqlite3.Error as e:
            self.removed.append(f"VACUUM failed: {e}")
            return
        state["last_vacuum"] = self.now
        self.freed += max(0, before - db.stat().st_size)
        self.removed.append(f"VACUUMED {db} {before}->{db.stat().st_size}B")

    def run(self, state, vacuum=False):
        self.artifacts()
        self.scratch()
        self.vision()
        if vacuum:
            self.vacuum(state)


def run_once(cfg=None, dry_run=False, vacuum=False):
    """One pass. Returns the Janitor (its .removed lines say what happened)."""
    cfg = cfg or default_config()
    statep = Path(cfg["state"])
    try:
        state = json.loads(statep.read_text())
    except (OSError, ValueError):
        state = {}
    j = Janitor(cfg, dry_run)
    j.run(state, vacuum=vacuum)
    if not dry_run:
        try:
            state["last_run"] = time.time()
            statep.write_text(json.dumps(state))
        except OSError:
            pass
    return j


def janitor_loop(server):
    """Daemon: once a day, if (and only if) the user switched deletion on."""
    import types as _types
    from joebro_backend import Handler
    time.sleep(120)
    while True:
        try:
            h = Handler.__new__(Handler)
            h.server = _types.SimpleNamespace(store=server.store)
            if str(h.pref("janitor_enabled")).lower() in ("1", "true", "yes", "on"):
                run_once(default_config(server.store.root),
                         vacuum=str(h.pref("janitor_vacuum")).lower() in ("1", "true", "yes", "on"))
        except Exception:
            pass
        time.sleep(DAY)


def main(argv=None, cfg=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    j = run_once(cfg, a.dry_run)
    for r in j.removed:
        print(r)
    print(f"janitor{' DRY-RUN' if a.dry_run else ''}: freed {j.freed / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
