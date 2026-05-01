"""subprocess_dispatch_internals — implementation modules for subprocess_dispatch.

The public API lives in scripts/lib/subprocess_dispatch.py (the facade).
Modules in this package contain the helpers extracted from the original
monolithic subprocess_dispatch.py.  External callers should import from
``subprocess_dispatch``, not from this package directly.
"""
