import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { BrowserSession, elementReference } from '../deal_tracker/browser_session.mjs'

test('browser references accept frame-qualified IDs but not arbitrary selectors', () => {
  for (const ref of ['e24', 'f2e24']) assert.equal(elementReference.test(ref), true)
  for (const ref of ['body', '#password', 'e1, input', 'f2e24 button']) assert.equal(elementReference.test(ref), false)
})

async function verifyBrowser(headless) {
  const directory = await mkdtemp(join(tmpdir(), 'deal-tracker-browser-test-'))
  const fixture = join(directory, 'fixture.mjs')
  const html = `<html><body><main><h1>Fixture shirt</h1><p>Ref. 1418/212</p><p>EUR 40</p>
    <button onclick="document.querySelector('input').hidden=false">Search</button>
    <input aria-label="Search products" hidden oninput="document.querySelector('#results').textContent='Results for '+this.value">
    <p id="results"></p><button onclick="document.querySelector('#results').textContent='Public FAQ'">General Information</button><button>Checkout</button></main></body></html>`
  await writeFile(fixture, `export default async ({page}) => { await page.route('https://shop.example.com/**', route => route.fulfill({contentType:'text/html',body:${JSON.stringify(html)}})); }`)
  const browser = new BrowserSession({ shop: 'https://shop.example.com', proxy: 'http://127.0.0.1:9', initPages: [fixture], headless })
  try {
    await assert.rejects(browser.navigate('http://127.0.0.1:8765'), /outside/)
    const initial = await browser.navigate('https://shop.example.com/shirt')
    assert.match(initial.text, /Ref\. 1418\/212/)
    const search = initial.snapshot.match(/button "Search".*?\[ref=((?:f\d+)?e\d+)\]/)[1]
    const opened = await browser.action({ action: 'click', ref: search })
    const input = opened.snapshot.match(/textbox "Search products".*?\[ref=((?:f\d+)?e\d+)\]/)[1]
    const results = await browser.action({ action: 'type', ref: input, text: '1418/212', submit: true })
    assert.match(results.text, /Results for 1418\/212/)
    assert.equal(results.url, initial.url)
    const checkout = results.snapshot.match(/button "Checkout".*?\[ref=((?:f\d+)?e\d+)\]/)[1]
    await assert.rejects(browser.action({ action: 'click', ref: checkout }), /not permitted/)
    await assert.rejects(browser.action({ action: 'click', ref: 'body' }), /reference/)
    const information = results.snapshot.match(/button "General Information".*?\[ref=((?:f\d+)?e\d+)\]/)[1]
    assert.match((await browser.action({ action: 'click', ref: information })).text, /Public FAQ/)
  } finally {
    await browser.close()
    await rm(directory, { recursive: true, force: true })
  }
}

test('MCP browser keeps JavaScript/search state and blocks write controls', { timeout: 45000 }, async () => {
  await verifyBrowser(true)
})

test('headed MCP browser works through an authenticated virtual display', { timeout: 45000, skip: !process.env.DISPLAY }, async () => {
  await verifyBrowser(false)
})