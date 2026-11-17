# Run Deal Tracker with Docker Compose

Use the checked-in files for local testing and Linux deployments. Build locally with [Start Locally](#start-locally), or deploy a prebuilt image with [Release Images](#release-images). Then configure collection and, if needed, a scheduler.

Run commands in the directory containing [compose.yaml](compose.yaml). Replace values in angle brackets with your own values.

## Runtime Files

| File | Purpose |
| --- | --- |
| [compose.yaml](compose.yaml) | Web service, one-shot worker, persistent volumes and resource limits. |
| [.env.example](.env.example) | Settings to copy to a private `.env`. Collection and email start off. |
| [compose.release.yaml](compose.release.yaml) | Disables source builds for prebuilt images. |
| [deploy/chromium-seccomp.json](deploy/chromium-seccomp.json) | Permits Chromium's sandbox to run in the worker. |
| [Worker service](deploy/deal-tracker-worker.service) and [timer](deploy/deal-tracker-worker.timer) | Optional daily scheduling on Linux with systemd. |

The image includes Python, the built UI, pi, Chromium and Xvfb. No host Python, Node or display is needed. Use Docker Engine 25 or later and Docker Compose 2.24.4 or later. Allow 512 MB for `web`, 2 GB for `worker`, and disk space for the current and previous images plus build cache.

The worker uses a virtual display with Chromium's sandbox enabled. Its bundled seccomp profile is the [Docker default profile](https://github.com/moby/profiles/blob/main/seccomp/default.json) with `clone`, `setns`, `unshare` and `chroot` permitted for the non-root browser. There is no profile-generation step. Keep this profile; do not replace it with `seccomp=unconfined` or disable Chromium's sandbox. See [Playwright: Docker](https://playwright.dev/docs/docker).

## Start Locally

From a new checkout:

```sh
cp .env.example .env
chmod 600 .env
docker compose config --quiet
docker compose build web
docker compose up -d --no-build --wait web
curl -fsS http://127.0.0.1:8765/api/health
```

Open <http://127.0.0.1:8765>. If the port is in use, change `DEAL_TRACKER_PORT` in `.env` and use that port in the URL and health command. Initially, health shows `"status":"ok"`, `"collector":"unconfigured"`, `"scheduler":"disabled"` and `"email":"dry_run_only"`.

Compose keeps the database in its `data` volume and the worker's dedicated pi profile in `pi-config`. These are separate from a native development database in `data/`. Keep `COMPOSE_PROJECT_NAME` unchanged after setup; changing it selects different volumes. Do not overwrite an existing operator `.env` with the example.

> **CAUTION:** The app has no login. Keep its published port on `127.0.0.1`. Do not mount the Docker socket, a repository or a home directory into either service.

> **CAUTION:** Keep `.env` private, mode 600 and out of Git. Do not share `docker compose config` or container environment output; they can contain secrets. Use `docker compose config --quiet` to validate without printing values.

After changing `.env`, recreate `web`. Each new worker pass reads the current settings:

```sh
docker compose up -d --no-build --force-recreate --wait web
```

## Configure Collection

> **CAUTION:** Checks send page text and search details to the model provider and use its quota. Set a spending limit at the provider; the app limits check counts, not cost. Enter credentials only in your own terminal.

1. Start pi in the dedicated worker profile:

   ```sh
   docker compose --profile job run --rm --no-deps -e PI_CODING_AGENT_DIR=/pi -e HOME=/tmp worker pi --no-session
   ```

2. Use `/login`, select your provider and complete its instructions. Use this for API keys as well as OAuth. Then use `/quit`.
3. Check the login and list available models:

   ```sh
   docker compose --profile job run --rm -T --no-deps -e PI_CODING_AGENT_DIR=/pi -e HOME=/tmp worker pi auth check --provider <provider-id>
   docker compose --profile job run --rm -T --no-deps -e PI_CODING_AGENT_DIR=/pi -e HOME=/tmp worker pi --list-models
   ```

   Expect `ready` from the auth check. If it says `not_ready`, repeat the login. To renew credentials later, use the same procedure.

4. Set these values in `.env`, using your provider ID and an available model ID:

   ```dotenv
   DEAL_TRACKER_PI_PROVIDER=<provider-id>
   DEAL_TRACKER_PI_MODEL=<model-id>
   DEAL_TRACKER_PI_THINKING=low
   DEAL_TRACKER_COLLECTION_ENABLED=true
   DEAL_TRACKER_SCHEDULER_ENABLED=true
   ```

5. Recreate `web` and check health. Expect `"collector":"pi"`, `"scheduler":"external"` and no `collector_issues`.
6. In the UI, make or open a paused search and select **Request check**. Process one check:

   ```sh
   docker compose --profile job run --rm -T --no-deps worker python -m deal_tracker.worker --limit 1
   ```

7. Inspect the job outcome and the page evidence in the UI. A worker exit code of 0 does not prove a successful check. A paused search does not send email.

These flags permit checks; they do not start a timer. The worker does one pass and exits. If a shop blocks the browser or needs additional resource origins, see [Browser](README.md#browser). A virtual display does not guarantee shop access from your server.

The profile stays in its writable volume across image updates, including OAuth refreshes. Do not bake credentials into images or copy a general coding-agent profile into the container. For all collection settings, see [Configure Collection](README.md#configure-collection).

## Server Access

Use the same files on Linux. For a source build, keep the checkout in a permanent directory such as `/opt/deal-tracker` and use [Start Locally](#start-locally). For a deployment without application source, use [Release Images](#release-images).

From your workstation, open an SSH tunnel:

```sh
ssh -N -L 18765:127.0.0.1:8765 <user>@<host>
```

Open <http://127.0.0.1:18765>. Adjust the server port if you changed `DEAL_TRACKER_PORT`. Close the tunnel with Ctrl+C.

For a private HTTPS reverse proxy, require authentication and restrict access to a trusted LAN or VPN. Set `DEAL_TRACKER_PUBLIC_ORIGIN=https://deals.example.com` in `.env`, with the exact browser origin and no path. Connect the proxy through a private Docker network or the server loopback port. Preserve the browser's `Host` and `Origin` headers; do not rewrite them. Forwarded headers do not add allowed hosts or origins ([api.py](deal_tracker/api.py)). Recreate `web`, then verify both a read and a save through the proxy.

The public-origin setting is not authentication. Keep the app port bound to loopback; the SSH tunnel and local health checks remain available.

<a id="12-optional-configure-email"></a>

## Optional Email

First inspect **Preview alerts** in the UI. Get SMTP settings and an app password from your email provider. The app supports authenticated `starttls` or `tls`, not SMTP OAuth or plain text. See [Email](README.md#email) for all settings.

Keep `DEAL_TRACKER_EMAIL_ENABLED=false` while setting these values in `.env`:

```dotenv
DEAL_TRACKER_SMTP_HOST=<smtp-host>
DEAL_TRACKER_SMTP_PORT=587
DEAL_TRACKER_SMTP_SECURITY=starttls
DEAL_TRACKER_SMTP_USERNAME=<email-address>
DEAL_TRACKER_SMTP_PASSWORD='<app-password>'
DEAL_TRACKER_EMAIL_FROM=<email-address>
DEAL_TRACKER_EMAIL_TO=<recipient-address>
```

Use the port and security mode from your provider. Each address must be one ASCII address without a display name. Single quotes preserve literal characters such as `$` in the password ([Compose interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)).

> **CAUTION:** Use an app password, not the main account password. The following command sends one real test message, even while automatic email is disabled.

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

Expect `{'status': 'sent', 'error': ''}` and confirm receipt in the recipient's inbox. `sent` means SMTP acceptance, not delivery. Only then set `DEAL_TRACKER_EMAIL_ENABLED=true`, recreate `web` and confirm `"email":"smtp"` in health.

## Schedule Checks

Install a scheduler only after a real check succeeds on the target host. Activate the searches that scheduled passes should check.

### systemd

On Linux with systemd, install the supplied [service](deploy/deal-tracker-worker.service) and [timer](deploy/deal-tracker-worker.timer):

```sh
command -v docker
sudo cp deploy/deal-tracker-worker.service deploy/deal-tracker-worker.timer /etc/systemd/system/
sudoedit /etc/systemd/system/deal-tracker-worker.service
```

Set `WorkingDirectory` to your absolute deployment directory. If Docker is not `/usr/bin/docker`, change its path in `ExecStart` and both `ExecStopPost` lines. Then enable the timer:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now deal-tracker-worker.timer
systemctl list-timers deal-tracker-worker.timer
```

The example runs at 08:00 UTC with up to 30 seconds of random delay and a catch-up pass after downtime. Edit `OnCalendar` to change it, then reload systemd and restart the timer. Increase the service's 30-minute timeout if you increase the daily check budget beyond its default 12 checks of up to 120 seconds each.

### Cron Alternative

Use cron instead of systemd, not alongside it. For example, with the correct directory and Docker path in a user's crontab:

```cron
0 8 * * * cd /opt/deal-tracker && /usr/bin/docker compose --profile job run --rm -T --no-deps worker
```

This uses the cron host's time zone and has no systemd catch-up or service timeout. The user must have Docker access. A database lock prevents overlapping worker passes.

## Operate

| Purpose | Command |
| --- | --- |
| Health | `curl -fsS http://127.0.0.1:8765/api/health` |
| Web log | `docker compose logs --tail 100 web` |
| Run one pass | `docker compose --profile job run --rm -T --no-deps worker` |
| Worker journal (systemd) | `journalctl -u deal-tracker-worker.service -n 50 --no-pager` |
| Run via systemd | `sudo systemctl start --no-block deal-tracker-worker.service` |
| Next systemd pass | `systemctl list-timers deal-tracker-worker.timer` |

Check the UI or worker output for failed checks; the app does not send failure alerts, and a failed check does not make the worker exit with an error.

### Back Up

```sh
mkdir -p backups
name=$(date -u +%Y%m%dT%H%M%SZ).sqlite3
docker compose exec -T web python -m deal_tracker.runner --database /data/tracker.sqlite3 backup "/data/backups/$name"
docker compose cp "web:/data/backups/$name" backups/
```

The runner makes a consistent SQLite copy while `web` runs. Keep the explicit `--database` path; the runner's default path is not the container database. Copy the backup off the server, for example from your workstation:

```sh
scp <user>@<host>:<deployment-directory>/backups/<name> .
```

The database backup does not contain the pi login. A replacement deployment needs a new login or an approved credential installed in its dedicated writable profile with UID/GID 10001 ownership.

### Before Maintenance

Before an update, rollback or restore, stop your scheduler and let all worker passes finish, including manually started passes. With systemd:

```sh
sudo systemctl stop deal-tracker-worker.timer
systemctl is-active deal-tracker-worker.service
```

Continue only when the worker is inactive. Make a backup and record the current image reference. Keep a private copy of `.env` and the previous image locally for rollback. Do not change the Compose project name or remove its volumes.

<a id="144-restore-a-backup"></a>

### Restore a Backup

> **CAUTION:** Do [Before Maintenance](#before-maintenance) first. Set `DEAL_TRACKER_COLLECTION_ENABLED=false` and `DEAL_TRACKER_EMAIL_ENABLED=false` in `.env`. Keep them off until you inspect the restored searches and outbox.

Put the backup in `backups/`. UID 10001 in the container must be able to read that directory and file. Stop `web`, preserve the current database and copy the backup into the volume:

```sh
docker compose stop web
docker compose run --rm --no-deps -T --entrypoint cp web /data/tracker.sqlite3 "/data/tracker.sqlite3.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose run --rm --no-deps -T -v "$PWD/backups:/restore:ro" --entrypoint cp web /restore/<name> /data/tracker.sqlite3
docker compose up -d --no-build --force-recreate --wait web
```

Inspect searches and deliveries in the `pending` and `uncertain` states. Restore the previous collection and email settings only after review, recreate `web`, and restart your scheduler. See [Backup and Restore](README.md#backup-and-restore).

### Update or Roll Back

Do [Before Maintenance](#before-maintenance) first.

- **Release image:** set the new digest in `.env`, then use the pull and start commands in [Release Images](#release-images).
- **Source build:** pull the intended application revision, set `DEAL_TRACKER_IMAGE` to a new local tag and run `docker compose build web`. Keep the old image tag.
- **Rollback:** restore the previous image reference and recreate `web`. If the schema changed, also restore the prior database backup.

Recreate `web` with `docker compose up -d --no-build --force-recreate --wait web`. Check health, browser access and one bounded check before restarting your scheduler. Review runtime file changes with each update; reinstall changed systemd units, reload systemd and restart an edited timer after validation. Never replace the operator environment or pi profile during an image update.

### Stop

Disable your scheduler, let any worker finish, then stop `web`. With systemd:

```sh
sudo systemctl disable --now deal-tracker-worker.timer
docker compose stop web
```

Do not use `docker compose down --volumes`; it removes the database and pi login.

<a id="15-release-images"></a>

## Release Images

For a source-free deployment, copy [compose.yaml](compose.yaml), [compose.release.yaml](compose.release.yaml), [.env.example](.env.example) and [deploy/chromium-seccomp.json](deploy/chromium-seccomp.json) into a permanent deployment directory, retaining their relative paths. Copy the systemd units too if needed. No application checkout or Dockerfile is needed there.

For a new installation, copy `.env.example` to `.env` and set its mode to 600. Keep an existing operator environment unchanged except for the required image settings. Set:

```dotenv
COMPOSE_FILE=compose.yaml:compose.release.yaml
DEAL_TRACKER_IMAGE=ghcr.io/sashimono-san/deal-tracker@sha256:<digest>
```

Use the full digest from a successful release workflow, not the placeholder. If `COMPOSE_FILE` already includes a proxy or another overlay, retain it and add the release override. The override removes both build definitions; it keeps volumes, resource limits and `pull_policy: never`.

Wait for the image to be published. Public GHCR packages allow anonymous pulls. For private packages, log in on the server with a personal access token (classic) limited to `read:packages`. Enter it only in your own terminal, never in Compose, Git or chat. New packages are private by default; maintainers must explicitly enable public access for anonymous pulls ([GHCR documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)).

For updates, do [Before Maintenance](#before-maintenance) before changing the digest. Pull explicitly and start without building:

```sh
docker compose config --quiet
docker compose pull --policy always web worker
docker compose up -d --no-build --force-recreate --wait web
curl -fsS http://127.0.0.1:8765/api/health
```

Configure collection and verify a bounded check before enabling the scheduler. Scheduled passes use the cached image; they never pull updates. Publishing an image does not deploy it.

### Publish a Release

1. Commit and push the intended application changes and [release workflow](.github/workflows/release.yml), without runtime data or credentials.
2. Publish a stable GitHub Release tagged `vX.Y.Z` at that commit. A tag push alone does not publish an image; prereleases are skipped.
3. Wait for **Publish release image**. It tests the backend and frontend, then publishes `linux/amd64` and `linux/arm64` images with provenance and an SBOM. There is no `latest` tag.
4. Use the full digest reference in the workflow's job summary for deployment.

The workflow uses `GITHUB_TOKEN` with package-write permission. It needs no personal registry token, pi login or SMTP secret. Credentials remain runtime state outside the image.
