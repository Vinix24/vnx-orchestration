import { expect, test } from 'claude-code/testing'

import cases from './fixtures/meter_parity'
import { guardLevel, resolveSettings, thresholds, type Source } from '../hooks/meter'

const source = (text: string | null): Source => (text === null ? { kind: 'missing' } : { kind: 'text', text })

test('the port reads the settings files as the guard does', async () => {
  expect(cases.settings_cases.length).toBeGreaterThanOrEqual(12)
  for (const c of cases.settings_cases) {
    const settings = resolveSettings([source(c.project), source(c.home)])
    expect({ name: c.name, thresholds: thresholds(settings) }).toEqual({ name: c.name, thresholds: c.expect.thresholds })
    expect({ name: c.name, fromPct: settings.tokens === null }).toEqual({ name: c.name, fromPct: c.expect.from_pct })
    expect({ name: c.name, enabled: settings.enabled }).toEqual({ name: c.name, enabled: c.expect.enabled })
    expect({ name: c.name, showMeter: settings.showMeter }).toEqual({ name: c.name, showMeter: c.expect.show_meter })
    expect({ name: c.name, windowTokens: settings.windowTokens }).toEqual({ name: c.name, windowTokens: c.expect.window_tokens })
    expect({ name: c.name, minWork: settings.minWorkTokens }).toEqual({ name: c.name, minWork: c.expect.min_work_tokens })
  }
})

test('the port holds an injected session below force and hard as the guard does', async () => {
  for (const c of cases.guard_cases) {
    const settings = resolveSettings([source(c.project)])
    expect({ name: c.name, level: guardLevel(c.size, c.start_size, settings) }).toEqual({ name: c.name, level: c.expect_level })
  }
})
