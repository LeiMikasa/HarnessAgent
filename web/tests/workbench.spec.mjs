import { test, expect } from '@playwright/test'
import path from 'node:path'
import fs from 'node:fs'

const shots = path.resolve('../.scratch/web-preview')
fs.mkdirSync(shots, { recursive: true })

async function newMockSession(page) {
  await page.getByRole('button', { name: '新建会话', exact: false }).first().click()
  await page.getByLabel('使用模型').selectOption('mock')
  await page.getByRole('radio', { name: /完全权限/ }).check()
  await page.getByRole('button', { name: '创建会话', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
}

test('create session, stream real Mock events and restore after reload', async ({ page }) => {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '把想法，变成下一步。' })).toBeVisible()
  await page.screenshot({ path: path.join(shots, 'desktop-empty.png'), fullPage: true })
  await newMockSession(page)
  await expect(page.getByRole('button', { name: '调整权限' })).toContainText('完全权限')
  await page.getByRole('textbox', { name: '输入任务' }).fill('验证 Web 的免费演示流程')
  await page.getByRole('button', { name: '发送任务' }).click()
  await expect(page.getByText(/这是预设的 Mock 响应/)).toBeVisible()
  await expect(page.locator('.status-badge')).toHaveText('就绪')
  await expect(page.locator('.tool-entry')).toHaveCount(3)
  await expect(page.locator('.context-total')).not.toContainText('—')
  await page.screenshot({ path: path.join(shots, 'desktop-session.png'), fullPage: true })
  await page.reload()
  await expect(page.getByText(/这是预设的 Mock 响应/)).toBeVisible()
  await expect(page.locator('.tool-entry')).toHaveCount(3)
  expect(errors).toEqual([])
})

test('invalid project directory reports error inside the session dialog', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话', exact: false }).first().click()
  await page.getByLabel('项目工作目录').fill('Z:\\missing-harness-fixture\\never-created')
  await page.getByLabel('使用模型').selectOption('mock')
  await page.getByRole('button', { name: '创建会话', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('工作目录不存在')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
})

test('mobile navigation and composer fit the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: '打开会话列表' }).click()
  await newMockSession(page)
  await expect(page.getByRole('textbox', { name: '输入任务' })).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
  expect(overflow).toBe(false)
  await page.screenshot({ path: path.join(shots, 'mobile-session.png'), fullPage: true })
})

test('change permission and model in the same session, confirm full mode and retain conversation', async ({ page }) => {
  await page.goto('/')
  await newMockSession(page)
  const url = page.url()
  await page.getByRole('textbox', { name: '输入任务' }).fill('保留这段对话')
  await page.getByRole('button', { name: '发送任务' }).click()
  await expect(page.getByText(/这是预设的 Mock 响应/)).toBeVisible()
  await page.getByRole('button', { name: '调整权限' }).click()
  await page.getByRole('radio', { name: /只读/ }).check()
  await page.getByRole('button', { name: '应用设置' }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(page.getByRole('button', { name: '调整权限' })).toContainText('只读')
  await page.getByRole('button', { name: '调整权限' }).click()
  await page.getByRole('radio', { name: /完全权限/ }).check()
  await expect(page.getByRole('button', { name: '应用设置' })).toBeDisabled()
  await page.screenshot({ path: path.join(shots, 'dynamic-permissions.png'), fullPage: true })
  await page.getByRole('checkbox', { name: '我确认将本会话切换为完全权限' }).check()
  await page.getByRole('button', { name: '应用设置' }).click()
  await expect(page.getByRole('button', { name: '调整权限' })).toContainText('完全权限')
  await page.getByRole('button', { name: '切换模型' }).click()
  await page.getByLabel('模型 ID').fill('mock-2')
  await page.screenshot({ path: path.join(shots, 'dynamic-model.png'), fullPage: true })
  await page.getByRole('button', { name: '应用设置' }).click()
  await expect(page.getByText('设置已生效 · 模型：mock / mock-2')).toBeVisible()
  expect(page.url()).toBe(url)
  await page.reload()
  await expect(page.getByRole('region', { name: 'Agent 对话' }).getByText('保留这段对话', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '调整权限' })).toContainText('完全权限')
  await page.getByRole('button', { name: '切换模型' }).click()
  await expect(page.getByLabel('模型 ID')).toHaveValue('mock-2')
  await page.getByRole('button', { name: '取消', exact: true }).click()
})

test('folder chooser fills the path and cancelling keeps the previous selection', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话', exact: false }).first().click()
  const input = page.getByLabel('项目工作目录')
  await input.fill('D:\\old-project')
  let pending
  await page.route('**/api/workspaces/pick', route => { pending = route })
  await page.getByRole('button', { name: '选择文件夹', exact: true }).click()
  await expect.poll(() => Boolean(pending)).toBe(true)
  await expect(page.getByRole('button', { name: '选择中…' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '创建会话', exact: true })).toBeDisabled()
  expect(pending.request().postDataJSON()).toEqual({ initial_path: 'D:\\old-project' })
  const selected = 'D:\\资料\\两层目录\\Vue 项目'
  await pending.fulfill({ json: { path: selected } })
  await expect(input).toHaveValue(selected)
  await expect(input).toBeFocused()
  pending = undefined
  await page.getByRole('button', { name: '选择文件夹', exact: true }).click()
  await expect.poll(() => Boolean(pending)).toBe(true)
  await pending.fulfill({ json: { path: null } })
  await expect(page.getByRole('button', { name: '选择文件夹', exact: true })).toBeEnabled()
  await expect(input).toHaveValue(selected)
  await page.screenshot({ path: path.join(shots, 'folder-picker.png'), fullPage: true })
})

test('folder chooser failure keeps manual directory input available', async ({ page }) => {
  await page.route('**/api/workspaces/pick', route => route.fulfill({
    status: 503, json: { detail: '无法打开系统文件夹选择窗口，请手动填写路径' },
  }))
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话', exact: false }).first().click()
  await page.getByRole('button', { name: '选择文件夹', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('手动填写路径')
  await page.getByLabel('项目工作目录').fill('D:\\manual-project')
  await expect(page.getByLabel('项目工作目录')).toHaveValue('D:\\manual-project')
})

test('host approval defaults to deny, Escape posts denial, and markdown is sanitized', async ({ page }) => {
  // UI contract fixture. Python integration tests exercise actual permission hooks.
  const metadata = { id: 'fixture', title: '审批测试', workdir: 'D:\\project', permission_mode: 'workspace',
    provider: 'mock', model: 'mock-1', created_at: '', updated_at: '' }
  const state = { metadata, busy: true, cancel_requested: false, activity: '等待你的批准',
    timeline: [{ id: 'assistant-a', kind: 'message', role: 'assistant', text: '<img src=x onerror="window.xssFixture=1"><script>window.xssFixture=2</script>安全正文' }],
    pending_approvals: [{ id: 'approval-a', tool: 'read_file', arguments: { path: 'D:\\资料\\说明.md' },
      reason: '读取外部文档', owner: 'lead', workdir: 'D:\\project', host_execution: true }],
    context_usage: null, metrics: {}, todos: [], last_error: '', seq: 1 }
  let submitted
  await page.route('**/api/sessions', route => route.fulfill({ json: [metadata] }))
  await page.route('**/api/sessions/fixture', route => route.fulfill({ json: state }))
  await page.route('**/api/sessions/fixture/events', route => route.fulfill({
    contentType: 'text/event-stream', body: `event: snapshot\ndata: ${JSON.stringify(state)}\n\n`,
  }))
  await page.route('**/api/sessions/fixture/approvals/approval-a', route => {
    submitted = route.request().postDataJSON()
    return route.fulfill({ json: { resolved: true } })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '允许本次在宿主机执行？' })).toBeVisible()
  await expect(page.getByRole('button', { name: '拒绝本次', exact: true })).toBeFocused()
  await page.screenshot({ path: path.join(shots, 'host-approval.png'), fullPage: true })
  await page.keyboard.press('Escape')
  await expect.poll(() => submitted).toEqual({ allowed: false })
  await expect(page.locator('.prose img')).toHaveCount(0)
  expect(await page.evaluate(() => window.xssFixture)).toBeUndefined()
})

test('thinking is collapsible, updates through SSE and stays separate from answer text', async ({ page }) => {
  const metadata = { id: 'thinking-fixture', title: '思考展示测试', workdir: 'D:\\project', permission_mode: 'workspace',
    provider: 'mock', model: 'mock-1', created_at: '', updated_at: '' }
  const initial = { id: 'model-a', kind: 'message', role: 'assistant', text: '', thinking: '正在检查配置…',
    thinking_status: 'streaming', status: 'running' }
  const final = { ...initial, text: '已经找到配置，接下来进行修改。', status: 'done', thinking_status: 'done',
    thinking: '已检查配置文件。\n<img src=x onerror="window.thinkingXss=1">' }
  const state = { metadata, busy: true, cancel_requested: false, activity: '正在思考',
    timeline: [initial, { id: 'model-b', kind: 'message', role: 'assistant', text: '这是一条没有思考内容的消息。', status: 'done' }],
    pending_approvals: [], context_usage: null, metrics: {}, todos: [], last_error: '', seq: 1 }
  let release
  await page.route('**/api/sessions', route => route.fulfill({ json: [metadata] }))
  await page.route('**/api/sessions/thinking-fixture', route => route.fulfill({ json: state }))
  await page.route('**/api/sessions/thinking-fixture/events', route => new Promise(resolve => {
    release = async () => {
      await route.fulfill({ contentType: 'text/event-stream', body: `event: entry\ndata: ${JSON.stringify(final)}\n\n` })
      resolve()
    }
  }))
  await page.goto('/')
  const section = page.locator('.thinking-entry')
  await expect(section).toHaveCount(1)
  await expect(section).not.toHaveAttribute('open', '')
  await expect(section.locator('summary')).toContainText('生成中')
  await section.locator('summary').click()
  await expect(section.locator('.thinking-body')).toHaveText('正在检查配置…')
  await expect.poll(() => Boolean(release)).toBe(true)
  await release()
  await expect(section.locator('.thinking-body')).toHaveText(final.thinking)
  await expect(section.locator('summary')).not.toContainText('生成中')
  await expect(page.locator('.prose').filter({ hasText: final.text })).toBeVisible()
  await expect(page.locator('.thinking-body img')).toHaveCount(0)
  expect(await page.evaluate(() => window.thinkingXss)).toBeUndefined()
  await expect(section).toHaveAttribute('open', '')
  await page.screenshot({ path: path.join(shots, 'thinking-stream.png'), fullPage: true })
})
