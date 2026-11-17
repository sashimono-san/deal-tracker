# Deploy Deal Tracker with Docker Compose

This document gives the procedure to operate Deal Tracker on a Linux server with Docker Compose and a daily systemd timer. Do the sections in sequence. Each section uses the results of the sections before it.

For the functions and the settings of the app, refer to [README.md](README.md).

The numbered setup below builds from source. For prebuilt GHCR images and release updates without a server checkout, use [Release Images](#15-release-images).

## Contents

1. [Status](#1-status)
2. [Components](#2-components)
3. [Requirements](#3-requirements)
4. [Get the code](#4-get-the-code)
5. [Make the Compose file](#5-make-the-compose-file)
6. [Make the seccomp profile](#6-make-the-seccomp-profile)
7. [Make the configuration file](#7-make-the-configuration-file)
8. [Build and start](#8-build-and-start)
9. [Open the UI](#9-open-the-ui)
10. [Log in to the model provider](#10-log-in-to-the-model-provider)
11. [Do the first check](#11-do-the-first-check)
12. [Optional: configure email](#12-optional-configure-email)
13. [Install the daily timer](#13-install-the-daily-timer)
14. [Operation](#14-operation)
15. [Release Images](#15-release-images)

## Conventions

| Term | Meaning |
| --- | --- |
| Server | The Linux computer that operates Deal Tracker. |
| Workstation | Your computer, with an SSH client and a web browser. |
| `<source-directory>` | The absolute path of the Deal Tracker checkout on the server, for example `/opt/deal-tracker`. |
| `<deployment-directory>` | The absolute path of the directory for the Compose file and the configuration, for example `/srv/deal-tracker`. |
| `<user>@<host>` | Your SSH login on the server. |
| `<value>` | A value that you supply. Do not type the angle brackets. |

Run the server commands in `<deployment-directory>`, unless a step gives a different directory.

A **CAUTION** identifies a risk to data, secrets or money. A CAUTION comes before the steps that it applies to.

## 1. Status

Use this procedure with these limits:

- The procedure was tested with Docker Desktop on macOS and Docker Compose 2.38. With a `linux/arm64` image, the tests covered the web health, a worker pass with collection off, the pi credential check, visible Chromium on the virtual display, the backup and restore commands and a real check. With a `linux/amd64` image under emulation, the tests covered the Python tests, the web health, visible Chromium on the virtual display, the backup command and a real check.
- These items are not tested: deployment on a Linux server, `amd64` hardware, the pi command `/login` in the container, email through a real SMTP server, and the systemd timer.
- Use a source build, or an image published by the [release workflow](.github/workflows/release.yml). Refer to [Release Images](#15-release-images).
- The app has no login. By default, it accepts only loopback host names. Use an SSH tunnel, or configure a private HTTPS reverse proxy as described in [section 9.1](#91-optional-private-https-reverse-proxy).
- The app does not send an alert when a check fails. The worker output and the UI show the result.
- The app limits the number of checks, not the cost.

For the status of the app, refer to [README.md, Status](README.md#status).

## 2. Components

| Component | Function |
| --- | --- |
| Image `deal-tracker:<commit-id>` | Compose builds the image on the server from the checkout. The image contains the app, the UI, pi 1.4.0, the Playwright MCP server, Chromium and Xvfb ([Dockerfile](Dockerfile)). |
| `web` service | Serves the UI and the API on port 8765 of the server loopback interface. It operates all the time. |
| `worker` service | Does one pass of checks and email, then stops. It starts only on request (Compose profile `job`). |
| Volume `data` | Contains the SQLite database and the app backups, at `/data`. `web` and `worker` share the volume. |
| Volume `pi-config` | Contains the pi login, at `/pi`. Only `worker` mounts the volume. |
| `.env` | Contains the settings and the secrets. |
| systemd timer | Starts one worker pass each day. |

At the end of the procedure, the deployment directory contains these items:

```text
<deployment-directory>/
├── compose.yaml
├── chromium-seccomp.json
├── .env
└── backups/
```

The systemd units are in `/etc/systemd/system/`.

## 3. Requirements

- A Linux server with systemd, Git, curl and Python 3. Section 6 uses Python 3.
- Docker Engine 25.0 or later. The health check uses `start_interval`, which Docker Engine 25.0 added ([Docker Engine 25.0 release notes](https://docs.docker.com/engine/release-notes/25.0/)).
- The Docker Compose plugin 2.20.2 or later ([Compose services reference](https://docs.docker.com/reference/compose-file/services/)).
- Free memory for the container limits: 512 MB for `web` and 2 GB for `worker`.
- Free disk space for two image tags and the build cache. One image tag uses approximately 2.2 GB.
- SSH access from the workstation to the server.
- An account at a model provider that pi supports ([pi: providers](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/providers.md)). Set a spending limit at the provider.
- Optional: an email account with SMTP access and an app password ([section 12](#12-optional-configure-email)).

## 4. Get the code

1. `git clone https://github.com/sashimono-san/deal-tracker.git <source-directory>`
2. `mkdir -p <deployment-directory>/backups`
3. `cd <deployment-directory>`

Keep the deployment directory outside the checkout. Then Git does not track your configuration and your secrets.

## 5. Make the Compose file

> **CAUTION:** The app has no login. Keep `127.0.0.1` in the `ports` line. Then only the server itself can connect to the UI.

> **CAUTION:** Do not mount the Docker socket, a repository or a home directory in the containers ([README.md, Security](README.md#security)).

1. Make the file `compose.yaml` with this content:

   ```yaml
   x-runtime: &runtime
     image: ${DEAL_TRACKER_IMAGE:-deal-tracker:local}
     pull_policy: never
     build:
       context: ${DEAL_TRACKER_SOURCE:?Set DEAL_TRACKER_SOURCE in .env}
       target: runtime
     env_file: .env
     environment:
       DEAL_TRACKER_DB: /data/tracker.sqlite3
       DEAL_TRACKER_PI_DIRECTORY: /pi
       DEAL_TRACKER_PI_EXECUTABLE: /usr/local/bin/pi
       DEAL_TRACKER_BROWSER_HEADLESS: ${DEAL_TRACKER_BROWSER_HEADLESS:-false}
     init: true
     read_only: true
     tmpfs:
       - /tmp:rw,nosuid,nodev,size=128m
     cap_drop: [ALL]
     security_opt: [no-new-privileges:true]
     pids_limit: 128
     logging:
       driver: json-file
       options:
         max-size: 10m
         max-file: "3"

   services:
     web:
       <<: *runtime
       ports:
         - "127.0.0.1:${DEAL_TRACKER_PORT:-8765}:8765"
       volumes:
         - data:/data
       mem_limit: 512m
       restart: unless-stopped
       healthcheck:
         test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).read()"]
         interval: 30s
         timeout: 5s
         start_period: 10s
         start_interval: 2s
         retries: 3

     worker:
       <<: *runtime
       profiles: [job]
       entrypoint:
         - xvfb-run
         - --auto-servernum
         - --server-args=-screen 0 1280x800x24 -nolisten tcp
       command: [python, -m, deal_tracker.worker, --once]
       volumes:
         - data:/data
         - pi-config:/pi
       pids_limit: 256
       security_opt:
         - no-new-privileges:true
         - seccomp=./chromium-seccomp.json
       mem_limit: 2g
       cpus: 2
       restart: "no"
       stop_grace_period: 150s

   volumes:
     data:
     pi-config:
   ```

The settings in the file have these functions:

| Setting | Function |
| --- | --- |
| `build`, `image`, `pull_policy` | Compose builds the image from `DEAL_TRACKER_SOURCE` and gives it the tag in `DEAL_TRACKER_IMAGE`. Compose never pulls the image from a registry. |
| `env_file` | Gives all values in `.env` to the containers as environment variables. |
| `environment` | Sets the paths in the containers. `DEAL_TRACKER_BROWSER_HEADLESS` is `false` by default. Then the browser uses the virtual display of the worker. |
| `read_only`, `tmpfs`, `cap_drop`, `security_opt` | The root file system is read-only. The containers can write only to their volumes and to `/tmp`. The processes have no Linux capabilities and cannot get more privileges. The image runs the app as the user `app` (UID 10001). |
| `ports` | Publishes the UI only on the loopback interface of the server. To use a different port, set `DEAL_TRACKER_PORT` in `.env`. |
| `healthcheck` | Docker sets `web` to healthy when `/api/health` responds. `docker compose up --wait` uses this state. |
| `entrypoint` of `worker` | `xvfb-run` starts Xvfb with an X11 authorization file and sets `DISPLAY` and `XAUTHORITY`. Then it starts the command. It removes the display when the command stops. X11 TCP access is off. Each `docker compose run ... worker <command>` gets a display ([README.md, Linux Without a Screen](README.md#linux-without-a-screen)). |
| `pids_limit` of `worker` | Chromium starts more processes than a limit of 128 permits. |
| `seccomp` of `worker` | Lets the Chromium sandbox operate. Section 6 makes this file. |
| `mem_limit`, `cpus` | Limit the resources of each service. |

## 6. Make the seccomp profile

The worker runs Chromium as a user without Linux capabilities. Then the Chromium sandbox needs the system calls `clone`, `setns`, `unshare` and `chroot`. The [Docker default seccomp profile](https://github.com/moby/profiles/blob/main/seccomp/default.json) permits these calls only with some capabilities or with argument limits. The profile of this section is the Docker default profile with these four calls added. The Chromium sandbox stays on. [Playwright: Docker](https://playwright.dev/docs/docker) gives the same approach for `clone`, `setns` and `unshare`.

1. Download the Docker default profile:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json -o docker-default-seccomp.json
   ```

2. Add the four system calls:

   ```sh
   python3 - <<'EOF'
   import json
   with open("docker-default-seccomp.json") as source:
       profile = json.load(source)
   profile["syscalls"].insert(0, {"names": ["clone", "setns", "unshare", "chroot"], "action": "SCMP_ACT_ALLOW", "args": [], "includes": {}, "excludes": {}})
   with open("chromium-seccomp.json", "w") as target:
       json.dump(profile, target, indent=2)
   EOF
   ```

3. `rm docker-default-seccomp.json`

The profile does not add capabilities. Only `worker` uses it. `web` uses the Docker default profile.

## 7. Make the configuration file

> **CAUTION:** `.env` will contain secrets, for example the SMTP password. Keep its mode at 600. Do not share the output of `docker compose config`, because it shows the values. Users with access to Docker on the server can also see the values with `docker inspect`.

1. `git -C <source-directory> rev-parse --short HEAD`
2. Write down the output. It is the `<commit-id>`.
3. Make the file `.env` with this content:

   ```dotenv
   DEAL_TRACKER_SOURCE=<source-directory>
   DEAL_TRACKER_IMAGE=deal-tracker:<commit-id>
   ```

4. `chmod 600 .env`
5. `docker compose config --quiet`
6. Make sure that the command shows no error.

The sections that follow add more settings to `.env`. For all the settings, refer to [README.md, Configure Collection](README.md#configure-collection) and [README.md, Email](README.md#email).

Compose reads `.env` for the variables in `compose.yaml`. It also gives all values in `.env` to `web` and `worker`. The worker reads the values at the start of each pass. `web` reads them only when Compose makes a new container. Thus, after each change to `.env`, run this command:

```sh
docker compose up -d --no-build --force-recreate --wait web
```

## 8. Build and start

1. `docker compose build web`
2. `docker compose up -d --no-build --wait web`
3. `curl -fsS http://127.0.0.1:8765/api/health`
4. Make sure that the output contains `"status":"ok"`, `"collector":"unconfigured"`, `"scheduler":"disabled"` and `"email":"dry_run_only"`.
5. Do a worker pass. Collection is off, thus the pass does no checks:

   ```sh
   docker compose --profile job run --rm -T --no-deps worker
   ```

6. Make sure that the output starts with `{"outcome": "disabled", "processed": 0`.
7. Start visible Chromium on the virtual display:

   ```sh
   docker compose --profile job run --rm -T --no-deps -e HOME=/tmp worker node -e '
   const { chromium } = require("playwright");
   (async () => {
     const browser = await chromium.launch({ headless: false });
     const page = await browser.newPage();
     const agent = await page.evaluate(() => navigator.userAgent);
     console.log(JSON.stringify({ display: process.env.DISPLAY, xauthority: Boolean(process.env.XAUTHORITY), headless: agent.includes("HeadlessChrome") }));
     await browser.close();
   })();
   '
   ```

8. Make sure that the output shows a display number, `"xauthority":true` and `"headless":false`.

The build makes an image for the architecture of the server. The worker uses the same image tag. Steps 5 and 7 use the seccomp profile and the limits of the worker. Step 7 does not contact a shop or a model provider.

## 9. Open the UI

1. On the workstation, start an SSH tunnel:

   ```sh
   ssh -N -L 8765:127.0.0.1:8765 <user>@<host>
   ```

2. Open <http://127.0.0.1:8765>.
3. To close the tunnel, push Ctrl+C in the terminal.

If port 8765 is in use on the workstation, use a different local port. For example, use `-L 18765:127.0.0.1:8765` and open <http://127.0.0.1:18765>. If you set `DEAL_TRACKER_PORT`, use that port after `127.0.0.1:` in the `-L` value.

By default, the app accepts only the host names `localhost`, `127.0.0.1` and `[::1]`. It also rejects a change from a different origin ([api.py](deal_tracker/api.py)). Keep this default when you use the SSH tunnel.

### 9.1 Optional: private HTTPS reverse proxy

> **CAUTION:** The app has no login. Restrict access to a trusted LAN or VPN. The public-origin setting does not provide authentication. Do not expose the app directly to the internet.

1. Set one browser-facing HTTPS origin in `.env`, for example:

   ```dotenv
   DEAL_TRACKER_PUBLIC_ORIGIN=https://deals.example.com
   ```

   Do not include credentials, a path, a query or a fragment. Include the port if the browser uses a port other than 443 ([settings.py](deal_tracker/settings.py)).

2. Connect the reverse proxy to the web container through a shared private Docker network. Keep the published app port bound to loopback.
3. Configure the proxy to preserve the browser's `Host` and `Origin` headers. For Caddy, use `reverse_proxy <web-network-alias>:8765`; do not rewrite either header. Forwarded headers do not add allowed hosts or origins ([api.py](deal_tracker/api.py)).
4. Recreate `web`: `docker compose up -d --no-build --force-recreate --wait web`.
5. Open the HTTPS address. Create or edit a search to verify that changes work as well as page loads.

The configured hostname is added to the allowed hosts. Its HTTPS origin remains valid when the proxy uses HTTP to reach the container. Loopback health checks and the SSH tunnel remain available ([api.py](deal_tracker/api.py)).

## 10. Log in to the model provider

> **CAUTION:** Type credentials only in your own terminal. Do not put tokens, API keys or the contents of `auth.json` in chat, in Git or in notes.

1. Start pi in the worker container:

   ```sh
   docker compose --profile job run --rm --no-deps -e PI_CODING_AGENT_DIR=/pi -e HOME=/tmp worker pi --no-session
   ```

2. In pi, type `/login`.
3. Select the provider and the method: OAuth or API key.
4. Do the steps that pi shows. If the browser cannot send the OAuth result back to pi, paste the final redirect URL or the authorization code into pi.
5. Type `/quit`.
6. Make sure that pi has the login. Use the pi provider ID, for example `anthropic`:

   ```sh
   docker compose --profile job run --rm -T --no-deps -e PI_CODING_AGENT_DIR=/pi -e HOME=/tmp worker pi auth check --provider <provider-id>
   ```

7. Make sure that the output is `ready`. If the output is `not_ready`, do steps 1 to 5 again.
8. Show the models that the login can use:

   ```sh
   docker compose --profile job run --rm -T --no-deps -e PI_CODING_AGENT_DIR=/pi -e HOME=/tmp worker pi --list-models
   ```

9. Add these lines to `.env`. Use a model ID from step 8:

   ```dotenv
   DEAL_TRACKER_PI_PROVIDER=<provider-id>
   DEAL_TRACKER_PI_MODEL=<model-id>
   DEAL_TRACKER_PI_THINKING=low
   ```

pi saves the credential in `auth.json` in its profile directory ([pi: providers](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/providers.md)). `PI_CODING_AGENT_DIR=/pi` puts the profile in the `pi-config` volume. The app gives the same directory to pi for each check. The app does not give provider environment variables, for example `ANTHROPIC_API_KEY`, to pi ([pi_harness.py](deal_tracker/pi_harness.py)). Thus, also for an API key, use `/login`.

The `pi-config` volume is a dedicated pi profile for this deployment. An OAuth refresh can change the files in it. There is no default provider and no default model. The thinking level is `off`, `low` or `medium`, and the model must support the level ([README.md, Configure Collection](README.md#configure-collection)). The provider `github-copilot` with the model `gpt-6.1-sol` and `medium` thinking passed real checks in the container.

## 11. Do the first check

> **CAUTION:** A check sends the page text and the search details to the model provider. It uses provider quota ([README.md, Security](README.md#security)).

1. Add these lines to `.env`:

   ```dotenv
   DEAL_TRACKER_COLLECTION_ENABLED=true
   DEAL_TRACKER_SCHEDULER_ENABLED=true
   ```

2. `docker compose up -d --no-build --force-recreate --wait web`
3. `curl -fsS http://127.0.0.1:8765/api/health`
4. Make sure that the output contains `"collector":"pi"` and `"scheduler":"external"`. If `collector_issues` is not empty, correct the settings that it identifies.
5. In the UI, open a search. If there is no search, make one.
6. Click **Request check**.
7. Do the check:

   ```sh
   docker compose --profile job run --rm -T --no-deps worker python -m deal_tracker.worker --limit 1
   ```

8. Make sure that the output contains `"outcome": "ok"` and `"processed": 1`. If the output contains `"outcome": "collector_disabled"`, correct the settings in `issues`.
9. In `jobs`, examine the `outcome` of the check. `success` means that the check returned observations with page evidence.
10. In the UI, examine the evidence.

**Request check** only puts a check in the queue. Step 7 does the check. A check of a paused search never sends email ([README.md, Manual Checks](README.md#manual-checks)). No pass starts automatically before section 13.

If the outcome is `blocked`, `failed` or `timed_out`, the UI shows the diagnostics of the check. These are possible causes:

- The shop loads page resources from a different host. Add that origin to `DEAL_TRACKER_BROWSER_RESOURCE_ORIGINS` ([README.md, Browser](README.md#browser)).
- The shop does not accept requests from the server. A virtual display does not prevent this ([README.md, Linux Without a Screen](README.md#linux-without-a-screen)).
- The pi login is not valid. Do section 10 again.
- The diagnostics are `pi did not return exactly one structured collection result`. The model stopped without a result. Request the check again. In the container tests, this occurred one time in three checks.

To use the headless mode of Chromium, set `DEAL_TRACKER_BROWSER_HEADLESS=true` in `.env`. Some shops reject the headless browser ([README.md, Status](README.md#status)).

## 12. Optional: configure email

The worker sends email through the SMTP server of an existing email account. The app is an SMTP client, not a mail server. It supports a user name and a password with `starttls` or `tls`. It does not support SMTP OAuth or plain text ([README.md, Email](README.md#email)).

> **CAUTION:** Use an app password, not the main password of the account. Keep the app password only in `.env`.

### 12.1 Examine the alert previews

1. In the UI, use **Preview alerts** ([README.md, Start Email Delivery](README.md#start-email-delivery)).
2. Examine the email previews.

### 12.2 Get the provider values

| Value | iCloud Mail | Gmail |
| --- | --- | --- |
| SMTP host | `smtp.mail.me.com` | `smtp.gmail.com` |
| Port and security | `587` and `starttls` | `587` and `starttls` |
| User name | The full email address | The full email address |
| Password | An [app-specific password](https://support.apple.com/en-us/102654) | An [app password](https://support.google.com/accounts/answer/185833?hl=en) |
| Account requirement | Two-factor authentication | 2-Step Verification |
| Reference | [iCloud Mail server settings](https://support.apple.com/en-us/102525) | [Gmail SMTP settings](https://support.google.com/mail/answer/7126229?hl=en) |

- For a different provider, get the SMTP host, the port and the security mode from the documentation of the provider. For `tls`, the usual port is 465.
- A change of the Apple Account password revokes all app-specific passwords. Then make a new app password and update `.env`.
- Google does not recommend app passwords in most cases.

### 12.3 Set the values

1. Add these lines to `.env`:

   ```dotenv
   DEAL_TRACKER_SMTP_HOST=<smtp-host>
   DEAL_TRACKER_SMTP_PORT=587
   DEAL_TRACKER_SMTP_SECURITY=starttls
   DEAL_TRACKER_SMTP_USERNAME=<email-address>
   DEAL_TRACKER_SMTP_PASSWORD='<app-password>'
   DEAL_TRACKER_EMAIL_FROM=<email-address>
   DEAL_TRACKER_EMAIL_TO=<recipient-address>
   ```

Compose uses a value in single quotes literally, also when it contains `$` ([Compose: variable interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)). Each address setting takes one ASCII address without a display name. The recipient can be the sender address. Email stays off, because `DEAL_TRACKER_EMAIL_ENABLED` is `false` by default.

### 12.4 Send a test message

1. Run this command:

   ```sh
   docker compose --profile job run --rm -T --no-deps worker python -c '
   from email.message import EmailMessage
   from deal_tracker.notifications import smtp_handoff
   from deal_tracker.settings import Settings
   settings = Settings.from_environment()
   message = EmailMessage()
   message["From"] = settings.email_from
   message["To"] = settings.email_to
   message["Subject"] = "Deal Tracker SMTP test"
   message.set_content("SMTP test from the Deal Tracker server.")
   print(smtp_handoff(settings, message.as_bytes()))
   '
   ```

2. Make sure that the output is `{'status': 'sent', 'error': ''}`.
3. Make sure that the message is in the inbox of the recipient.

`sent` means that the SMTP server accepted the message. It does not prove delivery ([README.md, Outbox](README.md#outbox)). If the status is `failed`, the error gives only the exception type, for example `SMTP delivery failed (SMTPAuthenticationError)` ([notifications.py](deal_tracker/notifications.py)). Then examine the user name, the app password, the port and the security mode.

### 12.5 Turn on email

1. Add this line to `.env`:

   ```dotenv
   DEAL_TRACKER_EMAIL_ENABLED=true
   ```

2. `docker compose up -d --no-build --force-recreate --wait web`
3. `curl -fsS http://127.0.0.1:8765/api/health`
4. Make sure that the output contains `"email":"smtp"`.

Only a successful result for an active search can make an email ([README.md, Results That Make an Email](README.md#results-that-make-an-email)).

## 13. Install the daily timer

Do this section after a successful check in section 11.

1. `command -v docker`
2. Make sure that the output is `/usr/bin/docker`. If the output is different, use it in the `ExecStart` and `ExecStopPost` lines of step 3.
3. Make the file `/etc/systemd/system/deal-tracker-worker.service` with a text editor and `sudo`. Use this content, with your `<deployment-directory>`:

   ```ini
   [Unit]
   Description=Deal Tracker check pass
   Requires=docker.service
   After=docker.service network-online.target
   Wants=network-online.target

   [Service]
   Type=oneshot
   WorkingDirectory=<deployment-directory>
   ExecStart=/usr/bin/docker compose --profile job run --rm -T --no-deps --name deal-tracker-job worker
   ExecStopPost=-/usr/bin/docker stop --time 150 deal-tracker-job
   ExecStopPost=-/usr/bin/docker rm -f deal-tracker-job
   TimeoutStartSec=30min
   TimeoutStopSec=3min
   ```

4. Make the file `/etc/systemd/system/deal-tracker-worker.timer` with this content:

   ```ini
   [Unit]
   Description=Daily Deal Tracker check pass

   [Timer]
   OnCalendar=*-*-* 08:00:00 UTC
   Persistent=true
   RandomizedDelaySec=30

   [Install]
   WantedBy=timers.target
   ```

5. `sudo systemctl daemon-reload`
6. `sudo systemctl enable --now deal-tracker-worker.timer`
7. `systemctl list-timers deal-tracker-worker.timer`
8. Make sure that the next start is at 08:00 UTC.

To do a test pass now:

9. `sudo systemctl start --no-block deal-tracker-worker.service`
10. `journalctl -fu deal-tracker-worker.service`
11. Make sure that the journal shows the JSON output of the pass. Push Ctrl+C to stop the output.

A pass does the queued checks first. Then it does the due checks of the active searches, up to the daily limit. Then it sends the queued email ([README.md, Worker](README.md#worker)). To include a search in the passes, use **Activate monitoring** ([README.md, Activate a Search](README.md#activate-a-search)).

The units have these functions:

- `TimeoutStartSec=30min` stops a pass that takes too long. The default limits permit 12 checks of a maximum of 120 seconds each, which is 24 minutes. If you increase `DEAL_TRACKER_DAILY_CHECK_LIMIT` or `DEAL_TRACKER_CHECK_TIMEOUT`, also increase `TimeoutStartSec`.
- The `ExecStopPost` lines stop and remove the container `deal-tracker-job` when systemd stops the service.
- `Persistent=true` starts one catch-up pass when the server was off at the scheduled time. `RandomizedDelaySec=30` adds a random delay of a maximum of 30 seconds ([systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)).
- To use a different time, change `OnCalendar`. Then run `sudo systemctl daemon-reload` and `sudo systemctl restart deal-tracker-worker.timer`.

A database lock prevents two passes at the same time ([README.md, Worker](README.md#worker)). A different scheduler, for example cron, can run the `ExecStart` command in `<deployment-directory>`.

## 14. Operation

Run these commands in `<deployment-directory>`.

### 14.1 Logs and status

| Purpose | Command |
| --- | --- |
| `web` log | `docker compose logs --tail 100 web` |
| Results of the last passes | `journalctl -u deal-tracker-worker.service -n 50 --no-pager` |
| Next pass | `systemctl list-timers deal-tracker-worker.timer` |
| Health | `curl -fsS http://127.0.0.1:8765/api/health` |
| Disk use | `docker system df` |

The health output also shows `worker_last_seen` and `checks_today` ([api.py](deal_tracker/api.py)). The worker exits with code 0 also when a check fails ([worker.py](deal_tracker/worker.py)). Thus systemd does not report a failed check. Examine the UI or the journal.

### 14.2 Do a pass now

1. `sudo systemctl start --no-block deal-tracker-worker.service`
2. `journalctl -fu deal-tracker-worker.service`

### 14.3 Make a backup

1. `name=$(date -u +%Y%m%dT%H%M%SZ).sqlite3`
2. Make the backup in the data volume:

   ```sh
   docker compose exec -T web python -m deal_tracker.runner --database /data/tracker.sqlite3 backup "/data/backups/$name"
   ```

3. Make sure that the output is `Backup created: /data/backups/<name>`.
4. `docker compose cp "web:/data/backups/$name" backups/`
5. On the workstation, copy the backup off the server:

   ```sh
   scp <user>@<host>:<deployment-directory>/backups/<name> .
   ```

The runner makes a consistent SQLite copy while `web` operates. It does not overwrite a file. Without `--database`, the runner uses `data/tracker.sqlite3` in the current directory ([README.md, Backup and Restore](README.md#backup-and-restore)). In the container, that path is on the read-only file system, and the command fails.

A backup on the same server does not protect from a server failure. Step 5 copies the backup to a different computer. The backup does not contain the pi login. To make a new login, do section 10 again.

### 14.4 Restore a backup

> **CAUTION:** Keep collection and email off until you have examined the restored searches and the outbox.

1. `sudo systemctl stop deal-tracker-worker.timer`
2. `systemctl is-active deal-tracker-worker.service`
3. If the output is `activating`, a pass operates. Wait until it stops.
4. In `.env`, set `DEAL_TRACKER_COLLECTION_ENABLED=false` and `DEAL_TRACKER_EMAIL_ENABLED=false`.
5. Put the backup file in `<deployment-directory>/backups/`.
6. `docker compose stop web`
7. Keep a copy of the current database:

   ```sh
   docker compose run --rm --no-deps -T --entrypoint cp web /data/tracker.sqlite3 "/data/tracker.sqlite3.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
   ```

8. Copy the backup into the volume:

   ```sh
   docker compose run --rm --no-deps -T -v "$PWD/backups:/restore:ro" --entrypoint cp web /restore/<name> /data/tracker.sqlite3
   ```

9. `docker compose up -d --no-build --force-recreate --wait web`
10. In the UI, examine the searches. Examine the deliveries in the `pending` and `uncertain` states ([README.md, Backup and Restore](README.md#backup-and-restore)).
11. In `.env`, set collection and email back to `true`.
12. `docker compose up -d --no-build --force-recreate --wait web`
13. `sudo systemctl start deal-tracker-worker.timer`

Steps 7 and 8 use one-off containers with UID 10001. Thus the database keeps the correct owner. UID 10001 must be able to read the `backups` directory and the backup file.

### 14.5 Update to a new version

1. `sudo systemctl stop deal-tracker-worker.timer`
2. Do steps 2 and 3 of [section 14.4](#144-restore-a-backup).
3. Make a backup ([section 14.3](#143-make-a-backup)).
4. `grep '^DEAL_TRACKER_IMAGE=' .env`
5. Write down the current tag. A rollback uses it.
6. `git -C <source-directory> pull --ff-only`
7. `git -C <source-directory> rev-parse --short HEAD`
8. In `.env`, set `DEAL_TRACKER_IMAGE=deal-tracker:<commit-id>` with the new commit ID.
9. Compare your files with sections 5, 6 and 13 of the new `<source-directory>/DEPLOYMENT.md`. Apply the changes.
10. `docker compose build web`
11. `docker compose up -d --no-build --wait web`
12. `curl -fsS http://127.0.0.1:8765/api/health`
13. If you changed the unit files, run `sudo systemctl daemon-reload`.
14. `sudo systemctl start deal-tracker-worker.timer`

Keep the current tag and the previous tag. To remove an older tag, run `docker image rm deal-tracker:<old-commit-id>`.

### 14.6 Roll back

> **CAUTION:** A rollback changes only the image. If the new version changed the database, also restore the backup of the update ([section 14.4](#144-restore-a-backup)). The database has a schema version ([store.py](deal_tracker/store.py)).

1. `sudo systemctl stop deal-tracker-worker.timer`
2. In `.env`, set `DEAL_TRACKER_IMAGE` to the previous tag.
3. `docker compose up -d --no-build --wait web`
4. `curl -fsS http://127.0.0.1:8765/api/health`
5. `sudo systemctl start deal-tracker-worker.timer`

### 14.7 Renew the pi login

If checks fail with an authentication error, do section 10 again.

### 14.8 Stop the deployment

> **CAUTION:** Do not use `docker compose down --volumes`. This command removes the database and the pi login.

1. `sudo systemctl disable --now deal-tracker-worker.timer`
2. `docker compose stop web`

## 15. Release Images

### Publish a Release

1. Commit and push the release workflow and the application changes to the repository. Do not include runtime data or credentials.
2. Publish a stable GitHub Release with a tag in the form `vX.Y.Z`, for example `v0.1.0`, at the intended application commit. A tag push alone does not publish an image; prereleases are skipped.
3. Wait for [Publish release image](.github/workflows/release.yml). It runs backend/frontend checks, then publishes `ghcr.io/sashimono-san/deal-tracker:<release-tag>` for `linux/amd64` and `linux/arm64`, with build provenance and an SBOM. It does not publish a `latest` tag.
4. Copy the full digest reference from the workflow's job summary. Use this reference for deployment; release tags can be moved, but a digest identifies fixed content.

The workflow uses the repository's `GITHUB_TOKEN` with package-write permission. It needs no personal registry token, pi login or SMTP secret. Publishing does not deploy to a server. Source: [GitHub Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

### Use a Release Image

New GHCR packages are private by default, even for public repositories. For anonymous pulls, explicitly make the package public in GitHub's package settings. Otherwise, log in on the server with a personal access token (classic) limited to `read:packages`. Enter it only in your own terminal; never put it in Compose, Git or chat.

For the generic Compose file in this guide, remove `build` from `x-runtime` and omit `DEAL_TRACKER_SOURCE`. Keep all mounts, resource limits, browser security settings and `pull_policy: never`. Set this value in the operator environment file:

```dotenv
DEAL_TRACKER_IMAGE=ghcr.io/sashimono-san/deal-tracker@sha256:<digest>
```

Use the full digest from the workflow, not the placeholder. The server needs no application checkout in this mode. Keep its deployment configuration, data, seccomp profile and pi profile.

For a new deployment, follow the remaining setup steps but skip the source clone and build commands. Pull before starting:

```sh
docker compose pull --policy always web worker
docker compose up -d --no-build --wait web
```

For updates, stop the timer and wait for any worker pass to finish, make a database backup, and record the previous image reference. Change the digest, pull, recreate `web` and inspect health and a bounded check before restarting the timer. Do not use the source-pull/build steps of section 14.5 in release mode. Keep the prior image locally for rollback; schema changes may also require the prior database backup.

Pi credentials remain runtime state in the worker's dedicated writable profile. Do not bake them into images, upload them to Actions, or mount a general coding-agent home directory. Use `/login` or install an approved credential in the profile with the container user's ownership.
