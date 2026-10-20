import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Model, Row } from '../types'

import { collectModel, commandText, configOf, statusLine } from './collect'
import type { Config, Io } from './collect'
import { paneRows } from './view'

const PANE = 'voortgang'
const BUTTON_COLUMNS = 13

const model = atom({ plugin: 'voortgang', key: 'model' } as const, null)
const isRefreshing = atom({ plugin: 'voortgang', key: 'isRefreshing' } as const, false)

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
    const next = await collectModel(ioOf($), config, home, now)
    await update($, model, () => next)
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

const toneOf = (tone: Row['tone']) => {
  if (tone === 'head') {
    return { bold: true }
  }

  if (tone === 'dim') {
    return { dimColor: true }
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
    await $.command.register({
      name: 'voortgang',
      description: 'Toont de VNX-voortgang per project',
    })
    void refresh($, config)
    $.clock.every(config.refreshSeconds * 1000, () => {
      void refresh($, config)
    })

    return next(e)
  })

  on('command.run', { command: 'voortgang' }, async $ => {
    const latest = await refresh($, config)
    const opened = await $.ui.open({ id: PANE, title: 'Voortgang' })

    return { text: commandText(latest, opened.isPlaced ? null : opened.reason) }
  }).catch(() => ({ text: 'Voortgang: verversen mislukt, het paneel is niet geopend.' }))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const rows = e.props.scroll.bodyRows > 0 ? e.props.scroll.bodyRows : (e.viewport?.rows ?? 24)
    const view = paneRows(await read($, model), {
      columns: e.props.bodyColumns,
      headerColumns: Math.max(1, e.props.bodyColumns - BUTTON_COLUMNS),
      rows,
      isRefreshing: await read($, isRefreshing),
    })

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold>{view.header}</Text>
          <Button
            key="ververs"
            label="ververs"
            hotkey="r"
            onPress={() => {
              void refresh($, config)
            }}
          />
        </Box>
        {view.lines.map(line => (
          <Text {...toneOf(line.tone)}>{line.text}</Text>
        ))}
      </Box>
    )
  })
}
