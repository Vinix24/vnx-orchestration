# Changelog

All notable changes to this plugin are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the plugin follows [Semantic Versioning](https://semver.org/).

## [0.1.2] - 2026-10-22

### Changed

- A full kimi with a known upper bound shows the time left instead of only the date: `kimi terug over 1d 19u (uiterlijk 24-10 13:05) !!`. Above 24 hours it is days and hours (`1d 19u`), from 1 to 24 hours hours and minutes (`5u 07m`), under an hour minutes (`42m`). It is rounded up on the smallest unit shown, so the text never promises kimi back too early.
- At or after the bound the text no longer says kimi is full: the `vol` status was written before the bound and is no new measurement. It reads `kimi laatst vol, grens 24-10 13:05 voorbij !` (tone `warning`) and names only the stored bound; no new time is computed.
- Without a known bound the text stays `kimi vol tot reset !!`.
- The display reads the time from the engine's clock (`$.clock.now()`) and is computed again on every render. There is no ticker and no new timer.

## [0.1.1] - 2026-10-22

### Changed

- Kimi at its weekly limit shows `kimi reset uiterlijk <dd-mm HH:MM>` (local time) instead of `kimi vol tot reset` when `vnx providers usage --json` carries `resets_at_latest`. It is an upper bound (the first weekly-limit failure of the current streak plus 7 days), never the exact reset, which the kimi CLI does not name. Without a known bound the text stays `kimi vol tot reset`. The `!!` marker is unchanged.

## [0.1.0] - 2026-10-21

The first version. One status line with the usage of every subscription and the balance of the API providers, so none of them has to be looked up with a command.

### Added

- Claude: the 5-hour and the weekly percentage from `$.session.usage().rateLimits`, read at `session.start` and at every `session.measure`. Without a reading the line says `claude: nog geen meting`, never 0.
- The other providers from `vnx providers usage --json`, run at the start and every `refreshMinutes` (default 10, 2 at least) with a timeout and one run at a time: codex as its percentage, kimi as its status, deepseek and openrouter as their balance in their currency. A row with `available: false` shows the provider and a short reason.
- A failing run, invalid JSON or another `schema` keeps the last good figures marked `(oud)` and names the cause once. An older vnx without `providers usage` is recognised by the refusal of the command itself and shows `vnx te oud voor providers usage` next to the Claude part.
- Tones on `warnPercent`, `errorPercent`, `warnBalance` and `errorBalance`. The status line takes no style, so a warning ends in `!` and an error in `!!`.
- A fit to the terminal width: openrouter, deepseek, kimi, codex and the weekly Claude figure drop in that order. The Claude 5-hour figure stays.
- `/verbruik`: the full figures as text lines and a refresh.
