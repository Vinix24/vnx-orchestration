// Display width of text in terminal cells; shared by the pane and the status line.

/** Code point ranges a terminal draws two cells wide: East Asian wide and fullwidth forms, and emoji. */
const WIDE: readonly (readonly [number, number])[] = [
  [0x1100, 0x115f],
  [0x231a, 0x231b],
  [0x23e9, 0x23ec],
  [0x25fd, 0x25fe],
  [0x2614, 0x2615],
  [0x2648, 0x2653],
  [0x2693, 0x2693],
  [0x26a1, 0x26a1],
  [0x26bd, 0x26be],
  [0x26c4, 0x26c5],
  [0x26d4, 0x26d4],
  [0x26ea, 0x26ea],
  [0x26f2, 0x26f3],
  [0x26f5, 0x26f5],
  [0x26fa, 0x26fa],
  [0x26fd, 0x26fd],
  [0x2705, 0x2705],
  [0x270a, 0x270b],
  [0x2728, 0x2728],
  [0x274c, 0x274c],
  [0x2753, 0x2755],
  [0x2795, 0x2797],
  [0x2b1b, 0x2b1c],
  [0x2b50, 0x2b50],
  [0x2b55, 0x2b55],
  [0x2e80, 0x303e],
  [0x3041, 0x33ff],
  [0x3400, 0x4dbf],
  [0x4e00, 0x9fff],
  [0xa000, 0xa4cf],
  [0xac00, 0xd7a3],
  [0xf900, 0xfaff],
  [0xfe30, 0xfe4f],
  [0xff00, 0xff60],
  [0xffe0, 0xffe6],
  [0x1f300, 0x1f64f],
  [0x1f680, 0x1f6ff],
  [0x1f900, 0x1faff],
  [0x20000, 0x3fffd],
]
const MARK = /^\p{M}$/u
const ZERO_WIDTH_JOINER = 0x200d
const VARIATION_SELECTOR_EMOJI = 0xfe0f

const isExtender = (char: string, code: number): boolean =>
  MARK.test(char) || (code >= 0xfe00 && code <= 0xfe0f) || (code >= 0x1f3fb && code <= 0x1f3ff) || code === ZERO_WIDTH_JOINER

function cellsOfCode(char: string, code: number): number {
  if (code < 0x20 || (code >= 0x7f && code < 0xa0) || (code >= 0x200b && code <= 0x200f) || code === 0x2060 || code === 0xfeff || MARK.test(char)) {
    return 0
  }

  return WIDE.some(([from, to]) => code >= from && code <= to) ? 2 : 1
}

type Cluster = { text: string; cells: number }

/** The text as what a terminal draws in one place: a character with the marks, selectors and joined characters after it. */
function clustersOf(text: string): Cluster[] {
  const clusters: Cluster[] = []
  let isJoined = false

  for (const char of text) {
    const code = char.codePointAt(0) ?? 0
    const last = clusters[clusters.length - 1]

    if (last !== undefined && (isJoined || isExtender(char, code))) {
      last.text += char
      last.cells = code === VARIATION_SELECTOR_EMOJI ? Math.max(last.cells, 2) : last.cells
      isJoined = code === ZERO_WIDTH_JOINER
    } else {
      clusters.push({ text: char, cells: cellsOfCode(char, code) })
      isJoined = false
    }
  }

  return clusters
}

/** The width of the text in display cells: wide characters take two, combining marks none. */
export function cellsOf(text: string): number {
  return clustersOf(text).reduce((sum, cluster) => sum + cluster.cells, 0)
}

/** The text cut to `columns` display cells with an ellipsis, so no character is split and none sticks out of the pane. */
export function fit(text: string, columns: number): string {
  if (cellsOf(text) <= columns) {
    return text
  }

  if (columns <= 1) {
    return '…'
  }

  let cells = 0
  let kept = ''

  for (const cluster of clustersOf(text)) {
    if (cells + cluster.cells > columns - 1) {
      break
    }

    cells += cluster.cells
    kept += cluster.text
  }

  return `${kept}…`
}
