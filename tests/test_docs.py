"""The deploy verification sentinels in docs/deploying.md must stay accurate.

The old HANDOFF.md documented exactly this kind of `grep -c` check, and a
sentinel count that quietly drifts from the code is worse than no sentinel at
all — it tells the person deploying that a broken copy is fine. This parses
the fenced example straight out of the doc and re-runs it against the actual
packaged files, so an edit to the bash without updating the doc fails a test
instead of failing someone's deploy months later.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from model_launcher.core.remote import remote_dir

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"

SENTINEL_LINE = re.compile(
    r'^grep -c (?P<pattern>\'[^\']*\'|\S+) "\$DEST/(?P<path>[^"]+)"\s*#\s*(?P<count>\d+)\s*$'
)


def _sentinels():
    text = (DOCS_DIR / "deploying.md").read_text()
    found = [
        m.groupdict()
        for line in text.splitlines()
        if (m := SENTINEL_LINE.match(line.strip()))
    ]
    assert found, (
        "no sentinel lines found in docs/deploying.md — did its format change?"
    )
    return found


def test_deploying_doc_exists_and_is_not_empty():
    path = DOCS_DIR / "deploying.md"
    assert path.is_file()
    assert len(path.read_text()) > 500


def test_deploying_doc_mentions_every_packaged_file():
    """The file tree in the doc must not silently drift from what actually
    ships — a file added to remote/ with nothing said about it is easy to
    miss when deploying by hand."""
    text = (DOCS_DIR / "deploying.md").read_text()
    for path in remote_dir().rglob("*"):
        if path.is_dir() or path.name == "__pycache__":
            continue
        assert path.name in text, (
            f"{path.name} is packaged but not mentioned in the doc"
        )


def test_deploying_doc_covers_the_example_queue():
    """example.queue is how someone actually gets a working queue file —
    the doc must say to copy it, not just that it exists."""
    text = (DOCS_DIR / "deploying.md").read_text()
    assert "example.queue" in text
    assert "cp $DEST/example.queue" in text


BASH_FENCE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
#: A bare `<word>` looks like a fill-in-the-blank placeholder but is real bash
#: syntax (input/output redirection): `--host <alias>` redirects stdin from a
#: file named `alias` instead of taking "alias" as an argument. This bit a
#: real deploy — every runnable example must spell a placeholder some other
#: way (this doc's convention: ALL_CAPS, no brackets).
ANGLE_PLACEHOLDER = re.compile(r"<[a-zA-Z][a-zA-Z0-9_-]*>")


def test_no_docs_have_angle_bracket_placeholders_in_runnable_bash():
    for path in sorted(DOCS_DIR.glob("*.md")):
        text = path.read_text()
        for block in BASH_FENCE.findall(text):
            match = ANGLE_PLACEHOLDER.search(block)
            assert not match, (
                f"{path.name} has a shell-breaking placeholder {match.group()!r} "
                "in a ```bash block — `<` and `>` are real redirection syntax, "
                "not fill-in-the-blank notation. Use an ALL_CAPS word instead."
            )


def test_verification_sentinels_match_the_packaged_files():
    for sentinel in _sentinels():
        pattern = sentinel["pattern"].strip("'")
        target = remote_dir() / sentinel["path"]
        assert target.is_file(), (
            f"docs/deploying.md references a missing file: {target}"
        )
        proc = subprocess.run(
            ["grep", "-c", pattern, str(target)],
            capture_output=True,
            text=True,
        )
        actual = int(proc.stdout.strip() or "0")
        expected = int(sentinel["count"])
        assert actual == expected, (
            f"docs/deploying.md says `grep -c {pattern} {sentinel['path']}` == "
            f"{expected}, but it is actually {actual} — update the doc"
        )
