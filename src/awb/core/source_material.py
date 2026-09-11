from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_SOURCE_BYTES = 25 * 1024 * 1024
SUPPORTED_SUFFIXES = {'.pdf', '.txt', '.md', '.markdown', '.tex', '.csv', '.json'}
CATALOG_NAME = 'catalog.json'
INDEX_NAME = 'INDEX.md'


class SourceMaterialError(RuntimeError):
    pass


def _safe_name(name: str) -> str:
    raw = Path(name or 'source').name.strip() or 'source'
    stem = re.sub(r'[^A-Za-z0-9._ -]+', '_', Path(raw).stem).strip(' ._-') or 'source'
    suffix = Path(raw).suffix.lower()
    return f'{stem[:100]}{suffix}'


def _catalog_path(root: Path) -> Path:
    return root / 'sources' / CATALOG_NAME


def list_sources(root: Path) -> list[dict[str, Any]]:
    path = _catalog_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_catalog(root: Path, items: list[dict[str, Any]]) -> None:
    sources = root / 'sources'
    sources.mkdir(parents=True, exist_ok=True)
    _catalog_path(root).write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding='utf-8')
    lines = [
        '# User-provided source material',
        '',
        'These files were uploaded by the user and are immutable evidence. Read the extracted text before making claims that depend on them.',
        '',
    ]
    for item in items:
        lines.append(f"## {item['filename']}")
        lines.append(f"- Original: `{item['original_path']}`")
        lines.append(f"- Extracted text: `{item['text_path']}`")
        lines.append(f"- SHA256: `{item['sha256']}`")
        lines.append(f"- Status: {item['status']}")
        if item.get('pages') is not None:
            lines.append(f"- Pages: {item['pages']}")
        lines.append('')
    (sources / INDEX_NAME).write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')


def _extract_pdf(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SourceMaterialError('PDF support is unavailable: pypdf is not installed') from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise SourceMaterialError(f'Invalid or unreadable PDF: {exc}') from exc
    parts: list[str] = []
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ''
        except Exception:
            text = ''
        parts.append(f'\n\n--- PAGE {page_no} ---\n\n{text.strip()}')
    return ''.join(parts).strip(), len(reader.pages)


def _extract_text(data: bytes, suffix: str) -> tuple[str, int | None]:
    if suffix == '.pdf':
        return _extract_pdf(data)
    try:
        return data.decode('utf-8'), None
    except UnicodeDecodeError:
        return data.decode('utf-8', errors='replace'), None


def ingest_source(root: Path, filename: str, data: bytes, content_type: str | None = None) -> dict[str, Any]:
    if not data:
        raise SourceMaterialError('Empty source file')
    if len(data) > MAX_SOURCE_BYTES:
        raise SourceMaterialError('Source file exceeds the 25 MB per-file limit')

    safe = _safe_name(filename)
    suffix = Path(safe).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise SourceMaterialError('Unsupported source type. Use PDF, TXT, MD, TEX, CSV or JSON.')

    digest = hashlib.sha256(data).hexdigest()
    existing = list_sources(root)
    for item in existing:
        if item.get('sha256') == digest:
            return item

    sources = root / 'sources'
    original_dir = sources / 'original'
    extracted_dir = sources / 'extracted'
    original_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir.mkdir(parents=True, exist_ok=True)

    prefix = digest[:12]
    original = original_dir / f'{prefix}-{safe}'
    text_file = extracted_dir / f'{prefix}-{Path(safe).stem}.txt'
    original.write_bytes(data)

    text, pages = _extract_text(data, suffix)
    text_file.write_text(text, encoding='utf-8')
    if suffix == '.pdf' and not text.strip():
        status = 'stored; no embedded text found (OCR required)'
    else:
        status = 'ready'

    item: dict[str, Any] = {
        'id': prefix,
        'filename': safe,
        'content_type': content_type or '',
        'bytes': len(data),
        'sha256': digest,
        'uploaded_at': datetime.now(timezone.utc).isoformat(),
        'original_path': str(original.relative_to(root)),
        'text_path': str(text_file.relative_to(root)),
        'text_chars': len(text),
        'pages': pages,
        'status': status,
    }
    existing.append(item)
    _write_catalog(root, existing)
    return item
