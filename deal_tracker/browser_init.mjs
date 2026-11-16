export default async function initialize({ page }) {
  page.on('popup', popup => { void popup.close() })
  await page.route('**/*', async route => {
    const request = route.request()
    if (['image', 'media', 'font'].includes(request.resourceType())) return route.abort()
    if (request.isNavigationRequest() && request.method() !== 'GET') return route.abort()
    try {
      const response = await fetch(`${process.env.DEAL_TRACKER_BROWSER_PROXY}/permission`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: request.url(), navigation: request.isNavigationRequest() }),
        signal: AbortSignal.timeout(15_000),
      })
      if (!(await response.json()).allowed) return route.abort()
      return route.continue()
    } catch {
      return route.abort()
    }
  })
}