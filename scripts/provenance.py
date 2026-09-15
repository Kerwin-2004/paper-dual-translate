from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


_WS = re.compile(r"\s+")
_INLINE_MARKER = "PDT_INLINE"


def _fragment_sort_key(item: dict) -> tuple[float, float, str]:
    bbox = item.get("bbox") or ()
    try:
        x = float(bbox[0]) if len(bbox) > 0 else 0.0
        y = float(bbox[1]) if len(bbox) > 1 else 0.0
    except (TypeError, ValueError):
        x = y = 0.0
    return y, x, str(item.get("id", ""))


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


def block_layout_uid(page: int, column: str, bbox, text: str) -> str:
    """Layout-sensitive identity for detecting same-text block swaps."""
    normalized = _WS.sub(" ", (text or "").strip())
    quantized = ",".join(f"{round(float(value) * 2) / 2:.1f}" for value in bbox)
    payload = f"{int(page)}\0{column or '?'}\0{quantized}\0{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def inline_fragment_specs(block: dict) -> list[dict]:
    """Return deterministic marker metadata for nested translation fragments."""
    fragments = sorted(
        (item for item in block.get("inline_fragments", []) if item.get("text")),
        key=_fragment_sort_key,
    )
    specs = []
    for index, fragment in enumerate(fragments):
        salt = hashlib.sha256(
            f"{fragment.get('id', '')}\0{fragment.get('text', '')}\0{fragment.get('bbox', '')}"
            .encode("utf-8")
        ).hexdigest()[:8]
        token = f"{_INLINE_MARKER}_{index}_{salt}"
        specs.append({
            "id": fragment.get("id"),
            "text": str(fragment.get("text", "")).strip(),
            "math": bool(fragment.get("math_only") or fragment.get("has_math")),
            "open": f"[[{token}]]",
            "close": f"[[/{token}]]",
        })
    return specs


def block_translation_source(block: dict) -> str:
    """Compose the model source, including every nested fragment exactly once."""
    source = str(block.get("text", "")).strip()
    additions = [f"{spec['open']}{spec['text']}{spec['close']}"
                 for spec in inline_fragment_specs(block)]
    if additions:
        source = source + "\n" + "\n".join(additions)
    return source


def block_identity(page: int, block: dict) -> dict[str, str]:
    """Compute identity from the block's current content and final geometry."""
    text = block_translation_source(block)
    return {
        "source_hash": block_source_hash(page, text),
        "layout_uid": block_layout_uid(
            page, block.get("column", "?"), block.get("bbox", ()), text),
    }


def refresh_block_identities(blocks_data: dict) -> int:
    """Refresh identities after every preprocessing mutation; return change count."""
    changed = 0
    for page in blocks_data.get("pages", []):
        page_no = int(page.get("page", 0))
        for block in page.get("blocks", []):
            identity = block_identity(page_no, block)
            if any(block.get(field) != value for field, value in identity.items()):
                changed += 1
            block.update(identity)
    return changed


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
