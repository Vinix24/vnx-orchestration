"""local_gemma — Local Gemma e4b via MLX with Ollama fallback.

Pip-installable submodule: vnx-orchestration[local-gemma]
"""
from .spawn import spawn_local_gemma, LocalGemmaSpawnResult

__all__ = ["spawn_local_gemma", "LocalGemmaSpawnResult"]
