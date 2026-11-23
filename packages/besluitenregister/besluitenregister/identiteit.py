"""Ids van besluit/1 (ADR-077 besluit 2). Canonieke vorm: gesorteerde sleutels, compacte scheiders."""
import hashlib, json, uuid

def canoniek(waarde):
    return json.dumps(waarde, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _sha(tekst):
    return hashlib.sha256(tekst.encode("utf-8")).hexdigest()

def event_id(regel):
    return "E-" + _sha(canoniek({k: v for k, v in regel.items() if k not in ("event_id", "vastgelegd_op")}))

def herkomst(context, bron_soort, bron_ref, titel):
    if bron_soort == "daily-log":  # de ref (`<log>#<kop>/<volgnummer>`) is de positie; de tekst mag veranderen en botst dan
        return _sha(canoniek([context, bron_soort, bron_ref]))
    return _sha(canoniek([context, bron_soort, bron_ref, titel]))

def nieuw_besluit_id(besloten_op, bestaande=()):
    """`B-<JJJJMMDD>-<8 hex uuid4>`; trekt opnieuw als het id al bestaat."""
    while (kandidaat := f"B-{besloten_op[:10].replace('-', '')}-{uuid.uuid4().hex[:8]}") in bestaande:
        pass
    return kandidaat
