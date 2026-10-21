// A JSON reader that keeps what JSON.parse drops: whether a number was written as an integer.
// The guard's validation (`context_budget.py`) tells 2000 from 2000.0, so the port has to as well.
// It accepts what Python's json module accepts, NaN and the infinities included.

export type Num = { readonly isNumber: true; readonly value: number; readonly isInt: boolean }
export type Json = null | boolean | string | Num | Json[] | { [key: string]: Json }

export class JsonError extends Error {}

const WHITESPACE = ' \t\n\r'
const NUMBER = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/
const ESCAPES: Record<string, string> = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t' }

export function isNum(value: Json | undefined): value is Num {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && (value as Num).isNumber === true
}

export function parseJson(text: string): Json {
  let at = 0

  const skip = () => {
    while (at < text.length && WHITESPACE.includes(text.charAt(at))) at += 1
  }

  const literal = (word: string, value: Json): Json => {
    if (!text.startsWith(word, at)) throw new JsonError('unexpected token at ' + at)
    at += word.length
    return value
  }

  const num = (value: number, isInt: boolean): Num => ({ isNumber: true, value, isInt })

  const string = (): string => {
    at += 1
    let out = ''
    while (at < text.length) {
      const ch = text.charAt(at)
      if (ch === '"') {
        at += 1
        return out
      }
      if (ch === '\\') {
        const next = text.charAt(at + 1)
        if (next === 'u') {
          const hex = text.slice(at + 2, at + 6)
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw new JsonError('bad escape at ' + at)
          out += String.fromCharCode(parseInt(hex, 16))
          at += 6
        } else if (next in ESCAPES) {
          out += ESCAPES[next]
          at += 2
        } else {
          throw new JsonError('bad escape at ' + at)
        }
      } else if (ch < ' ') {
        throw new JsonError('control character in a string at ' + at)
      } else {
        out += ch
        at += 1
      }
    }
    throw new JsonError('unterminated string')
  }

  const value = (): Json => {
    skip()
    const ch = text[at]
    if (ch === undefined) throw new JsonError('unexpected end')
    if (ch === '{') return object()
    if (ch === '[') return array()
    if (ch === '"') return string()
    if (ch === 't') return literal('true', true)
    if (ch === 'f') return literal('false', false)
    if (ch === 'n') return literal('null', null)
    if (ch === 'N') return literal('NaN', num(NaN, false))
    if (text.startsWith('Infinity', at)) return literal('Infinity', num(Infinity, false))
    if (text.startsWith('-Infinity', at)) return literal('-Infinity', num(-Infinity, false))
    const found = NUMBER.exec(text.slice(at))
    if (!found) throw new JsonError('unexpected token at ' + at)
    at += found[0].length
    return num(Number(found[0]), found[2] === undefined && found[3] === undefined)
  }

  const array = (): Json => {
    at += 1
    const items: Json[] = []
    skip()
    if (text[at] === ']') {
      at += 1
      return items
    }
    for (;;) {
      items.push(value())
      skip()
      if (text[at] === ',') at += 1
      else if (text[at] === ']') {
        at += 1
        return items
      } else throw new JsonError('expected , or ] at ' + at)
    }
  }

  const object = (): Json => {
    at += 1
    const members = Object.create(null) as { [key: string]: Json }
    skip()
    if (text[at] === '}') {
      at += 1
      return members
    }
    for (;;) {
      skip()
      if (text[at] !== '"') throw new JsonError('expected a key at ' + at)
      const key = string()
      skip()
      if (text[at] !== ':') throw new JsonError('expected : at ' + at)
      at += 1
      members[key] = value()
      skip()
      if (text[at] === ',') at += 1
      else if (text[at] === '}') {
        at += 1
        return members
      } else throw new JsonError('expected , or } at ' + at)
    }
  }

  const result = value()
  skip()
  if (at !== text.length) throw new JsonError('extra data at ' + at)
  return result
}
