#!/usr/bin/env python3
"""OI-1675: reproducible measurement of outbound TCP-connection ATTEMPTS.

Round 1 of this OI (`20260908-oi1675-testmodus-weigert-posten`) could not
reproduce the 51/102 figures `tests/conftest.py::_stub_forge_check_run_post`'s
docstring quotes for #1815: the METHOD that produced them lives nowhere in
that PR's diff, only the result in a docstring, presumably ad hoc
(`lsof`/network monitoring). This script is the method, checked in, so the
next time this number needs re-measuring it does not depend on someone's
shell history.

Two modes:

1. No arguments — the built-in OI-1675 demonstration. Calls
   ``forge_check_run.publish_check_run`` directly (the exact shape of the gap
   this OI closes: a test file calling the client's own function without
   mocking ``_api_request``) twice: once with the new test-mode guard
   (``_refuse_test_post_without_opt_in``) neutralized, once with it active.
   Prints the outbound-connection-ATTEMPT count for both.

2. ``--pytest-target <arg> [<arg> ...]`` — wraps an actual ``pytest.main()``
   run of the given target(s) in the same socket-level spy and reports the
   TOTAL outbound-connection attempts observed, from whatever made them. Point
   this at any test file or directory — e.g. the 21 recorder-driving files
   that ``_stub_forge_check_run_post``'s docstring counted by hand — to redo
   that measurement with a checked-in method instead of a fresh ad hoc one.

Method: ``socket.socket.connect`` is monkeypatched at the class level
(process-wide, for the duration of a single measurement) to record every
attempted ``(host, port)`` and raise BEFORE a real TCP handshake — nothing
ever reaches the wire. DNS resolution is stubbed to a TEST-NET-3 address
(RFC 5737, 203.0.113.0/24, never routable) so the measurement needs no network
access at all and does not depend on what a hostname happens to resolve to on
a given day.

BILLING SAFETY: No Anthropic SDK. No direct API calls to api.anthropic.com.
This script performs ZERO real network I/O by construction (see "Method").
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Sequence, Tuple
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import forge_check_run as fcr  # noqa: E402

#: The guard under measurement only engages when it detects a pytest run
#: (``PYTEST_CURRENT_TEST`` or ``"pytest" in sys.modules`` — see
#: ``forge_check_run._refuse_test_post_without_opt_in``). This script is
#: invoked as a plain script, not via pytest, so without this import the
#: "guard active" measurement below would silently take the "not under
#: pytest" no-op branch and never demonstrate anything. Importing here puts
#: this process in the same state a real pytest worker is already in.
import pytest  # noqa: E402,F401

#: RFC 5737 TEST-NET-3 — reserved for documentation/testing, guaranteed to
#: never route to a real host. Every stubbed DNS lookup resolves here.
_TEST_NET_ADDRESS = "203.0.113.1"


@contextmanager
def spy_on_socket_connect() -> Iterator[List[Tuple[str, int]]]:
    """Record every ``socket.socket.connect()`` attempt; block each before a
    real TCP handshake completes.

    This is the reusable primitive: import it into any test or script that
    needs to prove "no outbound connection happened" without relying on the
    machine's actual network reachability.
    """
    calls: List[Tuple[str, int]] = []

    def spying_connect(self, address, *args, **kwargs):  # noqa: ANN001
        try:
            host, port = address[0], address[1]
        except (TypeError, IndexError):
            host, port = str(address), -1
        calls.append((host, port))
        raise OSError(
            f"[meetharness OI-1675] geblokkeerd vóór een echte TCP-handshake: "
            f"poging naar {host}:{port}"
        )

    with mock.patch.object(socket.socket, "connect", spying_connect):
        yield calls


@contextmanager
def _stub_dns_resolution() -> Iterator[None]:
    """Resolve every hostname to the TEST-NET-3 address instead of doing a
    real DNS lookup, so this measurement sends no query for api.github.com
    and does not depend on network access at all."""

    def fake_getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001
        resolved_port = port if isinstance(port, int) else 443
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_TEST_NET_ADDRESS, resolved_port))]

    with mock.patch.object(socket, "getaddrinfo", fake_getaddrinfo):
        yield


def _attempt_publish_check_run(*, guard_active: bool) -> List[Tuple[str, int]]:
    """One direct call to ``publish_check_run``, mocking ONLY the credential
    chain (no real keychain read needed). ``_api_request`` and everything
    below it — ``urllib``, ``http.client``, the socket — stays REAL.

    ``guard_active=False`` neutralizes ``_refuse_test_post_without_opt_in``
    for the duration of this one call, simulating "this guard does not exist"
    without editing the source file — the exact before/after this measurement
    exists to make.
    """
    patches = [
        mock.patch.object(
            fcr, "_installation_token_with_freshness", return_value=("ghs_measurement", True)
        ),
        mock.patch.object(
            fcr, "resolve_owner_repo", return_value="Vinix24/vnx-orchestration"
        ),
    ]
    if not guard_active and hasattr(fcr, "_refuse_test_post_without_opt_in"):
        patches.append(
            mock.patch.object(fcr, "_refuse_test_post_without_opt_in", lambda *a, **k: None)
        )

    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(_stub_dns_resolution())
        calls = stack.enter_context(spy_on_socket_connect())
        try:
            fcr.publish_check_run("a" * 40, "vnx-gate/review", "success", "OI-1675 measurement")
        except fcr.ForgeCheckRunError:
            # guard_active=True: ForgeTestPostRefused fired before any network
            # code ran (0 attempts expected).
            # guard_active=False: the spy blocked the real attempt and
            # _api_request wrapped it as ForgeAPIError (1+ attempts expected).
            pass
    return calls


def _run_builtin_demonstration() -> int:
    before = _attempt_publish_check_run(guard_active=False)
    after = _attempt_publish_check_run(guard_active=True)

    print("=== OI-1675: uitgaande-verbindingen-meting, forge_check_run.publish_check_run ===")
    print(
        f"VOOR  (guard uitgeschakeld): {len(before)} poging(en)"
        + (f" -> {before}" if before else "")
    )
    print(
        f"ERNA  (guard actief):       {len(after)} poging(en)"
        + (f" -> {after}" if after else "")
    )

    if before and not after:
        print("Resultaat: de guard voorkomt de gemeten uitgaande verbinding.")
        return 0
    if not before:
        print(
            "WAARSCHUWING: geen poging gemeten zonder guard — de meetmethode zelf "
            "raakte het netwerk niet; controleer dit script vóór je op 'guard werkt' vertrouwt.",
            file=sys.stderr,
        )
        return 1
    if after:
        print("FAIL: de guard is actief maar er is toch een poging gemeten.", file=sys.stderr)
        return 1
    return 0


def _run_pytest_target(target: Sequence[str]) -> int:
    with _stub_dns_resolution(), spy_on_socket_connect() as calls:
        exit_code = pytest.main(list(target))

    print(f"=== OI-1675: uitgaande-verbindingen-meting over {list(target)} ===")
    if not calls:
        print("0 uitgaande verbindingspogingen.")
    else:
        for host, port in calls:
            print(f"poging naar {host}:{port}")
        print(f"TOTAAL: {len(calls)} uitgaande verbindingspoging(en)")
    return exit_code


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pytest-target",
        nargs=argparse.REMAINDER,
        metavar="ARG",
        help=(
            "draai deze pytest-argumenten onder de socket-spy i.p.v. de ingebouwde "
            "demonstratie (neemt alle resterende argumenten over, ook vlaggen zoals -q)"
        ),
    )
    args = parser.parse_args(argv)

    if args.pytest_target:
        return _run_pytest_target(args.pytest_target)
    return _run_builtin_demonstration()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
