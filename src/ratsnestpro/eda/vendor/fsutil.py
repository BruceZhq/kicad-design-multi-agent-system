"""Filesystem helpers.

On Windows, ``os.replace`` can transiently fail with a permission/sharing error
when another process (OneDrive sync, antivirus, an editor) briefly holds the
destination open. Retrying with a short backoff makes atomic saves robust on
synced folders without giving up atomicity.
"""

from __future__ import annotations

import os
import time


def replace_with_retry(src: str, dst: str, attempts: int = 12, delay: float = 0.08) -> None:
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # Windows: sharing violation / access denied
            last = e
            time.sleep(delay * (i + 1))
        except OSError as e:
            # WinError 5 (access denied) / 32 (sharing violation) surface as OSError too.
            if getattr(e, "winerror", None) in (5, 32):
                last = e
                time.sleep(delay * (i + 1))
            else:
                raise
    if last:
        raise last
