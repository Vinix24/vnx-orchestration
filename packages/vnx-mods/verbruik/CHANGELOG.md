# Changelog

All notable changes to this plugin are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the plugin follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

The first version. One status line with the usage of every subscription and the balance of the API providers, so none of them has to be looked up with a command.

### Added

- Claude: the 5-hour and the weekly percentage from `$.session.usage().rateLimits`, read at `session.start` and at every `session.measure`. Without a reading the line says `claude: nog geen meting`, never 0.
- The other providers from `vnx providers usage --json`, run at the start and every `refreshMinutes` (default 10, 2 at least) with a timeout and one run at a time: codex as its percentage, kimi as its status, deepseek and openrouter as their balance in their currency. A row with `available: false` shows the provider and a short reason.
- A failing run, invalid JSON or another `schema` keeps the last good figures marked `(oud)` and names the cause once. An older vnx without `providers usage` is recognised by the refusal of the command itself and shows `vnx te oud voor providers usage` next to the Claude part.
- Tones on `warnPercent`, `errorPercent`, `warnBalance` and `errorBalance`. The status line takes no style, so a warning ends in `!` and an error in `!!`.
- A fit to the terminal width: openrouter, deepseek, kimi, codex and the weekly Claude figure drop in that order. The Claude 5-hour figure stays.
- `/verbruik`: the full figures as text lines and a refresh.
