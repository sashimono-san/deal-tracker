import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { chromium } from 'playwright'

const sensitive = /\b(log\s*in|sign\s*in|account|cart|basket|bag|checkout|buy|purchase|payment|wishlist|subscribe|warenkorb|kaufen|bestellen|anmelden)\b/i
const controls = /\b(menu|search|man|woman|men|women|new in|md world|sale|close|filter|size|colo[u]?r|more|previous|next|details|information|faq|help|fabric|care|shipping|returns|reject|necessary|decline|accept|cancel)\b/i
export const elementReference = /^(?:f\d{1,5})?e\d{1,8}$/

function textOf(result) {
  return result.content?.filter(part => part.type === 'text').map(part => part.text).join('\n') || ''
}

function evaluated(result) {
  const text = textOf(result)
  const marker = '### Result\n'
  const offset = text.indexOf(marker)
  if (offset < 0) throw new Error('Browser did not return structured page data')
  return JSON.parse(text.slice(offset + marker.length).split('\n### ')[0])
}

export class BrowserSession {
  constructor({ shop, proxy, initPages = [], headless = true }) {
    this.shop = new URL(shop)
    this.proxy = proxy
    this.initPages = initPages
    this.headless = headless
    this.actions = 0
    this.navigations = 0
    this.client = null
    this.directory = null
  }

  inShop(value) {
    const url = new URL(value)
    const host = this.shop.hostname.replace(/^www\./, '')
    return url.protocol === 'https:' && !url.username && !url.password && (!url.port || url.port === '443') &&
      (url.hostname === host || url.hostname.endsWith(`.${host}`))
  }

  async start() {
    if (this.client) return
    this.directory = await mkdtemp(join(tmpdir(), 'deal-tracker-browser-'))
    const configuration = {
      browser: {
        launchOptions: { args: ['--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'] },
        contextOptions: { acceptDownloads: false, serviceWorkers: 'block' },
        initPage: [fileURLToPath(new URL('./browser_init.mjs', import.meta.url)), ...this.initPages],
      },
      webmcp: false, saveSession: false, codegen: 'none', outputDir: this.directory,
      timeouts: { action: 10_000, navigation: 20_000 },
    }
    const configPath = join(this.directory, 'config.json')
    await writeFile(configPath, JSON.stringify(configuration))
    const environment = {
      PATH: process.env.PATH || '', HOME: this.directory, LANG: 'en_US.UTF-8',
      DEAL_TRACKER_BROWSER_PROXY: this.proxy,
    }
    if (process.env.PLAYWRIGHT_BROWSERS_PATH) environment.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH
    if (!this.headless) {
      for (const field of ['DISPLAY', 'XAUTHORITY']) {
        if (process.env[field]) environment[field] = process.env[field]
      }
    }
    this.client = new Client({ name: 'deal-tracker-collector', version: '1' })
    try {
      const transport = new StdioClientTransport({
        command: process.execPath,
        args: [fileURLToPath(new URL('cli.js', import.meta.resolve('@playwright/mcp'))),
          '--config', configPath, ...(this.headless ? ['--headless'] : []), '--isolated', '--block-service-workers',
          '--executable-path', chromium.executablePath(), '--proxy-server', this.proxy,
          '--proxy-bypass', '<-loopback>'],
        cwd: this.directory, env: environment, stderr: 'pipe',
      })
      await this.client.connect(transport)
      transport.stderr?.resume()
    } catch (error) {
      await this.close()
      throw error
    }
  }

  async call(name, arguments_, signal, tolerateNavigationAbort = false) {
    await this.start()
    const result = await this.client.callTool({ name, arguments: arguments_ }, undefined, {
      signal, timeout: 25_000, resetTimeoutOnProgress: false,
    })
    if (result.isError && !(tolerateNavigationAbort && /ERR_ABORTED/.test(textOf(result)))) {
      throw new Error(textOf(result).slice(0, 600) || 'Browser operation failed')
    }
    return result
  }

  async read(signal) {
    const page = evaluated(await this.call('browser_evaluate', { function: `() => ({
      url: location.href, title: document.title,
      text: (document.querySelector('main') || document.body).innerText.slice(0, 10000),
      structured_data: Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(element => element.textContent).join('\\n').slice(0, 6000)
    })` }, signal))
    if (!this.inShop(page.url)) throw new Error('Browser left the selected shop')
    const snapshot = await this.call('browser_snapshot', {}, signal)
    return { ...page, fetched_at: new Date().toISOString(), snapshot: textOf(snapshot).slice(0, 10000) }
  }

  async navigate(url, signal) {
    if (!this.inShop(url)) throw new Error('URL is outside the selected HTTPS shop')
    if (++this.navigations > 6 || ++this.actions > 12) throw new Error('Browser action limit reached')
    await this.call('browser_navigate', { url }, signal, true)
    return this.read(signal)
  }

  async action({ action, ref, text, submit = false }, signal) {
    if (++this.actions > 12) throw new Error('Browser action limit reached')
    if (action === 'read') return this.read(signal)
    if (action === 'wait') {
      if (!text || text.length > 200) throw new Error('Specify the text to wait for')
      await this.call('browser_wait_for', { text, time: 10 }, signal)
      return this.read(signal)
    }
    if (!elementReference.test(ref || '')) throw new Error('Use an element reference from the latest browser snapshot')
    const element = evaluated(await this.call('browser_evaluate', {
      target: ref,
      function: `element => ({tag:element.tagName, role:element.getAttribute('role') || '',
        type:element.getAttribute('type') || '', href:element.closest('a')?.href || '',
        label:[element.getAttribute('aria-label'), element.getAttribute('placeholder'),
          element.getAttribute('title'), element.innerText].filter(Boolean).join(' ').slice(0, 1000)})`,
    }, signal))
    if (sensitive.test(`${element.label} ${element.href}`)) throw new Error('Account, cart, checkout and other write actions are not permitted')
    if (action === 'type') {
      if (element.tag !== 'INPUT' || !/search/i.test(`${element.type} ${element.label}`) || !text || text.length > 200) {
        throw new Error('Typing is limited to public shop search fields')
      }
      await this.call('browser_type', { target: ref, text, submit }, signal)
    } else if (action === 'click') {
      if (element.href ? !this.inShop(element.href) : !controls.test(element.label) && !['option', 'radio'].includes(element.role)) {
        throw new Error('Only shop links and browsing controls may be clicked')
      }
      await this.call('browser_click', { target: ref }, signal, true)
    } else {
      throw new Error('Unsupported browser action')
    }
    return this.read(signal)
  }

  async close() {
    const client = this.client
    this.client = null
    if (client) await client.close()
    if (this.directory) await rm(this.directory, { recursive: true, force: true })
    this.directory = null
  }
}