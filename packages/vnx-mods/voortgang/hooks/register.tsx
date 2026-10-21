import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Model, Tone } from '../types'

import { collectModel, commandText, configOf, messageOf, statusLine } from './collect'
import type { Config, Io } from './collect'
import { paneRows } from './view'

const PANE = 'voortgang'
const BUTTON_COLUMNS = 13

const model = atom({ plugin: 'voortgang', key: 'model' } as const, null)
const isRefreshing = atom({ plugin: 'voortgang', key: 'isRefreshing' } as const, false)
const failure = atom({ plugin: 'voortgang', key: 'failure' } as const, null)
// The working directory of the session, learned at its start: the project is the one above it.
const cwd = atom({ plugin: 'voortgang', key: 'cwd' } as const, null)
// The view the pane draws: what is going on now, or the plan. `/voortgang plan` sets it, `/voortgang` sets it back.
const paneView = atom({ plugin: 'voortgang', key: 'view' } as const, 'now')

// Joins a refresh asked while one runs (the timer, the button, the command). Never drawn from:
// a reload starts it over at null, which only means the next ask starts a fresh refresh.
let running: Promise<Model> | null = null

function ioOf($: EngineInterface): Io {
  return {
    list: async path => (await $.fs.list(path)).map(entry => entry.name),
    exists: path => $.fs.exists(path),
    read: path => $.fs.read(path),
    run: (argv, init) => $.process.run(argv, init),
  }
}

async function collectInto($: EngineInterface, config: Config): Promise<Model> {
  await update($, isRefreshing, () => true)

  try {
    const home = await $.env.get('HOME')
    const now = await $.clock.now()
    const next = await collectModel(ioOf($), config, home, await read($, cwd), now)
    await update($, model, () => next)
    await update($, failure, () => null)
    $.ui.status(statusLine(next))

    return next
  } finally {
    await update($, isRefreshing, () => false)
    running = null
  }
}

function refresh($: EngineInterface, config: Config): Promise<Model> {
  if (running === null) {
    running = collectInto($, config)
  }

  return running
}

/** A refresh that rejected is shown in the status line and in the pane; the model of the last good refresh stays. */
async function showFailure($: EngineInterface, error: unknown): Promise<void> {
  const reason = messageOf(error)

  await update($, failure, () => reason)
  $.ui.status(`voortgang · verversen mislukt: ${reason}`)
}

/** A refresh nobody awaits: the timer, the button and the start of a session. */
async function refreshShown($: EngineInterface, config: Config): Promise<void> {
  try {
    await refresh($, config)
  } catch (error) {
    await showFailure($, error)
  }
}

const toneOf = (tone: Tone) => {
  if (tone === 'head') {
    return { bold: true }
  }

  if (tone === 'dim') {
    return { dimColor: true }
  }

  if (tone === 'ok') {
    return { color: 'success' }
  }

  if (tone === 'warn') {
    return { color: 'warning' }
  }

  if (tone === 'error') {
    return { color: 'error' }
  }

  return {}
}

export const register: Register = (on, options) => {
  const config = configOf(options)

  on('session.start', async ($, e, next) => {
    await update($, cwd, () => e.cwd)
    await $.command.register({
      name: 'voortgang',
      description: 'Toont de voortgang van het VNX-project van deze sessie; met het argument plan het plan van next en later',
    })
    void refreshShown($, config)
    $.clock.every(config.refreshSeconds * 1000, () => {
      void refreshShown($, config)
    })

    return next(e)
  })

  on('command.run', { command: 'voortgang' }, async ($, e) => {
    const argument = e.args.trim()

    if (argument !== '' && argument !== 'plan') {
      return { text: `Voortgang: onbekend argument '${argument}'. Gebruik /voortgang of /voortgang plan.` }
    }

    await update($, paneView, () => (argument === 'plan' ? 'plan' : 'now'))

    let latest: Model

    try {
      latest = await refresh($, config)
    } catch (error) {
      await showFailure($, error)

      return { text: `Voortgang: verversen mislukt (${messageOf(error)}), het paneel is niet geopend.` }
    }

    const opened = await $.ui.open({ id: PANE, title: 'Voortgang' })

    return { text: commandText(latest, opened.isPlaced ? null : opened.reason) }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const rows = e.props.scroll.bodyRows > 0 ? e.props.scroll.bodyRows : (e.viewport?.rows ?? 24)
    const view = paneRows(await read($, model), {
      columns: e.props.bodyColumns,
      headerColumns: Math.max(1, e.props.bodyColumns - BUTTON_COLUMNS),
      rows,
      isRefreshing: await read($, isRefreshing),
      failure: await read($, failure),
      view: await read($, paneView),
    })

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold {...toneOf(view.headerTone)}>{view.header}</Text>
          <Button
            key="ververs"
            label="ververs"
            hotkey="r"
            onPress={() => {
              void refreshShown($, config)
            }}
          />
        </Box>
        {view.lines.map(line => (
          <Text>
            {line.segments.filter(part => part.text !== '').map(part => (
              <Text {...toneOf(part.tone)}>{part.text}</Text>
            ))}
          </Text>
        ))}
      </Box>
    )
  })
}
