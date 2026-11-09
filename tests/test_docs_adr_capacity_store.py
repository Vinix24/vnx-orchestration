"""Pin the shared host-capacity contract for rekenslot RS-0 through RS-11."""
from __future__ import annotations

import re
from pathlib import Path

DECISIONS_DIR = Path(__file__).resolve().parent.parent / "docs/governance/decisions"


def test_capacity_store_adr_contract():
    paths = sorted(DECISIONS_DIR.glob("ADR-*-capaciteitsstore.md"))
    assert len(paths) == 1, f"expected exactly one capacity-store ADR, found {paths}"
    path = paths[0]
    text = path.read_text(encoding="utf-8")
    assert re.findall(r"^# ADR-(\d+)\b", text) == [path.name.split("-")[1]]
    assert "veilige reeks" in text and not re.search(r"strikt[e]? FIFO", text, re.I)
    assert "--owner-pid" in text
    assert "parent_lease" in text
    assert "os.fchmod(fd, 0o660)" in text
    assert "vnxslot" in text
    assert "/Users/Shared/vnx-slots" in text
    assert "| 0 | Slotbewerking geslaagd;" in text
    assert "| 75 | Tijdelijke capaciteitsweigering" in text
    assert "| 78 | Ongeldige configuratie" in text
    assert "enforce_pending" in text
    assert "RS-1 is gedaan; het gemeten slotgetal en zijn meetdocument staan in het beleid" in text
    assert "doctor geeft exit 0 als vincentvandeth én als ci-runner" in text
    assert "minstens 7 dagen observe met zware aanvragen uit minstens twee projecten" in text
    assert "een besluit van de operator met datum en verwijzing" in text
    assert "Een proces dat zelf een nieuwe sessie opent ontsnapt aan de telling." in text
    assert "Een afstammeling van een losse acquire die naar launchd verhuist voordat hij gezien is, ontsnapt." in text
    assert "Een lid van vnxslot kan de toestand beschadigen." in text
    assert "ADR-026" in text
    assert "ADR-007" in text
    assert re.findall(r"^### (\d+)\. ", text, re.M) == [str(n) for n in range(1, 15)]
    assert "Een corrupte toestand is een weigering (78), nooit `off`." in text
