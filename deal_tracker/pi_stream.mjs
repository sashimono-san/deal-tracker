import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'

const child = spawn(process.argv[2], process.argv.slice(3), { stdio: ['pipe', 'pipe', 'pipe'] })
let received = 0
let lineBytes = 0
let rejected = false

function reject() {
  if (rejected) return
  rejected = true
  process.stderr.write('pi event stream exceeded its transport bound\n')
  child.kill('SIGTERM')
}

child.stdout.on('data', chunk => {
  received += chunk.length
  for (const byte of chunk) {
    lineBytes = byte === 10 ? 0 : lineBytes + 1
    if (lineBytes > 512_000) reject()
  }
  if (received > 16_000_000) reject()
})

createInterface({ input: child.stdout }).on('line', line => {
  if (rejected || !line.trim()) return
  try {
    const event = JSON.parse(line)
    if (event.type === 'message_end' && event.message?.role === 'assistant') {
      const usage = event.message.usage || event.usage
      const fields = ['input', 'output', 'cacheRead', 'cacheWrite']
      const reported = Boolean(usage && fields.every(field => Number.isSafeInteger(usage[field]) && usage[field] >= 0) && fields.some(field => usage[field] > 0))
      process.stdout.write(`${JSON.stringify({ type: 'collector_usage', reported, usage: reported ? Object.fromEntries(fields.map(field => [field, usage[field]])) : null })}\n`)
      return
    }
    if (event.type !== 'tool_execution_end' || !['fetch_shop_page', 'browser_action', 'finish_collection'].includes(event.toolName)) return
    if (event.toolName !== 'finish_collection' && !event.isError && !event.result?.isError) {
      const page = JSON.parse(event.result.content[0].text)
      event.result.content[0].text = JSON.stringify({
        url: page.url, fetched_at: page.fetched_at, text: page.text, structured_data: page.structured_data,
      })
    }
    process.stdout.write(`${JSON.stringify(event)}\n`)
  } catch {
    rejected = true
    process.stderr.write('pi returned a malformed event\n')
    child.kill('SIGTERM')
  }
})

process.stdin.pipe(child.stdin)
child.stdin.on('error', () => {})
child.stderr.pipe(process.stderr)
process.once('SIGTERM', () => child.kill('SIGTERM'))
process.once('SIGINT', () => child.kill('SIGINT'))
child.on('error', () => { process.exitCode = 1 })
child.on('close', code => { process.exitCode = rejected ? 1 : code ?? 1 })