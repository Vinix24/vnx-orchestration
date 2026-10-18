- Review gates (R4, operator decision 2026-10-17): `glm_gate` leaves the default takeover chain, which is now `codex_gate,kimi_gate,deepseek_gate` (`VNX_REVIEW_GATE_TAKEOVER_CHAIN` and `gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN`).
  glm_gate stays a registered gate and an explicit chain or stack can still name it.
  With codex and kimi both out, deepseek reads one seat and the other ends as `chain_exhausted`: no merge until a subscription reviewer is back.
