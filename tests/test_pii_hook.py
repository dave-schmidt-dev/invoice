"""INV-1 gate: the pre-commit PII guard must fail closed.

Drives the ACTUAL `hooks/pre-commit` shell script (not a reimplementation)
via `subprocess`, inside a throwaway temporary git repository per test. The
real hook is located by walking up from this test file's own path to the
project root, so this suite is portable and never hardcodes an absolute
project path.

Every fixture pattern planted in a test's own `pii-patterns.txt` is a
synthetic, made-up token (e.g. ``zzsecrettoken``) — never real client data.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def _find_project_root(start: Path) -> Path:
    """Walk up from *start* looking for hooks/pre-commit."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "hooks" / "pre-commit").is_file():
            return candidate
    raise RuntimeError(
        f"Could not locate project root (hooks/pre-commit) above {start}"
    )


PROJECT_ROOT = _find_project_root(Path(__file__).parent)
REAL_HOOK = PROJECT_ROOT / "hooks" / "pre-commit"


def _git_available() -> bool:
    return shutil.which("git") is not None


class PiiHookTestCase(unittest.TestCase):
    """Base class that builds a throwaway git repo wired to the real hook."""

    def setUp(self):
        if not _git_available():
            self.skipTest("git is not available on PATH")
        if not REAL_HOOK.is_file():
            self.skipTest(f"real hook not found at {REAL_HOOK}")

        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmpdir.name)

        self._run(["git", "init", "-q"])
        self._run(["git", "config", "user.email", "pii-hook-test@example.invalid"])
        self._run(["git", "config", "user.name", "PII Hook Test"])
        self._run(["git", "config", "commit.gpgsign", "false"])

        (self.repo / "hooks").mkdir(parents=True, exist_ok=True)
        shutil.copy2(REAL_HOOK, self.repo / "hooks" / "pre-commit")
        os.chmod(self.repo / "hooks" / "pre-commit", 0o755)

        self._run(["git", "config", "core.hooksPath", "hooks"])

    def tearDown(self):
        self._tmpdir.cleanup()

    # -- helpers ----------------------------------------------------------

    def _run(self, args, **kwargs):
        kwargs.setdefault("cwd", self.repo)
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        return subprocess.run(args, **kwargs)

    def _write_patterns(self, content: str):
        (self.repo / "hooks" / "pii-patterns.txt").write_text(content)

    def _omit_patterns(self):
        path = self.repo / "hooks" / "pii-patterns.txt"
        if path.exists():
            path.unlink()

    def _write_file(self, name: str, content: str):
        (self.repo / name).write_text(content)

    def _write_binary_file(self, name: str, data: bytes):
        (self.repo / name).write_bytes(data)

    def _stage(self, *paths):
        self._run(["git", "add", *paths])

    def _commit(self, message="test commit"):
        """Stage's already done by caller; just attempt the commit."""
        return self._run(["git", "commit", "-q", "-m", message])


class TestKnownPiiPatternBlocked(PiiHookTestCase):
    """Case 1: a staged line containing a known (synthetic) PII pattern."""

    def test_known_pattern_blocks_commit(self):
        self._write_patterns("zzsecrettoken\n")
        self._write_file("notes.txt", "some benign line\nleaked zzsecrettoken here\n")
        self._stage("hooks/pii-patterns.txt", "notes.txt")

        result = self._commit("add notes with planted pii pattern")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCKED", result.stderr)


class TestPlusLeadingContentBlocked(PiiHookTestCase):
    """Case 2: added-line content starting with a single '+' (E.164-style).

    Proves the added-line filter (`grep '^+' | grep -v '^+++'`) does not
    drop legitimate '+'-prefixed content — only the diff's own `+++` file
    header line is excluded, not a content line that happens to start with
    exactly one '+'.
    """

    def test_plus_prefixed_e164_like_line_blocks_commit(self):
        self._write_patterns("15551234567\n")
        self._write_file("contacts.txt", "+15551234567\n")
        self._stage("hooks/pii-patterns.txt", "contacts.txt")

        result = self._commit("add contact with plus-prefixed number")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCKED", result.stderr)


class TestMissingPatternFileBlocked(PiiHookTestCase):
    """Case 3: missing hooks/pii-patterns.txt fails closed."""

    def test_missing_pattern_file_blocks_commit(self):
        self._omit_patterns()
        self._write_file("notes.txt", "totally benign content\n")
        self._stage("notes.txt")

        result = self._commit("add notes without pattern file")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("pattern file missing", result.stderr)


class TestEmptyPatternFileBlocked(PiiHookTestCase):
    """Case 4: a pattern file with only comments/blanks fails closed."""

    def test_empty_pattern_file_blocks_commit(self):
        self._write_patterns("# just a comment\n\n   \n# another comment\n")
        self._write_file("notes.txt", "totally benign content\n")
        self._stage("hooks/pii-patterns.txt", "notes.txt")

        result = self._commit("add notes with empty pattern list")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("no active patterns", result.stderr)


class TestMalformedRegexBlocked(PiiHookTestCase):
    """Case 5: a malformed regex in the pattern file is a grep error,
    which the hook treats as fail-closed rather than silently passing."""

    def test_malformed_regex_blocks_commit(self):
        self._write_patterns("foo(bar\n")
        self._write_file("notes.txt", "totally benign content\n")
        self._stage("hooks/pii-patterns.txt", "notes.txt")

        result = self._commit("add notes with malformed pattern regex")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("grep failed", result.stderr)


class TestAddedBinaryFileBlocked(PiiHookTestCase):
    """Case 6: an added binary file can't be line-scanned, so it's blocked."""

    def test_added_binary_file_blocks_commit(self):
        self._write_patterns("zzsecrettoken\n")
        # Bytes with embedded NULs so git/diff classify this as binary.
        binary_payload = b"\x00\x01\x02\xff\xfe" + os.urandom(64)
        self._write_binary_file("blob.bin", binary_payload)
        self._stage("hooks/pii-patterns.txt", "blob.bin")

        result = self._commit("add binary blob")

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCKED", result.stderr)
        self.assertIn("binary file", result.stderr)


class TestCleanDiffPasses(PiiHookTestCase):
    """Case 7: a clean staged diff with no pattern matches passes."""

    def test_clean_text_diff_passes(self):
        self._write_patterns("zzsecrettoken\n")
        self._write_file("notes.txt", "nothing sensitive here at all\n")
        self._stage("hooks/pii-patterns.txt", "notes.txt")

        result = self._commit("add clean notes")

        self.assertEqual(result.returncode, 0, result.stderr)


class TestDoublePlusPrefixEdgeCase(PiiHookTestCase):
    """Case 8 (documented known limitation): content that itself begins
    with '++' collides with the diff's own '+++ b/<path>' file header.

    When the staged line's content starts with '++', e.g. the file content
    is "++zzsecrettoken", the unified diff renders the added line as
    "+++zzsecrettoken" (one '+' for "this is an added line" plus the two
    literal '+' characters from the content). The hook's added-line filter
    is `grep '^+' | grep -v '^+++'`, which excludes ANY line starting with
    three '+' characters — intended to drop the "+++ b/<path>" file-header
    line, but it can't distinguish that header from a content line that
    happens to start with '++'. As a result this content is silently
    dropped from `added_lines` and never reaches the pattern scan.

    This is a KNOWN LIMITATION of the current hook (not something this test
    suite is meant to fix — out of scope per the hook's own comments). This
    test documents and locks in the CURRENT behavior: such a commit is
    allowed through (exit 0) despite containing the planted PII pattern.
    If a future hardening pass changes this, this test should be updated
    to match the new intended behavior.
    """

    def test_double_plus_prefixed_content_is_scanned(self):
        # Regression: a content line beginning with '++' renders as '+++...'
        # in the unified diff. The hook must still scan it — it gates on the
        # diff's hunk structure ('@@' body) instead of a naive '^+++' filter,
        # so '++'-content is no longer confused with the '+++ b/<path>' file
        # header and can no longer smuggle a PII pattern past the guard.
        self._write_patterns("zzsecrettoken\n")
        self._write_file("plusplus.txt", "++zzsecrettoken\n")
        self._stage("hooks/pii-patterns.txt", "plusplus.txt")

        result = self._commit("add content starting with double plus")

        self.assertNotEqual(
            result.returncode,
            0,
            "'++'-prefixed content containing a planted pattern must be "
            f"BLOCKED. Hook stderr: {result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
