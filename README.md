# Deal Tracker

Deal Tracker is a local web app for buying intentions. Each buying intention is a search. A search has one of these modes:

- **Exact product**: one product with a known SKU in one shop.
- **Shop sale**: a sale in one shop.
- **Discovery**: a general description of a product that you want.

The app collects evidence from shop pages and shows the results as candidates. You can expand a search into new related searches. To make an exact-product search from a candidate, use **Track exact product**.

The code and the API use the word "watch" for a search.

The app uses Python (FastAPI), SQLite and a React UI with TypeScript and Vite. The server also serves the compiled UI.

## Status

- **Operates locally:** the UI, the database, the schedules, the worker and the email outbox. The automated tests use synthetic pages and fake services.
- **Tested with a real shop:** a visible browser collected a Massimo Dutti price on the Mac. The Linux container can also read this product and use the shop search with an Xvfb virtual display. In the container, GPT-6.1 Sol collected the reference, the price, the color, the market and the stock of this product, with `arm64` and with emulated `amd64`. One of three container checks gave no result.
- **Known limits:** Massimo Dutti sent HTTP 403 to the headless browser. The HTTP reader got no product data from this shop.
- **Not tested:** reliable use of Chromium's headless mode for this shop, `amd64` hardware, a scheduled VM run and email through a real SMTP provider. No homelab deployment was done.
- **Defaults:** model use, worker checks and email delivery are off.
- **Timers:** there is no timer on the Mac or on the VM.

The project notes outside this repository keep the history, the test records and the lower-level design details.

## Intent

- The app is for a small number of occasional searches. It is not a platform for many searches at the same time.
- The deployment uses the provider `github-copilot` with the model `gpt-6.1-sol` and `medium` thinking. The app code has no default model.
- Test the virtual-display worker on the target host before you install a timer.
- Keep email off until you select an SMTP provider and a recipient.
- The deployment target is the container on the homelab VM, with a daily timer. This deployment must have a separate approval.

## Requirements

- Python 3.12 or later.
- Node 22.19 or later. pi and the browser bridge use Node. The container uses Node 24 LTS.
- pi 1.4.0, only for collection. The UI operates without pi when collection is off.

## Install and Start

1. Go to the repository root.
2. Use these commands to install the app and its browser, and to build the UI:

   ```sh
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -e '.[dev]'
   npm ci
   npm run browser:install
   npm --prefix frontend ci
   npm --prefix frontend run build
   ```

3. Start the server:

   ```sh
   .venv/bin/python -m uvicorn deal_tracker.api:create_app --factory --host 127.0.0.1 --port 8765
   ```

4. Open <http://127.0.0.1:8765>.

The server stops when you close its terminal. Your searches stay in the database when you start the server again. If port 8765 is in use, use a different free port.

CAUTION: Do not make the server available on a LAN or on the internet. The server has no login. The start command above makes the server available only on `127.0.0.1`.

### Database

The default database is `data/tracker.sqlite3`. This path is relative to the current directory. Start the server and the worker in the repository root, or use an absolute path. To use a different database, set `DEAL_TRACKER_DB` to its path. Do not use a temporary test database for daily use.

Git ignores the runtime data, the virtual environment and the UI build output.

### Container

The [Dockerfile](Dockerfile) packages the same app. [DEPLOYMENT.md](DEPLOYMENT.md) describes a server deployment with Docker Compose.

## Configure Collection

1. If pi 1.4.0 is not installed, install it in a Node environment that you control:

   ```sh
   npm install --global @earendil-works/pi-coding-agent@1.4.0
   ```

2. Select a pi profile directory:
   - For a local test, you can use your current pi profile. A second account is not necessary.
   - For the unattended container, use a dedicated profile.
3. If the profile has no login, use the pi command `/login` in your own terminal.
4. Give the server and the worker the same values for the settings in the table below.
5. Start the server and the worker again.

| Setting | Default | Description |
| --- | --- | --- |
| `DEAL_TRACKER_COLLECTION_ENABLED` | `false` | Set to `true` to let pi use a model. Do this only after you approve the model use. |
| `DEAL_TRACKER_SCHEDULER_ENABLED` | `false` | Set to `true` to let the worker do checks. Manual checks also use the worker. |
| `DEAL_TRACKER_PI_EXECUTABLE` | `pi` | The pi command or its absolute path. |
| `DEAL_TRACKER_PI_DIRECTORY` | None | The absolute path of the pi profile directory. The app gives it to pi as `PI_CODING_AGENT_DIR`. |
| `DEAL_TRACKER_PI_PROVIDER` | None | The provider ID. There is no default provider. |
| `DEAL_TRACKER_PI_MODEL` | None | The model ID. There is no default model. |
| `DEAL_TRACKER_PI_THINKING` | `low` | The thinking level: `off`, `low` or `medium`. The model must support the level. |
| `DEAL_TRACKER_BROWSER_ENABLED` | `true` | `true` uses the Playwright MCP browser. `false` uses the HTTP reader, which cannot use JavaScript. |
| `DEAL_TRACKER_BROWSER_HEADLESS` | `true` in the app; `false` in the stack | `false` uses a desktop display or an Xvfb virtual display. |
| `DEAL_TRACKER_BROWSER_RESOURCE_ORIGINS` | None | Public HTTPS origins for page resources, with commas between them. The maximum is eight. The app always permits the shop origin. Example: `https://static.massimodutti.net`. |
| `PLAYWRIGHT_BROWSERS_PATH` | Playwright cache | The browser installation directory. The container image uses `/ms-playwright`. |
| `DEAL_TRACKER_CHECK_TIMEOUT` | `120` | The time limit of one check in seconds, from 5 to 120. |
| `DEAL_TRACKER_DAILY_CHECK_LIMIT` | `12` | The maximum number of checks in one UTC day for all searches, from 1 to 100. |

The server and the worker read exported environment variables. They do not read a dotenv file. In Compose, use an environment file. Refer to [DEPLOYMENT.md](DEPLOYMENT.md#7-make-the-configuration-file). The **Connection status** dialog shows the problems in the settings. It does not show secrets.

The app does not change the default model of the pi profile. It gives the provider, the model and the thinking level to pi for each check. An OAuth refresh can change the files in the pi profile. For more information, refer to the [pi documentation](https://github.com/earendil-works/pi/tree/v1.4.0/packages/coding-agent).

CAUTION: Do not put keys, tokens or the contents of credential files in chat, in Git or in notes.

## Worker

The worker does one batch of checks and then stops. A batch does the queued manual checks first. Then it does the due checks of active searches, up to the daily limit. If email is on, a batch also sends the queued emails.

To start one batch:

```sh
.venv/bin/python -m deal_tracker.worker
```

To do only one check, add `--limit 1`:

```sh
.venv/bin/python -m deal_tracker.worker --limit 1
```

A database lock prevents two batches at the same time. The daily count stays correct after a restart. If a batch reaches the daily limit, the other due checks wait for a later batch. Manual and scheduled checks use the same daily limit. This limit counts checks. It is not a money limit.

To start a batch every day, use a systemd timer. Refer to [DEPLOYMENT.md](DEPLOYMENT.md#13-install-the-daily-timer).

## Activate a Search

New and expanded searches start paused. To activate a search, use **Activate monitoring**. The search must have these items:

- A shop URL that uses HTTPS.
- A delivery country and a currency.
- A schedule.

An exact-product search must also have a SKU and a product URL in the same shop. Also, the collection settings must be complete, and `DEAL_TRACKER_SCHEDULER_ENABLED` must be `true`.

Size, color and the item-price target are optional. If you set them, they filter the results. If you do not set an item-price target, each result that shows stock and a price is for review. For more information, refer to [Results That Make an Email](#results-that-make-an-email).

If you edit an active search, the app pauses it. You must activate each expanded search.

### Manual Checks

**Request check** puts a check in the queue for the next batch. It does not start the worker. To do the check sooner, start a batch. **Request check** operates only when the collection settings are complete, `DEAL_TRACKER_SCHEDULER_ENABLED` is `true` and the shop URL uses HTTPS. You can request a check for a paused search. A check of a paused search never sends email.

## Schedules

Each search has its own schedule. In the search editor, set **Repeat** and **Repeat every** (N):

- **Manual only**: no scheduled checks.
- **Daily**: a check every N days.
- **Weekly**: a check every N weeks, on the weekdays that you select.

A schedule also has a start date and a time zone. A scheduled date becomes due at midnight in that time zone. The worker does the check at its next start, not at midnight.

After a check, the next due date is the first scheduled date after the local day of that check. If the worker does not start on some scheduled dates, it does only one check for all of these dates. A manual check of an active search also moves the next due date.

### Sale Periods

Use **Daily checks during** to add daily checks in these periods:

- Black Friday week through Cyber Monday. This period starts 7 days before Black Friday.
- Christmas: 15-26 December.
- New Year: 27 December-7 January.
- Year start: 8-31 January.

These periods are off by default. They do not state that a shop has a sale on these dates. Sale-period checks obey the start date, the pause state, the worker lock and the daily limit.

## Collection

The app has one collector. [pi_harness.py](deal_tracker/pi_harness.py) contains the system prompt and gives pi one search snapshot. [pi_collector.mjs](deal_tracker/pi_collector.mjs) gives pi these tools:

- `fetch_shop_page` opens a shop page and returns its text.
- `browser_action` clicks, types and waits in the shop page. This tool is available only in browser mode.
- `finish_collection` returns the result.

In browser mode, [browser_session.mjs](deal_tracker/browser_session.mjs) connects pi to the official `@playwright/mcp@0.0.84` server. pi does not load its built-in tools, skills, context files, personal extensions or MCP servers. The app does not change your MCP settings. Instructions for coding agents in this repository have no effect on the collector.

### Browser

The browser session stays open for the full check. The collector can do these actions:

- Open a shop URL.
- Read a page snapshot.
- Click a shop link or a navigation control.
- Type in the search field of the shop.
- Wait for results.

The collector cannot use login, account, cart or checkout controls. It cannot use selectors, JavaScript, file uploads, downloads or browser administration.

The browser connects through a local proxy ([browser_proxy.py](deal_tracker/browser_proxy.py)). The proxy permits only HTTPS connections to the shop and to the resource origins that you set. These hosts must have public addresses. [browser_init.mjs](deal_tracker/browser_init.mjs) obeys `robots.txt` and keeps navigation in the shop. It blocks images, media and fonts, and it closes pop-up windows. The browser has no other web access.

If a shop loads page resources from a different host, add that origin to `DEAL_TRACKER_BROWSER_RESOURCE_ORIGINS`.

#### Linux Without a Screen

A server without a screen can run a browser with a virtual display. The browser does not have to use Chromium's headless mode.

For this mode, set `DEAL_TRACKER_BROWSER_HEADLESS=false`. The container worker starts its command with `xvfb-run`. This command starts Xvfb, sets `DISPLAY` and `XAUTHORITY`, and removes the display when the command stops. X11 TCP access is disabled. A desktop login, monitor or VNC server is not necessary.

The harness and the MCP session pass `DISPLAY` and `XAUTHORITY` to their child processes only for a visible-browser check. Other environment restrictions remain in effect. The image includes `xvfb` and `xauth`.

For a native Linux installation with these packages, start the worker with:

```sh
DEAL_TRACKER_BROWSER_HEADLESS=false xvfb-run --auto-servernum --server-args="-screen 0 1280x800x24 -nolisten tcp" .venv/bin/python -m deal_tracker.worker --once
```

The normal provider and collection settings must also be set. A virtual display does not guarantee that a shop will accept requests from a different host. Test the actual shop from the deployment host before you enable its timer.

The [HTTP reader](deal_tracker/fetcher.py) obeys the same HTTPS, public-address and `robots.txt` rules. It cannot use JavaScript.

### Limits

Each check has these limits:

- 6 page navigations.
- 12 browser actions.
- 8 model turns.
- 5 observations.
- The time limit in `DEAL_TRACKER_CHECK_TIMEOUT`.

### Evidence

Each observation must have evidence from a page. The evidence is one or more unchanged excerpts, with a blank line between the excerpts. Each excerpt must occur in the text or the JSON-LD data of one page capture. A page capture has one URL and one time. The app ignores differences in whitespace.

The app rejects invented text, paraphrases and text from a different page capture. It does not use approximate matches. The model text and the model confidence are not evidence. Examine the size, the color and the delivery country of each result.

### Exact Products

For an exact product, the SKU identifies the product. The product URL and the product name can change. If the product URL does not work, the collector can use the navigation and the search field of the shop. A product with a similar name is not a match without SKU evidence.

### Shop Sale Notes

The first check for a shop and a delivery country can also look for published sale dates. This uses the same page limit. The app keeps the result in SQLite and shows it in **Shop sale notes**. Later checks of all searches for the same shop and delivery country use this note again.

A note has a page URL, a time and an unchanged excerpt. If the collector finds no sale dates on the pages that it reads, the note records `not_found`. If the shop blocks the collector, the app does not keep a note. Notes do not change schedules. A saved note is old information, not new evidence. This function is not a web search and not complete research.

### Token Usage

The worker output shows `model`, `effort` and `usage` for each finished check. The `usage` counts come from the provider through pi. The total is uncached input + cache read + cache write + output.

If `reported_calls` is less than `model_calls`, the counts are not complete. Counts that are not available do not show zero use. If pi sends all events, a rejected result keeps its counts. A process failure or a timeout can lose the counts. SQLite does not keep these counts. To keep them, save the worker output.

## Security

- Website text is untrusted data.
- The pi process gets no SMTP secrets and no administration tools.
- The model provider gets the page text and the search details.
- The app does not keep raw pi sessions.

CAUTION: Use the non-root container for isolation. The tool limits are not an operating-system sandbox.

CAUTION: Do not mount the Docker socket, a repository or a home directory in the container. Give the container only a dedicated pi profile that you trust.

## Email

Email is off by default. Email operates only when `DEAL_TRACKER_EMAIL_ENABLED` is `true` and you set the host, the sender and the recipient. The app is only an SMTP client. It does not support HTTP email APIs or SMTP OAuth.

| Setting | Default | Description |
| --- | --- | --- |
| `DEAL_TRACKER_EMAIL_ENABLED` | `false` | Set to `true` to send email. |
| `DEAL_TRACKER_SMTP_HOST` | None | The SMTP server of your email provider. |
| `DEAL_TRACKER_SMTP_PORT` | `587` | The SMTP port. For `tls`, the usual port is 465. |
| `DEAL_TRACKER_SMTP_SECURITY` | `starttls` | `starttls` or `tls`. There is no plain-text mode. |
| `DEAL_TRACKER_SMTP_USERNAME` | None | The SMTP user name, if your provider uses authentication. |
| `DEAL_TRACKER_SMTP_PASSWORD` | None | The SMTP password. It is necessary if you set a user name. |
| `DEAL_TRACKER_EMAIL_FROM` | None | The sender address. Use one ASCII address without a display name. |
| `DEAL_TRACKER_EMAIL_TO` | None | The recipient address. Use one ASCII address without a display name. |

### Results That Make an Email

Only a successful pi result for an active search can make an email. The result must be for the current version of the search.

- For an exact product, the result must agree with the search in SKU, delivery country and currency. If the search has a size or a color, the result must also agree with them. The result must show stock and a price at or below the item-price target. If the search has no item-price target, the result is for review.
- Some results are for review only. All shop-sale and discovery results are for review. These results make an email only if you select **Email new candidates that need review**.
- The **Shopping for** text is private context. The shop does not have to show it.
- Imported results and runner results make only previews.

The app never buys a product. It does not make claims about your personal taste.

### Outbox

The [outbox](deal_tracker/notifications.py) keeps a record of each email:

- It does not send an offer again at the same price or at a higher price.
- A lower price can make a new email.
- It validates a queued email again before delivery.

Each delivery attempt has a 45-second limit. The `sent` state means that the SMTP server accepted the email. It does not prove that the email arrived.

The app never repeats a failed or uncertain delivery automatically. For these deliveries, use these actions:

- **Retry delivery** puts the email in the queue again. The next batch sends it. For an uncertain delivery, you must first accept the risk of a duplicate email.
- **Mark received** closes the delivery and sends no email.
- **Dismiss delivery** closes the delivery and sends no email. The app does not send that offer again.

SQLite keeps all check results and delivery records. The UI shows only recent items. The app does not erase old records automatically.

### Start Email Delivery

1. Do the first checks of a real shop with email off.
2. Examine the evidence.
3. Use **Preview alerts** to make email previews.
4. Examine the email previews.
5. Set the email settings for your email provider.
6. Set `DEAL_TRACKER_EMAIL_ENABLED` to `true`.
7. Start the server and the worker again.

## Searches and Candidates

Each expanded search has its own ID and state. If you edit or archive the original search, the source snapshot of the expanded search does not change. To use a different mode, use **Expand search**. The app does not change the mode of a saved search.

If a check finds the same candidate again, the app updates the candidate and keeps your feedback. A search that you make with **Track exact product** keeps its source snapshot.

### Save Errors

API requests stop after 10 seconds. If a save fails or stops, the editor keeps your values. Then you can use **Cancel** or save again. A timeout does not prove that the save failed. Before you save again, use **Refresh searches** to see if the app saved the search. The app never repeats a save automatically.

The health endpoint is <http://127.0.0.1:8765/api/health>.

## Import and Local Commands

**Import result** accepts a JSON collection result. The dialog adds the current search version. `CollectionResult` and `Observation` in [models.py](deal_tracker/models.py) define the format. If you import the same request ID again, the app does not save it twice. Before the app saves observations, it validates the source scope, the mode, the times and the search version.

The [runner](deal_tracker/runner.py) can do a check with a trusted local command. It gives the command a search snapshot and the result schema on stdin. It validates the JSON on stdout and records failures. It limits the time and the output of the command. Only one runner command can operate on a database at a time. The runner does not replace the restricted pi collector.

CAUTION: Use the runner only with trusted commands. Its temporary directory and its reduced environment do not isolate the file system or the network.

To show the runner options:

```sh
.venv/bin/python -m deal_tracker.runner --help
```

## Backup and Restore

To make a consistent SQLite backup, give a new file name:

```sh
.venv/bin/python -m deal_tracker.runner backup data/backups/tracker-backup.sqlite3
```

The runner does not overwrite a file. By default, it uses `data/tracker.sqlite3` in the current directory. It does not read `DEAL_TRACKER_DB`. If your database has a different path, add `--database` before `backup`:

```sh
.venv/bin/python -m deal_tracker.runner --database /path/to/tracker.sqlite3 backup /path/to/new-backup.sqlite3
```

To restore a backup:

1. Stop the server, the worker and the timer.
2. Keep a copy of the current database.
3. Set `DEAL_TRACKER_DB` to the path of the backup file.
4. Start the server and the worker.
5. Before you enable email, examine the deliveries in the `pending` and `uncertain` states.

The runner operates on macOS and Linux. Do not put database backups or pi credentials in Git. Protect the pi credentials separately from the database backups.

## Frontend Development

1. Keep the server in operation on port 8765.
2. In a second terminal, start the Vite server:

   ```sh
   npm --prefix frontend run dev -- --port 5173
   ```

3. Open <http://127.0.0.1:5173/static/>.

Vite sends `/api` requests to the server. If the server uses a different port, set `DEAL_TRACKER_API` to the server URL. Do not use `vite preview` instead of the server. After you change the UI, build it again before you use port 8765.

## Tests

These commands start the tests, the linters and the UI build:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check deal_tracker tests
npm test
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run lint
```

`npm test` starts a real Chromium browser for the browser bridge tests. The tests do not connect to real shops, models or email providers.
