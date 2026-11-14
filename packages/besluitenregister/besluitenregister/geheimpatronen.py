"""Kopie van PATTERNS uit scripts/lib/secret_patterns.py (ADR-077 besluit 6): zelfde namen, volgorde, regex en flags.
De gelijkheidstest bewaakt de kopie; een patroon wijzigen gebeurt in beide lijsten in dezelfde PR."""
import re
from collections import namedtuple

Patroon = namedtuple("Patroon", "name regex")

PATTERNS = [
    Patroon('secret_assignment', re.compile('([\\w-]*(?:KEY|TOKEN|SECRET|PASSWORD)[\\w-]*=)(?!<secret>)(?=\\S)', re.IGNORECASE)),
    Patroon('sk_key', re.compile('\\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}')),
    Patroon('github_token', re.compile('\\bgh[pousr]_[A-Za-z0-9]{20,}')),
    Patroon('aws_key_id', re.compile('\\b(?:AKIA|ASIA)[A-Z0-9]{16}\\b')),
    Patroon('slack_token', re.compile('\\bxox[abprs]-[A-Za-z0-9-]{10,}')),
    Patroon('bearer', re.compile('\\bBearer\\s+[A-Za-z0-9._~+/=-]{8,}')),
    Patroon('authorization_header', re.compile('\\bAuthorization:[^\\r\\n]*', re.IGNORECASE)),
    Patroon('pem_block', re.compile('-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\\Z)', re.DOTALL)),
    Patroon('jwt', re.compile('\\beyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+')),
    Patroon('long_value', re.compile('(=)(?!<secret>)(?=(?:\\$?[\\"\'])?[A-Za-z0-9_+/=-]{40,})|(\\b[A-Za-z][\\w-]*:[ \\t]+)(?!<secret>)(?=[A-Za-z0-9_+/=-]{40,})\\S+')),
]
