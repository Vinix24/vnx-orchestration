import { expect, test } from 'claude-code/testing'
import type { On } from 'claude-code'

type World = { files: Record<string, string>; tokens: number | undefined; window: number }

const PROJECT = '/work/app'
const HOME = '/home/someone'
const SESSION = 'session-one'
const SETTINGS = `${PROJECT}/.claude/context-rotation.json`
const STATE = `${PROJECT}/.claude/rotation/state/${SESSION}.enforcement.json`
const BANDS = JSON.stringify({ warn_tokens: 1000, force_tokens: 2000, hard_tokens: 3000 })

const props = (columns: number) =>
  ({
    hasSurvey: false,
    isWorking: false,
    maxRows: 5,
    bodyColumns: columns,
    scroll: { offset: 0, bodyRows: 5 },
  }) as never

// The engine beneath the plugin: files, the session and its usage come from the world the test holds.
function engine(on: On, world: World) {
  on('fs.exists', (_, e) => ({ value: e.path in world.files }))
  on('fs.read', (_, e) => ({ value: world.files[e.path] ?? '' }))
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? HOME : undefined }))
  on('session.cwd', () => ({ value: PROJECT }))
  on('session.id', () => ({ value: SESSION }))
  on('session.usage', () => ({
    value: { startedAt: 0, context: { window: world.window, tokens: world.tokens }, rateLimits: [] },
  }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.measure', (_, e) => ({ changed: e.changed }))
  // What the engine draws in the band when no plugin does: nothing.
  on('ui.render', ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
}

// The band as drawn: its pieces joined, and the tone of each.
async function drawn($: any, columns = 120) {
  const ui = await $.ui.mount({
    plugin: 'context-meter',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: props(columns),
  })
  const pieces = await ui.findAll({ type: 'Text' })
  const texts: string[] = pieces.map((piece: any) => piece.text)
  const tones: (string | undefined)[] = pieces.map((piece: any) => piece.props.color)
  await ui.unmount()
  return { text: texts.join(''), tones, found: texts.length > 0 }
}

async function measured($: any, world: World, tokens: number | undefined) {
  world.tokens = tokens
  await $.session.measure({ context: { window: world.window, tokens }, rateLimits: [], changed: ['context'] })
}

async function start($: any) {
  await $.session.start({ cwd: PROJECT, surface: 'terminal', isInteractive: true })
}

test('below warn the line is green and names the next threshold', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS }, tokens: 500, window: 200000 }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text).toBe('context ▓░░░░░░░ 500 · ok · warn at 1K')
  expect(line.tones.every(tone => tone === 'success')).toBe(true)
})

test('warn is yellow and says what the guard wants', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS }, tokens: 1500, window: 200000 }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text).toBe('context ▓▓▓▓░░░░ 1.5K · warn: wrap up · force at 2K')
  expect(line.tones.every(tone => tone === 'warning')).toBe(true)
})

test('force is red, and the reading follows every session.measure', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS }, tokens: 500, window: 200000 }
  engine(on, world)
  await start($)
  await measured($, world, 2400)
  const line = await drawn($)
  expect(line.text).toBe('context ▓▓▓▓▓▓░░ 2.4K · force: write the handover · hard at 3K')
  expect(line.tones.every(tone => tone === 'error')).toBe(true)
})

test('hard is red and says only reads are allowed', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS }, tokens: 3500, window: 200000 }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text).toBe('context ▓▓▓▓▓▓▓▓ 3.5K · hard: only reads are allowed')
  expect(line.tones.every(tone => tone === 'error')).toBe(true)
})

test('no reading yet is dim text, never a zero', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS }, tokens: undefined, window: 200000 }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text).toBe('context: no reading yet')
  expect(line.text).not.toContain('0')
  expect(line.tones).toEqual([undefined])
})

test('enabled false shows the fill dim with rotation off and no band colour', async ($, on) => {
  const world: World = {
    files: { [SETTINGS]: JSON.stringify({ enabled: false, warn_tokens: 1000, force_tokens: 2000, hard_tokens: 3000 }) },
    tokens: 2500,
    window: 200000,
  }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text).toBe('context ▓▓▓▓▓▓░░ 2.5K · rotation off')
  expect(line.tones).toEqual([undefined, undefined])
})

test('show_meter false draws nothing', async ($, on) => {
  const world: World = {
    files: { [SETTINGS]: JSON.stringify({ show_meter: false, warn_tokens: 1000, force_tokens: 2000, hard_tokens: 3000 }) },
    tokens: 2500,
    window: 200000,
  }
  engine(on, world)
  await start($)
  expect((await drawn($)).found).toBe(false)
})

test('percentage bands on another window end in a warning', async ($, on) => {
  const world: World = { files: {}, tokens: 120000, window: 1000000 }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text).toBe('context ▓▓▓▓▓▓░░ 120K · warn: wrap up · force at 130K · window 1M, bands assume 200K')
  expect(line.tones[line.tones.length - 1]).toBe('warning')
})

test('a window smaller than hard_tokens is said too', async ($, on) => {
  const world: World = {
    files: { [SETTINGS]: JSON.stringify({ warn_tokens: 1000, force_tokens: 2000, hard_tokens: 600000 }) },
    tokens: 500,
    window: 200000,
  }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text.endsWith(' · hard 600K is above the 200K window')).toBe(true)
  expect(line.tones[line.tones.length - 1]).toBe('warning')
})

test('absolute bands ignore a window that differs', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS }, tokens: 500, window: 1000000 }
  engine(on, world)
  await start($)
  expect((await drawn($)).text).not.toContain('window')
})

test('after an injected handover force waits for min_work_tokens', async ($, on) => {
  const settings = JSON.stringify({ warn_tokens: 1000, force_tokens: 2000, hard_tokens: 3000, min_work_tokens: 500 })
  const state = JSON.stringify({ start_size: 1800 })
  const world: World = { files: { [SETTINGS]: settings, [STATE]: state }, tokens: 2100, window: 200000 }
  engine(on, world)
  await start($)
  const held = await drawn($)
  expect(held.text).toBe('context ▓▓▓▓▓░░░ 2.1K · warn: wrap up · force from 2.3K')
  expect(held.tones.every(tone => tone === 'warning')).toBe(true)

  await measured($, world, 2400)
  expect((await drawn($)).text).toBe('context ▓▓▓▓▓▓░░ 2.4K · force: write the handover · hard at 3K')
})

test('a held hard session says the same, never red', async ($, on) => {
  const settings = JSON.stringify({ warn_tokens: 1000, force_tokens: 2000, hard_tokens: 3000, min_work_tokens: 500 })
  const world: World = { files: { [SETTINGS]: settings, [STATE]: JSON.stringify({ start_size: 2900 }) }, tokens: 3100, window: 200000 }
  engine(on, world)
  await start($)
  const line = await drawn($)
  expect(line.text).toBe('context ▓▓▓▓▓▓▓▓ 3.1K · warn: wrap up · force from 3.4K')
  expect(line.tones.every(tone => tone === 'warning')).toBe(true)
})

test('a start size that is not a count is ignored', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS, [STATE]: JSON.stringify({ start_size: -4 }) }, tokens: 2100, window: 200000 }
  engine(on, world)
  await start($)
  expect((await drawn($)).text).toContain('force: write the handover')
})

test('a narrow width drops the trailing parts first and keeps the band word', async ($, on) => {
  const world: World = { files: { [SETTINGS]: BANDS }, tokens: 2400, window: 200000 }
  engine(on, world)
  await start($)
  expect((await drawn($, 50)).text).toBe('context ▓▓▓▓▓▓░░ 2.4K · force: write the handover')
  expect((await drawn($, 30)).text).toBe('context ▓▓▓▓▓▓░░ 2.4K · force')
  expect((await drawn($, 20)).text).toBe('context 2.4K · force')
})

test('the project file wins over the home file, and a bad project file shows the defaults', async ($, on) => {
  const world: World = {
    files: { [SETTINGS]: '{not json', [`${HOME}/.claude/context-rotation.json`]: BANDS },
    tokens: 2400,
    window: 200000,
  }
  engine(on, world)
  await start($)
  expect((await drawn($)).text).toBe('context ░░░░░░░░ 2.4K · ok · warn at 110K')
})
