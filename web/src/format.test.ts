import { describe, expect, it } from 'vitest'
import { formatBytes, formatDuration, shortPath } from './format'

describe('format', () => {
  it('sizes', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 ** 3)).toBe('5.0 GB')
  })

  it('durations', () => {
    expect(formatDuration(250)).toBe('250 ms')
    expect(formatDuration(1500)).toBe('1.5 s')
    expect(formatDuration(90000)).toBe('1 min 30 s')
  })

  it('paths use the most specific known folder', () => {
    const folders = { home: 'C:\\Users\\a', desktop: 'C:\\Users\\a\\Desktop' }
    expect(shortPath('C:\\Users\\a\\Desktop\\x.txt', folders)).toBe('Desktop\\x.txt')
    expect(shortPath('C:/Users/a/y.txt', folders)).toBe('Home\\y.txt')
    expect(shortPath('D:\\z.txt', folders)).toBe('D:\\z.txt')
  })
})
