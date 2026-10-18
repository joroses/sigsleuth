"""A saved snapshot file of sections ("## title" lines, then a body), compared
with what a test writes now. A change fails the test with a diff per section,
so each change names the request it belongs to."""

import difflib
import os
import re
from pathlib import Path


def check_snapshot(path: Path, written: str, update_variable: str) -> None:
    """Fail unless written matches the saved file; rewrite the file instead
    when the environment variable update_variable is set."""
    if os.environ.get(update_variable):
        path.write_text(written, encoding="utf-8")
    saved = path.read_text(encoding="utf-8") if path.exists() else ""
    assert written == saved, (
        f"{path.name} changed; if that is intended, rerun with {update_variable}=1 "
        "and read the diff before committing it.\n" + changes(saved, written)
    )


def changes(saved: str, written: str) -> str:
    before, after = _sections(saved), _sections(written)
    return "".join(
        f"\n## {title}\n"
        + "".join(difflib.unified_diff(before.get(title, []), after.get(title, []), n=1))
        for title in dict.fromkeys([*before, *after])
        if before.get(title) != after.get(title)
    )


def _sections(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for block in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        title, _, body = block.partition("\n")
        found[title] = [line + "\n" for line in body.strip("\n").splitlines()]
    return found
