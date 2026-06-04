# Task 08 — SSRF-safe URL validator + adversarial test suite

Source-inspiratie: SEOcrawler PR #100 (SSRF-safe async fetch — scoped-down to URL policy + DNS-resolution validator). Tier: T3 complex. Deadline: 2 hours wallclock.

## Context

Build a `URLPolicy` validator that rejects unsafe URLs BEFORE they hit any HTTP client. This is the security boundary that prevents SSRF attacks. Full PR #100 also has DNS-pinning + browser route-abort; this scoped task focuses on the validator + adversarial test suite — the part that requires adversarial reasoning, not just code-pattern.

## Threat model (the unsafe targets the validator must reject)

1. **Private IP ranges** — 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 (link-local), fc00::/7 (IPv6 ULA), ::1 (IPv6 loopback)
2. **Cloud metadata endpoints** — 169.254.169.254, fd00:ec2::254 (AWS IMDS)
3. **Non-HTTP schemes** — file://, gopher://, ftp://, javascript:, data:
4. **Localhost variants** — localhost, ip6-localhost, 0.0.0.0, [::]
5. **Decimal-encoded IPs** — http://2130706433/ (= 127.0.0.1)
6. **Hex-encoded IPs** — http://0x7f000001/
7. **DNS-resolves to private** — a public hostname whose A record points to RFC1918 (DNS-rebinding partial defense; validator must resolve + check)
8. **URL with no host** — http:///path
9. **CRLF injection** — http://example.com/\r\nHost:evil.com
10. **Userinfo evasion** — http://example.com@127.0.0.1/

## Required deliverables

### 1. `url_policy.py`

```python
class URLPolicy:
    def validate(self, url: str) -> None:
        """Raises URLPolicyViolation with a clear reason if unsafe.

        Two-step:
        1. Lexical check (scheme, hostname, encoded-IP)
        2. DNS resolution + IP range check
        """

class URLPolicyViolation(Exception):
    """Raised with a reason field."""
    def __init__(self, reason: str, url: str): ...
```

### 2. `tests/test_url_policy.py`

10 adversarial tests covering each threat above. Each test:
- Provides a malicious URL
- Asserts `URLPolicyViolation` is raised
- Asserts the `reason` field contains an identifying token

Plus 3 positive tests:
- `test_validate_public_http_url_allowed` — https://example.com/
- `test_validate_public_with_path_query_allowed` — https://api.example.com/v1/data?q=1
- `test_validate_subdomain_allowed` — https://blog.example.com/

### 3. DNS resolution behavior

The DNS-resolves-to-private test (#7) uses a real hostname that you can mock OR a deterministic test fixture. Recommended: use `socket.getaddrinfo` and mock it in the test via `monkeypatch`. The validator itself must call `socket.getaddrinfo` (do not short-circuit when the test runs).

## Definition of done

- All 13 tests pass: `pytest tests/test_url_policy.py -v`
- 10 adversarial URLs all raise `URLPolicyViolation` with descriptive `reason`
- 3 public URLs pass cleanly
- Validator does NOT make any HTTP request itself (purely policy + DNS-resolve)
- No bare except clauses
