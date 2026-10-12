# MyGarage web app

A self-hosted website for your garage: browse tools with photos and specs, open manuals and other documents, follow buy links for consumables, view proof of purchase, print an insurance inventory, and read project plans (including a 3D viewer for STL files). From your phone you can log maintenance, add photos and receipts, edit a tool's details, and add new tools; each change is committed to your data repo. It works offline too, and edits made without a connection are sent when you're back.

This repo is only the app. It contains no garage data. Your tools and projects live in a separate git repo of Markdown files and PDFs that you own (a **data repo**); start one from the sample at [jt87/MyGarage-dataset](https://github.com/jt87/MyGarage-dataset). That repo stays the source of truth. The server keeps a clone of it on your TrueNAS as its cache and renders every page from that, so browsing never waits on GitHub and keeps working if GitHub is down.

### How it stays current (and inside GitHub's free limits)

Updates reach the server three ways, cheapest first:

| Trigger | What happens | How often |
|---------|--------------|-----------|
| GitHub push webhook | Pulls the new commit right away | Once per push to `main` |
| Someone opens a page | If the last check is over 5 minutes old, one `git ls-remote` (a single small request returning the latest commit ID), then a pull only if it changed | At most 288 a day, and only while the site is being used |
| Background timer | Same check | Every 30 minutes (48 a day) |

That's at most about 340 tiny checks a day, plus one download per real change. GitHub doesn't publish a limit for git traffic like this, and its REST API allows 5,000 requests an hour, so this is a small fraction of anything GitHub would notice. A hard cap of 60 checks an hour backs it up, and the last check time is saved to disk so restarts don't cause a burst. The home screen shows how many checks ran today.

GitHub Actions runs in this app repo only, so editing tools and projects in your data repo never uses Actions minutes. Pull requests run the tests (about 1 minute); merges build the image once (about 2 minutes, x86 only). That's a few minutes a month against the 2,000 free minutes for private repos. Make the image package **public** (step 1 below) so it doesn't count against the 500 MB private package storage limit.

### Design

The interface follows Apple's Human Interface Guidelines so it looks like a stock app once added to the Home Screen. It uses Apple's system colors (systemBlue tint, grouped backgrounds, semantic label grays) with automatic light and dark mode and Increase Contrast support. Icons are the open-source [Feather](https://feathericons.com) set (MIT license, see `app/static/feather-LICENSE.txt`), with stroke weights tuned to sit beside SF Pro text. Text is SF Pro at the iOS Dynamic Type sizes and follows your iPhone's text size setting. Screens use inset grouped lists, large titles that collapse into the nav bar on scroll, a bottom tab bar on iPhone and iPad portrait, and a sidebar on iPad landscape and Mac. Parts open an action sheet with store links, Share opens the iOS share sheet, and the shopping list uses Reminders-style checkmarks. Manuals open in a built-in reader with a page indicator, because Safari can't scroll a PDF embedded in a page and a Home Screen app has no back button out of a raw PDF. iPhone `.heic` photos work as tool photos.

## What's where

| Page | What it shows |
|------|---------------|
| MyGarage (home) | Totals, photo shelf of every tool, recent maintenance, sync status |
| Tools | Every tool grouped by category, with a filter |
| Tool page | Photo gallery, specs, purchase and insurance details, proof of purchase, consumables with store links, manuals and documents, notes, maintenance log, the projects that use it |
| Shopping | Every consumable in one checklist; tap a part for store links (checkmarks saved on that device) |
| Projects | Project write-ups, files (PDF viewer, STL 3D viewer), tools used |
| Insurance | Printable inventory with model, serial, paid and replacement values, receipt status; CSV download |
| Reference | Pages from `reference/` (reached from the home screen) |
| Search | Tools, parts, projects and reference pages. On iPad or Mac, press `/` |

### How the content is read

The app reads the formats shown in [MyGarage-dataset](https://github.com/jt87/MyGarage-dataset), so copy that repo's layout and keep the formats. It reads the inventory table in the main `README.md` for category, paid and replacement value, and each tool's `README.md` for the title (`# Brand Model — Name`), the spec table, and the `## Consumables & parts` and `## Maintenance log` tables. Other sections show up as written.

Files in a tool folder are sorted by name: images become the photo gallery (`photo.jpg` first, nameplates last), PDFs with "manual" or "guide" in the name are manuals, other PDFs are documents, and anything whose name starts with or contains **`receipt`**, `invoice`, `proof`, `order` or `bill-of-sale` is proof of purchase (kept out of the gallery).

In the consumables table, **bold** part numbers (e.g. `Acme **TS-547**`) become store searches for that exact part. Store buttons appear for the stores named in the "Where to buy" column (Amazon, Home Depot, Lowe's, Walmart, eBay, Harbor Freight, Menards), plus a general web shopping search.

## Deploy on TrueNAS SCALE

You need TrueNAS SCALE 24.10 (Electric Eel) or newer, which runs apps with Docker.

1. **Make the image pullable.** Merging this to `main` runs the GitHub Action that builds `ghcr.io/<owner>/mygarage` (for this repo, `ghcr.io/jt87/mygarage`). On GitHub, open your profile → Packages → `mygarage` → Package settings, and set visibility to **Public**. The image holds only the app code, never your garage data. (If you'd rather keep it private, add a GHCR registry credential in TrueNAS instead.)
2. **Create a GitHub token.** GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Repository access: only your data repo. Permissions: Contents → **Read and write**, so edits made in the app can be saved (choose Read-only, and set `READ_ONLY: "true"`, if you only want to browse). A public repo still needs a token for editing. Pick a long expiry and set a reminder; when it expires the site keeps showing the saved copy and the home screen says it can't reach GitHub.
3. **Create a dataset** for the app's data, e.g. `tank/apps/mygarage`, and give the `apps` user (uid 568) full control of it. It holds a clone of the repo, a thumbnail cache and your login settings (`config/`). Only `config/` matters if you ever rebuild it.
4. **Install.** Apps → Discover Apps → ⋮ (top right) → Install via YAML. Name it `mygarage` and paste [`deploy/truenas-compose.yaml`](deploy/truenas-compose.yaml), filling in each `CHANGE_ME`: the token, a webhook secret (run `openssl rand -hex 32` in a Mac Terminal) and the dataset path.
5. **Create your login.** On your home Wi-Fi, open `http://<truenas-ip>:8088`. Like Sonarr and Radarr, there's no default login: the first screen asks you to create a username and password. For safety this screen only works from your home network, so nobody can claim the app if it's reachable from the internet before you've set it up.
6. **Add the GitHub webhook** for instant updates, once the site is on your domain. On GitHub open your **data repo's** Settings → Webhooks → Add webhook. Set the payload URL to `https://garage.example.com/webhook/github`, the content type to `application/json`, and the secret to the same value as `WEBHOOK_SECRET`. Choose "Just the push event". Webhooks are free and don't count against any limit. If GitHub can't reach your server (for example, it's only on Tailscale), skip this; changes then show up within 5 minutes of opening the site. If Cloudflare Access or another login sits in front of the site, let `/webhook/github` through, since it checks GitHub's signature itself.

To update the app later, open it in Apps and choose Update (or Pull Image, then restart) after a new image is built.

## Editing and offline use (1.1)

**Editing.** A tool page has **Edit** (name, category, paid and replacement values, the details table, parts), **Log Maintenance**, **Add Photo**, **Add Receipt** and **Add Manual**. Tools has a **+** for a new tool, and Edit → Edit README.md opens the whole file for anything else. Each save is one commit to your data repo, written in the same Markdown formats the app reads, by "<your username> via MyGarage". The server pulls the latest commit, applies your change, and pushes, so edits made on GitHub in the meantime aren't lost. If you and someone else changed the same field, nothing is saved and the app asks you to reload. Photos keep their original quality (iPhone HEIC is fine), up to `MAX_UPLOAD_MB` each (default 50; GitHub refuses files over 100 MB).

**Offline.** Every page and photo is kept on the device and refreshed whenever the data repo changes. Manuals, receipts and STL files are kept once you've opened them; Settings → This Device → Save All Manuals & Files gets them all at once before you head somewhere without signal. While offline a banner says you're looking at the saved copy. Logging out removes everything saved on that device.

**Edits without a connection** are kept on the device (photos included) and listed under Waiting to Send on the home screen and the tool's page. They're sent automatically the next time the app is open and online; iOS doesn't let web apps send in the background. Each edit carries an ID, so one that's sent twice is only saved once.

Offline mode needs HTTPS (your domain through a reverse proxy, or Tailscale's HTTPS names), because browsers only allow offline storage on secure sites. Over plain `http://<truenas-ip>:8088` everything else, including editing, still works.

## Put it on your domain

Point a subdomain such as `garage.example.com` at the app through whatever already fronts your domain:

| You use | Do this |
|---------|---------|
| Nginx Proxy Manager | Add a proxy host for `garage.example.com` → `http://<truenas-ip>:8088`, request an SSL certificate, turn on Force SSL |
| Cloudflare Tunnel | Add a public hostname `garage.example.com` → `http://<truenas-ip>:8088` |
| Traefik / Caddy | Route the hostname to port 8088 like your other apps |
| Tailscale only | No proxy needed; use `http://<truenas-name>:8088` from your devices |

## Login and security

This works like the Security section in Sonarr and Radarr (Settings → General → Security). In MyGarage it's on the home screen under **Settings**.

| Setting | Options | Notes |
|---------|---------|-------|
| Authentication | **Login Page** (recommended), Browser Popup, External | Login Page works best from the Home Screen. Browser Popup is HTTP Basic auth: Safari's built-in prompt. External turns off MyGarage's own login; use it only when a reverse proxy (Authelia, Authentik, Cloudflare Access) already requires one. |
| Required | **Always**, Not on Home Network | "Not on Home Network" is the *arr "Disabled for Local Addresses": devices on your Wi-Fi skip the login, and anything arriving through your domain still has to log in. |
| Username and password | Set on first run | Stored as a salted PBKDF2-SHA256 hash (600,000 rounds), never as plain text. Changing either one logs out your other devices. |
| API key | Shown in Settings | For scripts or iOS Shortcuts: send an `X-Api-Key` header (or `?apikey=`). It can read everything and make edits (a Shortcut could POST to `/tools/<folder>/log` with `date` and `what`), but can't change security settings. |
| Log Out All Other Devices | In Settings | For a lost phone or a shared computer. |

It also fixes some weak spots the *arr apps have had:
- **Local address checks can't be faked.** In 2026 Sonarr had a bug (CVE-2026-30975) where "Disabled for Local Addresses" could be bypassed with a forged header. MyGarage decides "local" from the actual network connection, and treats anything that came through a proxy as remote. Never port-forward 8088 on your router; put the site behind your reverse proxy instead.
- **First-run setup only from home**, as described in step 5.
- **Guessing is slowed down.** Each wrong password takes the server a moment to check. After 10 failures in 15 minutes the device is locked out, and after 100 failures from anywhere, the login pauses for 15 minutes.
- **Cookies** are HttpOnly and SameSite=Lax, and marked Secure when the site is reached over HTTPS. Forms posted from other websites are refused.

**Forgot the password?** In TrueNAS open Apps → mygarage → Shell (or run `docker exec -it mygarage sh`), then run `python -m app.auth reset`. Open MyGarage from your home network and create a new login. `python -m app.auth status` shows the current settings.

Optional: setting `AUTH_USERNAME` and `AUTH_PASSWORD` in the YAML creates the first login instead of the setup screen. They're ignored once a login exists, so remove them afterwards.

On iPhone or iPad, open the site in Safari and use Share → Add to Home Screen. It then opens full screen with its own icon, like a stock app. In a manual, the Share button hands the PDF to the iOS share sheet for Save to Files, Print, AirDrop or Books.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GIT_REPO_URL` | (none) | Your data repo to clone. Leave unset to read a folder mounted at `REPO_PATH` instead |
| `GIT_BRANCH` | `main` | Branch to follow |
| `GIT_TOKEN` | (none) | GitHub token (Contents: Read and write for editing); sent as a header, never written to disk |
| `WEBHOOK_SECRET` | (none) | Enables `/webhook/github` for instant updates on push |
| `FRESHNESS_SECONDS` | `300` | A page view checks GitHub first if the last check is older than this |
| `SYNC_INTERVAL` | `1800` | Seconds between background checks (minimum 60). "Check for Updates" on the home screen checks right away |
| `CONFIG_PATH` | `/data/config/auth.json` | Where the login settings are kept |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | (none) | Optional first login instead of the setup screen; ignored once one exists |
| `READ_ONLY` | `false` | `true` hides every way to change the data repo from the app |
| `MAX_UPLOAD_MB` | `50` | Largest photo or file you can add (GitHub's hard limit is 100) |
| `GIT_AUTHOR_EMAIL` | `mygarage@users.noreply.github.com` | Email on commits made from the app |
| `GITHUB_REPO` | (none) | Your data repo as `owner/name`; shows "Edit" links to the README on GitHub |
| `SITE_TITLE` | `MyGarage` | App name: large title, first tab, Home Screen label |
| `REPO_PATH` / `CACHE_PATH` | `/data/repo`, `/data/cache` | Where the clone and thumbnails live |

## Run it locally

```
git clone https://github.com/jt87/MyGarage-dataset.git ../MyGarage-dataset   # or your own data repo
pip install -r requirements.txt
REPO_PATH=../MyGarage-dataset CACHE_PATH=/tmp/garage-cache uvicorn app.main:app --reload
```

Then open <http://localhost:8000>. Tests: `pip install pytest httpx && python -m pytest -q`. They build a small made-up data repo in a temp folder (`tests/fixture.py`), so they need no real data.
