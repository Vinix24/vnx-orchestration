# Compat shim -> unified_state_manager.py; remove in 1.0.1
from unified_state_manager import *

if __name__ == "__main__":
    import runpy
    runpy.run_module("unified_state_manager", run_name="__main__", alter_sys=True)
