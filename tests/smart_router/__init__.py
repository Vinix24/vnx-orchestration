"""Package shim — merges scripts/lib/smart_router.py into this test package namespace.

pytest treats tests/smart_router/ as the 'smart_router' package (because __init__.py
exists). When classifier.py does `from smart_router import ROLE_TO_TASK_CLASS`, Python
looks in this package and finds nothing — unless we load the real module and merge its
public API here. This shim does that without replacing sys.modules['smart_router'].
"""
import importlib.util
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

# Load the real smart_router.py, registering it under a private key so that the
# dataclass decorator can resolve cls.__module__ via sys.modules.
_KEY = "_smart_router_impl"
if _KEY not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_KEY, _LIB / "smart_router.py")
    _impl = importlib.util.module_from_spec(_spec)
    sys.modules[_KEY] = _impl  # register before exec so @dataclass resolves __module__
    _spec.loader.exec_module(_impl)
else:
    _impl = sys.modules[_KEY]

# Merge public API into this package so `from smart_router import X` resolves here.
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
