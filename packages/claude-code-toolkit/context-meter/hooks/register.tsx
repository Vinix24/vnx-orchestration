import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { MeterView } from '../types'
import { SETTINGS_FILE, fit, pieces, resolveSettings, startSizeOf, viewOf, type Source } from './meter'

const view = atom({ plugin: 'context-meter', key: 'view' } as const, null as MeterView | null)

const SAFE_SESSION_ID = /^[A-Za-z0-9_-]{1,120}$/

async function refresh($: EngineInterface): Promise<void> {
  const fileText = async (path: string): Promise<Source> => {
    if (!(await $.fs.exists(path))) return { kind: 'missing' }
    try {
      return { kind: 'text', text: String(await $.fs.read(path)) }
    } catch {
      return { kind: 'unreadable' }
    }
  }

  const { context } = await $.session.usage()
  const project = (await $.env.get('CLAUDE_PROJECT_DIR')) || (await $.session.cwd())
  const home = await $.env.get('HOME')
  const sources: Source[] = [await fileText(`${project}/.claude/${SETTINGS_FILE}`)]
  if (home) sources.push(await fileText(`${home}/.claude/${SETTINGS_FILE}`))

  const sessionId = await $.session.id()
  const state: Source = SAFE_SESSION_ID.test(sessionId)
    ? await fileText(`${project}/.claude/rotation/state/${sessionId}.enforcement.json`)
    : { kind: 'missing' }
  const startSize = startSizeOf(state.kind === 'text' ? state.text : undefined)

  await update($, view, () => viewOf(context, resolveSettings(sources), startSize))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await refresh($)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    await refresh($)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, view)
    const line = current === null || e.props.hasSurvey ? null : pieces(current)
    if (line === null) return next(e)

    const { Box, Text } = $.ui.resolve(e)

    return (
      <Box>
        {fit(line, e.props.bodyColumns).map(piece => (
          <Text color={piece.tone} dimColor={piece.isDim}>
            {piece.text}
          </Text>
        ))}
      </Box>
    )
  })
}
