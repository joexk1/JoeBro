"""Self-checks for the file-safety hardening: per-path locking serializes
concurrent read-modify-writes (no lost edits) and backups prune to 5.
Run: python3 test_safety.py"""
import importlib.util, tempfile, threading, types
from pathlib import Path

spec = importlib.util.spec_from_file_location("jb", str(Path(__file__).with_name("joebro_backend.py")))
jb = importlib.util.module_from_spec(spec); spec.loader.exec_module(jb)


def _handler(work):
    store = jb.Store(Path(tempfile.mkdtemp()))
    store.exec("insert into sessions(id,name,model,endpoint_url,endpoint_id,workdir,created_at)"
               " values(?,?,?,?,?,?,?)", ("s1", "t", "m", "", "", str(work), jb.now_iso()))
    h = jb.Handler.__new__(jb.Handler); h.server = types.SimpleNamespace(store=store)
    return h


def test_concurrent_edits_no_lost_updates():
    # 12 threads each append their line before the <END> sentinel via
    # edit_document find/replace. With per-path locking every line must land.
    work = Path(tempfile.mkdtemp()).resolve()  # resolve /var->/private/var like prod
    (work / "f.md").write_text("<END>\n")
    h = _handler(work)
    N = 12

    def worker(i):
        body = f"path: f.md\nfind: <END>\nreplace: line{i}\n<END>"
        try:
            h.execute_file_tool(work, "edit_document", body, allow_outside=False)
        except Exception as e:
            print("edit failed", i, e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()
    text = (work / "f.md").read_text()
    landed = sum(1 for i in range(N) if f"line{i}" in text)
    assert landed == N, f"lost updates: only {landed}/{N} lines landed:\n{text}"


def test_backup_prunes_to_five(monkeypatched_dir=None):
    bdir_root = Path(tempfile.mkdtemp())
    jb.app_support_dir = lambda: bdir_root          # redirect backups to temp
    f = Path(tempfile.mkdtemp()) / "doc.md"
    for i in range(8):
        f.write_text(f"version {i}")
        jb.backup_existing(f)
    backups = list((bdir_root / "backups").glob("doc.md.*.bak"))
    assert len(backups) == 5, f"expected 5 backups kept, got {len(backups)}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
    print("all safety checks passed")
