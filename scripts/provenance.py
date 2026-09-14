from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


_WS = re.compile(r"\s+")


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def block_source_hash(page: int, text: str) -> str:
    """Stable content signature independent of ephemeral p1bN ordering."""
    normalized = _WS.sub(" ", (text or "").strip())
    payload = f"{int(page)}\0{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def file_record(path: str | Path) -> dict:
    p = Path(path).resolve()
    st = p.stat()
    return {"name": p.name, "size": st.st_size, "sha256": sha256_file(p)}


def write_manifest(path: str | Path, source: str | Path,
                   effective_source: str | Path, pages: str) -> dict:
    src = Path(source).resolve()
    eff = Path(effective_source).resolve()
    data = {
        "schema": 1,
        "source": file_record(src),
        "effective_source": file_record(eff),
        "normalized": eff != src,
        "pages": str(pages),
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def validate_manifest(path: str | Path, source: str | Path):
    """Return (ok, manifest-or-None, reason)."""
    p = Path(path)
    if not p.is_file():
        return True, None, "missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return False, None, f"manifest 解析失败: {e}"
    expected = (data.get("source") or {}).get("sha256")
    if not expected:
        return False, data, "manifest 缺少 source.sha256"
    actual = sha256_file(source)
    if actual != expected:
        return False, data, f"源 PDF SHA-256 不匹配: manifest={expected[:12]}… 当前={actual[:12]}…"
    return True, data, "ok"


def page_selection_covers(prepared: str, requested: str) -> bool:
    """Whether a finite requested selection is available in prepared artifacts."""
    prepared = str(prepared or "all").strip().lower()
    requested = str(requested or "all").strip().lower()
    if prepared == "all":
        return True
    if requested == "all":
        return False

    def expand(spec: str) -> set[int]:
        pages: set[int] = set()
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-", 1)
                pages.update(range(int(start), int(end) + 1))
            else:
                pages.add(int(part))
        return pages

    try:
        return expand(requested).issubset(expand(prepared))
    except (TypeError, ValueError):
        return False
