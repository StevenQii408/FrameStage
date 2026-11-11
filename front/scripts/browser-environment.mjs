import { constants } from 'node:fs'
import { access, mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'

export function temporaryDirectory(prefix) {
  return mkdtemp(join(tmpdir(), prefix))
}

export async function chromeExecutable(override = process.env.CHROME_BIN) {
  const names = process.platform === 'win32'
    ? ['chrome.exe', 'chromium.exe']
    : ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']
  const candidates = override ? [override] : (process.env.PATH || '').split(delimiter)
    .filter(Boolean).flatMap(dir => names.map(name => join(dir, name)))
  if (!override && process.platform === 'darwin') {
    candidates.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium')
  }
  if (!override && process.platform === 'win32') {
    for (const base of [process.env.ProgramFiles, process.env['ProgramFiles(x86)'], process.env.LOCALAPPDATA]) {
      if (base) candidates.push(join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    }
  }
  for (const candidate of candidates) {
    try { await access(candidate, constants.X_OK); return candidate } catch { /* try next location */ }
  }
  throw new Error('未找到可执行的 Chrome 或 Chromium。请使用 --chrome 或 CHROME_BIN 指定浏览器可执行文件的路径。')
}
