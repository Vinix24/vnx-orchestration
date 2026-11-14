"""ADR-077 besluit 6: de kopie van de geheimpatronen blijft gelijk aan scripts/lib/secret_patterns.py."""
import importlib.util, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "besluitenregister"))
from besluitenregister.geheimpatronen import PATTERNS as KOPIE

def test_patronen_per_positie_gelijk():
    pad = ROOT / "scripts" / "lib" / "secret_patterns.py"
    assert pad.is_file(), f"{pad} ontbreekt: de gelijkheidstest slaat nooit over"
    spec = importlib.util.spec_from_file_location("secret_patterns_bron", pad)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    assert len(KOPIE) == len(mod.PATTERNS) == 10
    for k, b in zip(KOPIE, mod.PATTERNS):
        assert (k.name, k.regex.pattern, k.regex.flags) == (b.name, b.regex.pattern, b.regex.flags)
