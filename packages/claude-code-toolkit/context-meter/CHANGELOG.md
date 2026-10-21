# Changelog

All notable changes to this plugin are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the plugin follows [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-21

### Added

- A line above the prompt: an 8-cell bar, the fill in tokens, the band and what the `context-rotation` guard does there, and the next threshold. Green below warn, yellow from warn, red from force. At hard the text says only reads are allowed.
- The figure from `$.session.usage().context` at session start and on every `session.measure`. No reading yet is shown dim and never as 0.
- The bands from the guard's own settings, ported from `context_budget.py`: the project file, else the user file in the home directory, else the defaults; bad keys fall back one by one; absolute tokens win over percentages. A fixture of 43 settings cases and 14 min-work cases is read by both the TypeScript test and the guard's pytest.
- `enabled: false` shows the fill dim with `rotation off`. `show_meter: false` hides the line.
- A yellow note when the bands are percentages and the session's window differs from `window_tokens`, and when `hard_tokens` is above the window.
- After an injected handover, force and hard wait for `min_work_tokens`: the band stays yellow at most and says `force from <start + min_work_tokens>`.
- Narrow terminals drop the trailing parts first and never the band word.
