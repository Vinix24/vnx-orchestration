import type { EngineInterface, Register } from 'claude-code'

import type { Config, Fleet, RateLimit } from '../types'

import { NEVER_RAN, advance, commandText, configOf, fit, outcomeOf, segmentsOf } from './usage'

const RUN_TIMEOUT_MS = 25_000
const MINUTE_MS = 60_000

// The figures between hooks. A reload starts them over, and `session.start` fills them again.
let rateLimits: readonly RateLimit[] = []
let fleet: Fleet = NEVER_RAN
// The width the engine last told a render hook; null until it has. Without it nothing is dropped.
let columns: number | null = null
// Joins a run asked while one is going: one run at a time.
let running: Promise<void> | null = null

function redraw($: EngineInterface, config: Config): void {
  $.ui.status(fit(segmentsOf(rateLimits, fleet, config), columns))
}

async function binOf($: EngineInterface, config: Config): Promise<string> {
  if (!config.vnxBin.startsWith('~')) {
    return config.vnxBin
  }

  const home = await $.env.get('HOME')

  return home === undefined || home === '' ? config.vnxBin : `${home}${config.vnxBin.slice(1)}`
}

async function measure($: EngineInterface, config: Config): Promise<void> {
  try {
    const run = await $.process.run([await binOf($, config), 'providers', 'usage', '--json'], { timeoutMs: RUN_TIMEOUT_MS })

    fleet = advance(fleet, outcomeOf(run))
  } catch {
    fleet = advance(fleet, { kind: 'failed', cause: 'vnx niet uitgevoerd' })
  } finally {
    running = null
  }

  redraw($, config)
}

function refresh($: EngineInterface, config: Config): Promise<void> {
  if (running === null) {
    running = measure($, config)
  }

  return running
}

export const register: Register = (on, options) => {
  const config = configOf(options)

  on('session.start', async ($, e, next) => {
    rateLimits = (await $.session.usage()).rateLimits
    redraw($, config)
    await $.command.register({ name: 'verbruik', description: 'Toont het verbruik van elke provider en ververst de meting' })
    void refresh($, config)
    $.clock.every(config.refreshMinutes * MINUTE_MS, () => {
      void refresh($, config)
    })

    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    rateLimits = e.rateLimits
    redraw($, config)

    return next(e)
  })

  // The status line is given no width. The prompt hint is drawn on every width change and carries it; this hook draws nothing.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const width = e.viewport?.columns ?? null

    if (width !== columns) {
      columns = width
      redraw($, config)
    }

    return next(e)
  })

  on('command.run', { command: 'verbruik' }, async $ => {
    rateLimits = (await $.session.usage()).rateLimits
    await refresh($, config)

    return { text: commandText(rateLimits, fleet, config) }
  })
}
