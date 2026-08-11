"""Short-lived, single-use, in-memory staging for uploaded documents.

Reaching the validator from an iPad — or any device without a shell — needs
somewhere to put the file. This is that somewhere, kept as small and as
short-lived as it can be:

* **Memory only.** Nothing is written to disk, so nothing survives a restart
  and nothing lands in a backup.
* **Single use.** ``take`` removes the entry. A code that has been validated
  once cannot be replayed.
* **Expiring.** Entries are dropped after ``ttl_seconds`` whether used or not,
  and every put and take sweeps the expired ones.
* **Bounded.** A per-file cap and a total cap, so a flood of uploads cannot
  exhaust the box.

This is a real change to the "stores nothing" property of the base64 and URL
routes, and is worth being precise about: a document sits in RAM for at most
the TTL, or until it is validated, whichever comes first.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

__all__ = ["UploadStore", "StagedUpload", "UploadError"]


class UploadError(Exception):
    """Raised with a message safe to show the caller."""


@dataclass
class StagedUpload:
    content: bytes
    filename: str
    created: float


class UploadStore:
    def __init__(
        self,
        ttl_seconds: int = 1800,
        max_file_bytes: int = 12 * 1024 * 1024,
        max_total_bytes: int = 64 * 1024 * 1024,
        max_entries: int = 16,
    ):
        self.ttl = ttl_seconds
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self._items: Dict[str, StagedUpload] = {}
        self._lock = threading.Lock()

    # -- internals --------------------------------------------------------

    def _purge_locked(self, now: float) -> None:
        dead = [k for k, v in self._items.items() if now - v.created > self.ttl]
        for k in dead:
            # Drop the reference; CPython frees the buffer immediately.
            self._items.pop(k, None)

    def _total_locked(self) -> int:
        return sum(len(v.content) for v in self._items.values())

    # -- API ---------------------------------------------------------------

    def put(self, content: bytes, filename: str) -> str:
        if not content:
            raise UploadError("the uploaded file is empty")
        if len(content) > self.max_file_bytes:
            raise UploadError(
                f"file is {len(content) // 1024} KB, over the "
                f"{self.max_file_bytes // (1024 * 1024)} MB limit"
            )
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            if len(self._items) >= self.max_entries:
                raise UploadError(
                    f"too many uploads are waiting ({self.max_entries}). They "
                    f"expire after {self.ttl // 60} minutes; try again shortly."
                )
            if self._total_locked() + len(content) > self.max_total_bytes:
                raise UploadError(
                    "the staging area is full. Uploads expire after "
                    f"{self.ttl // 60} minutes; try again shortly."
                )
            code = secrets.token_urlsafe(9)
            while code in self._items:  # astronomically unlikely; cheap to be sure
                code = secrets.token_urlsafe(9)
            self._items[code] = StagedUpload(content=content, filename=filename, created=now)
            return code

    def take(self, code: str) -> Tuple[bytes, str]:
        """Retrieve and remove. Raises UploadError if unknown or expired."""
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            item = self._items.pop(code.strip(), None)
        if item is None:
            raise UploadError(
                f"upload id '{code.strip()[:24]}' is unknown, already used, or "
                f"expired. Codes are valid once and for {self.ttl // 60} minutes; "
                f"upload the file again to get a new one."
            )
        return item.content, item.filename

    def peek_count(self) -> int:
        with self._lock:
            self._purge_locked(time.time())
            return len(self._items)


UPLOAD_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ATS validator — upload</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 34rem; margin: 0 auto; padding: 2rem 1.25rem; }}
  h1 {{ font-size: 1.3rem; margin-bottom: .25rem; }}
  p.sub {{ color: #666; margin-top: 0; }}
  label {{ display: block; font-weight: 600; margin: 1.25rem 0 .35rem; }}
  input[type=file] {{ width: 100%; padding: .75rem; border: 1px solid #8884;
                      border-radius: .5rem; background: #8881; }}
  button {{ margin-top: 1.5rem; width: 100%; padding: .85rem; font-size: 1rem;
            font-weight: 600; border: 0; border-radius: .5rem;
            background: #0b6cff; color: #fff; }}
  .code {{ font: 1.4rem ui-monospace, SFMono-Regular, Menlo, monospace;
           padding: .9rem; border: 1px dashed #8886; border-radius: .5rem;
           text-align: center; user-select: all; margin: 1rem 0; }}
  .note {{ font-size: .85rem; color: #666; }}
  .err {{ color: #c00; font-weight: 600; }}
</style></head><body>
<h1>ATS validator</h1>
<p class="sub">Upload a CV, get a one-time id, then ask Claude to validate it.</p>
{body}
<p class="note">Files are held in memory only, for a single use, and are
discarded after {ttl} minutes or as soon as they are validated — whichever
comes first. Nothing is written to disk.</p>
</body></html>
"""

UPLOAD_FORM = """<form method="post" enctype="multipart/form-data">
  <label for="f">Choose a file</label>
  <input id="f" name="file" type="file" required
         accept=".docx,.pdf,.rtf,.txt,.md">
  <button type="submit">Upload</button>
</form>
<p class="note">On iPhone or iPad this opens the Files picker, so you can pick
straight out of iCloud Drive.</p>
"""


def result_page(code: str, filename: str, ttl_minutes: int) -> str:
    body = (
        f"<p>Uploaded <strong>{_escape(filename)}</strong>. Your one-time id:</p>"
        f'<div class="code">{_escape(code)}</div>'
        f"<p>In Claude: <em>“validate upload {_escape(code)}”</em></p>"
        f'<p><a href="">Upload another</a></p>'
    )
    return UPLOAD_PAGE.format(body=body, ttl=ttl_minutes)


def form_page(ttl_minutes: int, error: Optional[str] = None) -> str:
    body = (f'<p class="err">{_escape(error)}</p>' if error else "") + UPLOAD_FORM
    return UPLOAD_PAGE.format(body=body, ttl=ttl_minutes)


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
