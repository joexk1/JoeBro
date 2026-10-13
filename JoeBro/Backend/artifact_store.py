"""Private, bounded artifact references shared by the file-transfer tools.

An artifact is a file the agent fetched or staged: it lives in <data dir>/artifacts
as `<id>.blob` plus `<id>.json` metadata, and tools pass around the 32-hex id,
never the bytes. Nothing leaves the store except through copy_to_folder, which
only writes into a folder listed in artifact_roots.json (default: Documents and
Downloads) and never overwrites or follows a symlink.
"""
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from jb_core import data_dir

MAX_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ARTIFACTS = 1000
# Where a file came from. Provenance only, never fetched again.
SOURCES = ("email", "upload", "url", "doc", "device", "folder")


class ArtifactError(ValueError):
    pass


def store_root():
    root = data_dir() / "artifacts"
    if root.is_symlink():
        raise ArtifactError("Artifact directory cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root.resolve()


def configured_roots():
    """name -> absolute folder the user allows files to be written into or read
    from. Defaults to Documents and Downloads; edit artifact_roots.json (a
    {"name": "/abs/path"} object in the data folder) to change them."""
    config = Path(os.environ.get("JOEBRO_ARTIFACT_ROOTS_FILE") or data_dir() / "artifact_roots.json").expanduser()
    raw = os.environ.get("JOEBRO_ARTIFACT_ROOTS")
    home = Path.home()
    try:
        if raw:
            values = json.loads(raw)
        elif config.exists():
            values = json.loads(config.read_text())
        else:
            values = {"documents": str(home / "Documents"), "downloads": str(home / "Downloads")}
    except (OSError, ValueError):
        raise ArtifactError("Artifact folder configuration is invalid") from None
    if not isinstance(values, dict):
        raise ArtifactError("Artifact roots must be a name-to-absolute-path object")
    result = {}
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name) or not isinstance(value, str):
            raise ArtifactError("Invalid artifact root configuration")
        p = Path(value).expanduser()
        if not p.is_absolute() or not p.is_dir():
            continue
        result[name] = p.resolve()
    return result


def safe_name(filename):
    name = Path(str(filename or "artifact.bin").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:180].strip(" .")
    return name if name and name not in (".", "..") else "artifact.bin"


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ArtifactError("Invalid artifact_id")
    return value


@contextmanager
def _locked():
    root = store_root()
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield root


def _write(path, data):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".artifact-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _record(artifact_id):
    p = store_root() / (_id(artifact_id) + ".json")
    if p.is_symlink():
        raise ArtifactError("Artifact metadata cannot be a symlink")
    try:
        record = json.loads(p.read_text())
    except (OSError, ValueError):
        raise ArtifactError("Artifact not found") from None
    if not isinstance(record, dict) or record.get("artifact_id") != artifact_id:
        raise ArtifactError("Artifact metadata is invalid")
    return record


def _ref(record):
    return {k: record[k] for k in ("artifact_id", "path", "filename", "mime_type", "size_bytes", "sha256", "created_at") if k in record}


def save_bytes(data, filename, mime_type=None, source_url=None, source=None):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ArtifactError("Artifact exceeds 50 MiB or is not bytes")
    name = safe_name(filename)
    with _locked() as root:
        if len(list(root.glob("*.json"))) >= MAX_ARTIFACTS:
            raise ArtifactError("Artifact count limit reached")
        if sum(p.stat().st_size for p in root.glob("*.blob")) + len(data) > MAX_TOTAL_BYTES:
            raise ArtifactError("Artifact storage limit reached (256 MiB)")
        aid = uuid.uuid4().hex
        p = root / (aid + ".blob")
        record = {"artifact_id": aid, "path": str(p), "filename": name,
                  "mime_type": mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
                  "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "created_at": time.time()}
        if source in SOURCES:
            record["source"] = source
        # The URL is provenance only, and is stored without credentials or query tokens.
        if source_url:
            from urllib.parse import urlsplit, urlunsplit
            url = urlsplit(source_url)
            if url.scheme == "https" and url.hostname and not url.username:
                record["source_url"] = urlunsplit((url.scheme, url.netloc, url.path, "", ""))
        _write(p, data)
        try:
            _write(root / (aid + ".json"), json.dumps(record).encode())
        except Exception:
            p.unlink()
            raise
        return _ref(record)


def resolve_artifact(artifact_id):
    """The blob's path, after checking it is the file the metadata describes."""
    record = _record(artifact_id)
    p = Path(record.get("path", ""))
    root = store_root()
    if record.get("moved_to"):
        if not any(p.is_relative_to(r) for r in configured_roots().values()):
            raise ArtifactError("Moved artifact is outside configured folders")
    elif p != root / (artifact_id + ".blob"):
        raise ArtifactError("Artifact path failed ownership check")
    if p.is_symlink() or p.resolve() != p or not p.is_file():
        raise ArtifactError("Artifact file missing or symlinked")
    if p.stat().st_size != record.get("size_bytes") or p.stat().st_size > MAX_BYTES:
        raise ArtifactError("Artifact size changed")
    if hashlib.sha256(p.read_bytes()).hexdigest() != record.get("sha256"):
        raise ArtifactError("Artifact contents changed")
    if not record.get("moved_to"):
        try:
            os.utime(p, None)   # last-use marker for the janitor
        except OSError:
            pass
    return p


def discard_artifact(artifact_id):
    """Delete an intermediate artifact (blob + metadata). Never touches files
    already delivered into a user folder."""
    with _locked() as root:
        try:
            record = _record(artifact_id)
        except ArtifactError:
            return False
        if record.get("moved_to"):
            return False
        for suffix in (".blob", ".json"):
            (root / (artifact_id + suffix)).unlink(missing_ok=True)
        return True


def artifact_info(artifact_id):
    resolve_artifact(artifact_id)
    return _ref(_record(artifact_id))


def list_artifacts(limit=30):
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ArtifactError("limit must be 1 to 100")
    result = []
    for p in sorted(store_root().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            result.append(_ref(_record(p.stem)))
        except ArtifactError:
            continue
    return {"artifacts": result, "count": len(result)}


def _checked_relative(path, what):
    if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ArtifactError(f"{what} must be a relative path without traversal")


def copy_to_folder(artifact_id, root_name, folder="", filename=None, move=False):
    roots = configured_roots()
    if root_name not in roots:
        raise ArtifactError("Unknown folder root; it must be listed in artifact_roots.json")
    _checked_relative(folder, "Folder")
    root = roots[root_name]
    directory = root / folder
    # Never follow existing symlinks, including aliases back inside the same root.
    current = root
    for component in Path(folder).parts:
        current = current / component
        if current.is_symlink():
            raise ArtifactError("Destination folder cannot contain symlinks")
    if not directory.resolve().is_relative_to(root):
        raise ArtifactError("Destination escapes configured folder")
    with _locked() as storage:
        source = resolve_artifact(artifact_id)
        record = _record(artifact_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = safe_name(filename or record["filename"])
        if filename and filename != name:
            raise ArtifactError("Filename must be a simple safe basename")
        target = directory / name
        if target.exists() or target.is_symlink():
            raise ArtifactError("Destination exists; choose another filename")
        fd, temp = tempfile.mkstemp(dir=directory, prefix=".joebro-artifact-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(source.read_bytes())
                out.flush()
                os.fsync(out.fileno())
            os.link(temp, target)           # atomic publish with no overwrite, unlike os.replace
        except FileExistsError:
            raise ArtifactError("Destination exists; choose another filename") from None
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        if move:
            old = record["path"]
            record.update(path=str(target), moved_to=str(target))
            _write(storage / (artifact_id + ".json"), json.dumps(record).encode())
            if Path(old) != target:
                Path(old).unlink()
        return {"artifact_id": artifact_id, "path": str(target), "mime_type": record["mime_type"],
                "size_bytes": record["size_bytes"], "operation": "moved" if move else "copied"}


def import_file(root_name, relative_path, tag=None):
    """Import an existing file from a configured folder into a stable shared ref."""
    roots = configured_roots()
    if root_name not in roots:
        raise ArtifactError("Unknown folder root; it must be listed in artifact_roots.json")
    _checked_relative(relative_path, "Source path")
    if not relative_path:
        raise ArtifactError("Source path must name a file")
    root = roots[root_name]
    source = root / relative_path
    current = root
    for component in Path(relative_path).parts:
        current = current / component
        if current.is_symlink():
            raise ArtifactError("Source path cannot contain symlinks")
    if not source.resolve().is_relative_to(root) or not source.is_file():
        raise ArtifactError("Source file missing or outside configured folder")
    if source.stat().st_size > MAX_BYTES:
        raise ArtifactError("Source exceeds 50 MiB")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    return save_bytes(data, source.name, source=tag)
