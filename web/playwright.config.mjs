import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'
import crypto from 'node:crypto'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const workspace = path.join(root, '.scratch', 'web-e2e', crypto.randomUUID())
fs.mkdirSync(workspace, { recursive: true })
const python = process.platform === 'win32'
  ? path.join(root, '.venv', 'Scripts', 'python.exe')
  : path.join(root, '.venv', 'bin', 'python')

export default defineConfig({
  testDir: './tests',
  outputDir: '../.scratch/web-test-results',
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:8767',
    viewport: { width: 1440, height: 960 },
    channel: process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : 'chromium'),
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `"${python}" -m agent.web --port 8767 --workdir "${workspace}"`,
    cwd: root,
    url: 'http://127.0.0.1:8767/api/config',
    reuseExistingServer: false,
    timeout: 15_000,
  },
})
