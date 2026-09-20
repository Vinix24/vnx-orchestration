# Intelligence A/B-ontwerp: leert het framework bij?

> Dispatch 20260920-171500-intel-lus, PUNT 3b. Dit is een **ontwerp**, niet een
> draaiend experiment. `VNX_INTEL_AB_TEST` bestaat als vlag en staat uit. Niets
> in dit document mag worden uitgevoerd zonder T0-goedkeuring na de merge.

## De twee vragen, gescheiden

**(a) ADOPTIE** is nu meetbaar dankzij `pattern_injection_outcome`:
adoptiegraad = `used=1 / offered`. Dat staat in
`scripts/intel_injection_join.py adoption`. Adoptie zegt **of een patroon in
het rapport opdukt**, niet of het werk beter wordt.

**(b) EFFECT** is nog niet meetbaar. Adoptie is geen kwaliteit. Een patroon
kan worden overgenomen en het resultaat toch verslechteren, of worden
genegeerd terwijl het werk prima slaagt. Effect vraagt een andere maat.

## Wat het experiment zou moeten antwoorden

De enige vraag die telt: **verlaagt het injecteren van intelligence-patronen
het rework en het falen van dispatches?**

## Ontwerp

### Uitkomstmaat (primary endpoint)

Rework rate per dispatch, gelezen uit de bestaande governance-grootboek
receipts: een dispatch is "rework" als er binnen 14 dagen een vervolg-dispatch
op dezelfde track ontstaat met `parent_dispatch` naar de oorspronkelijke
dispatch, of als de receipt `status != success` draagt.

Secundaire uitkomstmaat: First-Pass Yield (FPY) = dispatches die in één keer
slagen zonder rework, gedeeld door totaal. Let op: FPY en rework rate zijn
elkaars spiegel; rapporteer er een, niet beide, om dubbel tellen te voorkomen.

### Arms

- **Control (arm 0):** `VNX_INTEL_AB_TEST=1` met forced **suppress** —
  intelligence_injections wordt geschreven met `items_injected=0` en alle
  patronen in `suppressed_json`. De dispatch-instructie bevat geen patterns.
- **Treatment (arm 1):** `VNX_INTEL_AB_TEST=1` met forced **inject** —
  patronen worden zoals normaal geinjecteerd.

Beide armen schrijven nog steeds `dispatch_pattern_offered` en
`pattern_injection_outcome` rijen, zodat adoptie naast effect meetbaar
blijft. De vlag bepaalt alleen of het patroon in de instructie landt, niet
of het wordt aangeboden in de store.

### Randomisatie

Per dispatch, gehashed op `dispatch_id` modulo 2. Dit voorkomt operator-bias
en houdt de armen binnen één track gebalanceerd. De hash moet deterministisch
zijn en vastgelegd in `intelligence_injections.ab_arm` (al in het schema
aanwezig via het A/B-framework) zodat de toewijzing auditeerbaar is.

### Sample size

Minimaal **200 dispatches per arm**. Ondergrens: met een baseline rework rate
van ~30% (gemeten op de huidige vloot) en een verwacht effect van 5 procentpunt
reductie is n=200 per arm voldoende om een verschil met 80% power op alpha=0,05
te detecteren. Bij minder dan 200 per arm is het experiment onderpowered en
levert het geen uitspraak.

### Welk verschil telt

Een verschil van **5 procentpunt rework rate** ten gunste van treatment.
Kleiner dan dat is het niet het waard om de vlag structureel aan te zetten.
Het drempelgetal staat open voor T0-adjustering na een pilot, maar de
drempel moet **vaststaan voordat** de meting loopt, niet erna.

### Matching

Vergelijk armen gematched op `role + task_class + week`, identiek aan het
bestaande `intelligence_ab_report.py`-patroon. Dit voorkomt dat een
specifieke rol of een drukke week het beeld scheeftrekt.

## Falsificatie — wat zou bewijzen dat het NIET bijleert

Een metriek die alleen kan stijgen meet niets. Drie concrete uitkomsten die
het framework falsifiëren:

1. **Adoptiegraad stijgt, rework rate daalt niet.** Als `used=1 / offered`
   omhoog gaat terwijl de rework rate in hetzelfde venster niet daalt (of
   stijgt), dan neemt de adoptie toe zonder effect. Het framework "leert"
   patronen te injecteren die worden overgenomen, maar die overname maakt het
   werk niet beter. Dit is de sterkste falsificatie.

2. **Adoptiegraad stijgt door verwatering.** Als patronen generieker worden
   (kortere, bredere content met meer token-overlap met elk rapport), stijgt
   `used=1` mechanisch zonder dat er echt wordt geleerd. Toets hiervoor: de
   gemiddelde patroon-lengte (`LENGTH(content)`) per patroon over tijd. Stijgt
   de adoptiegraad terwijl de gemiddelde lengte daalt, is het geen leren maar
   verwatering. De adoptiegraad moet worden gecorrigeerd voor content-lengte,
   of de overlap-drempel moet omhoog wanneer patronen korter worden.

3. **Control-arm presteert beter dan treatment.** Als de rework rate in de
   control-arm (geen injectie) lager is dan in de treatment-arm, dan doen de
   geinjecteerde patronen actief schade. Dat is geen "geen effect", dat is
   een tegen-effect en direct bewijs dat het framework niet bijleert.

De falsificatie is verplicht: een metriek die alleen kan stijgen (adoptiegraad
zonder rework-daling) is geen bewijs van leren. Het ontwerp is pas geldig als
alle drie deze uitkomsten tot afwijzing van de vlag leiden.

## Wat dit ontwerp NIET doet

- Het draait niet. Geen van de armen wordt in deze dispatch uitgevoerd.
- Het raakt de 192.495 snippets, `dream` of de andere vier vlaggen niet.
- Het zet `VNX_INTEL_AB_TEST` niet aan. Dat is T0-werk na de merge.

## Cross-references

- Adoptie-metriek script: `scripts/intel_injection_join.py adoption`
- Join-script: `scripts/intel_injection_join.py join`
- Bestaand A/B-rapport (success-rate, niet rework): `scripts/intelligence_ab_report.py`
- A/B-framework flag: `VNX_INTEL_AB_TEST` (`scripts/lib/config_registry.py`)
- Schrijver (nu werkend na tz-fix): `scripts/gather_intelligence.py::_record_one_injection_outcome`
