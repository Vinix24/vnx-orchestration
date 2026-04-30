"""Provider abstraction for the receipt classifier.

Providers wrap CLI calls (claude, ollama, gemini, codex) and return a uniform
result dict. No Anthropic SDK; subprocess only.
"""

from .base import ClassifierProvider, ClassifierResult, get_provider

__all__ = ["ClassifierProvider", "ClassifierResult", "get_provider"]
