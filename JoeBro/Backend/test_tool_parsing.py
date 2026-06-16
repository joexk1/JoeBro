"""Self-check for the AI tool-call parser: every XML/fenced shape the models
emit must execute the right tool AND leave clean display text. Run: python3 test_tool_parsing.py"""
import importlib.util, tempfile, types
from pathlib import Path

spec = importlib.util.spec_from_file_location("jb", str(Path(__file__).with_name("joebro_backend.py")))
jb = importlib.util.module_from_spec(spec); spec.loader.exec_module(jb)


def fresh():
    data = Path(tempfile.mkdtemp()); work = Path(tempfile.mkdtemp())
    (work / "overview.md").write_text("# Overview\n\nExisting.\n")
    store = jb.Store(data)
    store.exec("insert into sessions(id,name,model,endpoint_url,endpoint_id,workdir,created_at)"
               " values(?,?,?,?,?,?,?)", ("s1", "t", "m", "", "", str(work), jb.now_iso()))
    h = jb.Handler.__new__(jb.Handler); h.server = types.SimpleNamespace(store=store)
    return h, work


def run(h, reply):
    return h.execute_ai_file_tools("s1", reply, {"permission_mode": "sandbox", "session": "s1"})


def test_direct_xml_create():
    h, work = fresh()
    cleaned, events = run(h, "ok\n<create_document>\n<path>A.md</path>\n<content>hi A</content>\n</create_document>\nDone.")
    assert (work / "A.md").read_text() == "hi A", "direct XML create_document must write file"
    assert "create_document" not in cleaned and "<path>" not in cleaned, "markup must be stripped"
    assert events[0]["tool"] == "create_document" and events[0]["exit_code"] == 0


def test_invoke_create():
    h, work = fresh()
    cleaned, events = run(h, 'sure\n<invoke name="create_document">\n<parameter name="path">B.md</parameter>\n<parameter name="content">hi B</parameter>\n</invoke>')
    assert (work / "B.md").read_text() == "hi B", "<invoke> create_document must write file"
    assert cleaned.strip() == "sure"


def test_tool_call_wrapper_bash():
    h, _ = fresh()
    cleaned, events = run(h, '<tool_call><invoke name="bash"><parameter name="command">echo hi</parameter></invoke></tool_call>')
    assert events and events[0]["tool"] == "bash", "wrapped <invoke> bash must parse"
    assert "<tool_call>" not in cleaned, "wrapper tags must be stripped"


def test_edit_document_direct():
    h, work = fresh()
    run(h, "<edit_document>\n<path>overview.md</path>\n<find>Existing.</find>\n<replace>Replaced!</replace>\n</edit_document>")
    assert "Replaced!" in (work / "overview.md").read_text(), "edit_document find/replace must apply"


def test_yaml_block_scalar_content():
    # DeepSeek emits `content: |-` with indented body — must dedent, not keep `|-`.
    h, work = fresh()
    run(h, "```edit_document\npath: overview.md\ncontent: |-\n  # Title\n\n  Body line\n```")
    got = (work / "overview.md").read_text()
    assert got == "# Title\n\nBody line", f"block scalar must dedent cleanly, got {got!r}"


def test_fenced_still_works():
    h, work = fresh()
    run(h, "fine\n```create_document\npath: C.md\ncontent:\nhi C\n```")
    assert (work / "C.md").read_text() == "hi C", "plain fenced blocks must still work"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
    print("all tool-parsing checks passed")
