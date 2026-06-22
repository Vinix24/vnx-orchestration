# Task t4-01 — Safe path resolver (directory-traversal sandbox)

Tier: T4 frontier. Deadline: 1 hour wallclock. This is a SECURITY task graded on
adversarial depth — a naive `".." in path` check scores low; only a resolver that
truly anchors inside the base directory scores full marks.

## Context

You build the boundary that lets a web service serve files from ONE directory and
nothing else. Given a configured base directory and an untrusted user-supplied path,
return the resolved absolute path IFF it stays inside the base directory; otherwise
reject. This is the exact control that stops "download `../../../../etc/passwd`".

## Deliverable

Create `safe_path.py` with:

```python
class UnsafePath(Exception):
    """Raised when a user path escapes (or could escape) the base directory."""

def resolve_safe(base: str, user_path: str) -> str:
    """Return the absolute resolved path of `user_path` interpreted relative to
    `base`, IFF the result is contained within `base`. Raise UnsafePath otherwise.

    `base` is a trusted absolute directory that exists. `user_path` is untrusted.
    """
```

Also create `tests/test_safe_path.py` with your own adversarial + positive tests.

## The threat matrix the resolver MUST reject

1. Parent traversal: `../etc/passwd`, `a/../../etc/passwd`, deeply nested `../`
2. Absolute paths: `/etc/passwd`, `/`
3. Symlink escape: a path that is (or passes through) a symlink pointing OUTSIDE base
4. Percent/URL-encoded traversal: `%2e%2e%2f`, `..%2f`, `%2e%2e/`
5. Backslash + mixed separators (treat `\` as a separator too): `..\..\secret`
6. Null byte / control-char truncation: `safe.txt\x00../../etc/passwd`
7. Trailing dot/space tricks: `secret. `, `..../` (dot-dot-dot-dot)
8. Current-dir noise that still escapes: `./a/./../../etc/passwd`
9. Empty / whitespace-only path
10. A path that normalizes to exactly `base` itself (allowed) vs one char outside (rejected)

## Must ALLOW (legitimate, must NOT raise)

- `report.pdf`, `sub/dir/file.txt`, `./nested/ok.md`, `a/b/../b/file.txt` (stays inside)
- A path resolving to a real file inside base via a symlink that stays INSIDE base

## Rules

- Reject by raising `UnsafePath` — never return an out-of-base path.
- Resolve real paths (handle symlinks); do not rely on string matching alone.
- No TODO comments, no stubs, no placeholder logic. Production-quality.
