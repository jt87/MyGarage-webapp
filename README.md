# MyGarage web app

A read-only website for this repo: browse tools with photos and specs, open manuals and other documents, follow buy links for consumables, view proof of purchase, print an insurance inventory, and read project plans (including a 3D viewer for STL files).

The repo stays the source of truth. The server keeps a clone of it on your TrueNAS as its cache and renders every page from that, so browsing never waits on GitHub and keeps working if GitHub is down.

### How it stays current (and inside GitHub's free limits)

Updates reach the server three ways, cheapest first:

| Trigger | What happens | How often |
|---------|--------------|-----------|
| GitHub push webhook | Pulls the new commit right away | Once per push to `main` |
| Someone opens a page | If the last check is over 5 minutes old, one `git ls-remote` (a single small request returning the latest commit ID), then a pull only if it changed | At most 288 a day, and only while the site is being used |
| Background timer | Same check | Every 30 minutes (48 a day) |

That's at most about 340 tiny checks a day, plus one download per real change. GitHub doesn't publish a limit for git traffic like this, and its REST API allows 5,000 requests an hour, so this is a small fraction of anything GitHub would notice. A hard cap of 60 checks an hour backs it up, and the last check time is saved to disk so restarts don't cause a burst. The home screen shows how many checks ran today.

GitHub Actions only runs when `webapp/` changes, never for tool or project edits. Pull requests run the tests (about 1 minute); merges build the image once (about 2 minutes, x86 only). That's a few minutes a month against the 2,000 free minutes for private repos. Make the image package **public** (step 1 below) so it doesn't count against the 500 MB private package storage limit.

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

The app follows the conventions in `CLAUDE.md`. It reads the inventory table in the main `README.md` for category, paid and replacement value, and each tool's `README.md` for the title (`# Brand Model — Name`), the spec table, and the `## Consumables & parts` and `## Maintenance log` tables. Other sections show up as written.

Files in a tool folder are sorted by name: images become the photo gallery (`photo.jpg` first, nameplates last), PDFs with "manual" or "guide" in the name are manuals, other PDFs are documents, and anything whose name starts with or contains **`receipt`**, `invoice`, `proof`, `order` or `bill-of-sale` is proof of purchase (kept out of the gallery).

In the consumables table, **bold** part numbers (e.g. `Delta **22-547**`) become store searches for that exact part. Store buttons appear for the stores named in the "Where to buy" column (Amazon, Home Depot, Lowe's, Walmart, eBay, Harbor Freight, Menards), plus a general web shopping search.

## Deploy on TrueNAS SCALE

You need TrueNAS SCALE 24.10 (Electric Eel) or newer, which runs apps with Docker.

1. **Make the image pullable.** Merging this to `main` runs the GitHub Action that builds `ghcr.io/jt87/mygarage`. On GitHub, open your profile → Packages → `mygarage` → Package settings, and set visibility to **Public**. The image holds only the app code, never your garage data. (If you'd rather keep it private, add a GHCR registry credential in TrueNAS instead.)
2. **Create a read-only GitHub token.** GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Repository access: only `jt87/MyGarage`. Permissions: Contents → Read-only. Pick a long expiry and set a reminder; when it expires the site keeps showing the saved copy and the home screen says it can't reach GitHub.
3. **Create a dataset** for the app's data, e.g. `tank/apps/mygarage`, and give the `apps` user (uid 568) full control of it. It only holds a clone of the repo and a thumbnail cache, so it doesn't need snapshots.
4. **Install.** Apps → Discover Apps → ⋮ (top right) → Install via YAML. Name it `mygarage` and paste [`deploy/truenas-compose.yaml`](deploy/truenas-compose.yaml), filling in each `CHANGE_ME`: the token, a password, a random `SECRET_KEY` (run `openssl rand -hex 32` in a Mac Terminal) and the dataset path.
5. Open `http://<truenas-ip>:8088` and log in.
6. **Add the GitHub webhook** for instant updates, once the site is on your domain. On GitHub open the repo's Settings → Webhooks → Add webhook. Set the payload URL to `https://garage.example.com/webhook/github`, the content type to `application/json`, and the secret to the same value as `WEBHOOK_SECRET`. Choose "Just the push event". Webhooks are free and don't count against any limit. If GitHub can't reach your server (for example, it's only on Tailscale), skip this; changes then show up within 5 minutes of opening the site. If Cloudflare Access or another login sits in front of the site, let `/webhook/github` through, since it checks GitHub's signature itself.

To update the app later, open it in Apps and choose Update (or Pull Image, then restart) after a new image is built.

## Put it on your domain

Point a subdomain such as `garage.example.com` at the app through whatever already fronts your domain:

| You use | Do this |
|---------|---------|
| Nginx Proxy Manager | Add a proxy host for `garage.example.com` → `http://<truenas-ip>:8088`, request an SSL certificate, turn on Force SSL |
| Cloudflare Tunnel | Add a public hostname `garage.example.com` → `http://<truenas-ip>:8088` |
| Traefik / Caddy | Route the hostname to port 8088 like your other apps |
| Tailscale only | No proxy needed; use `http://<truenas-name>:8088` from your devices |

Once it's only reached over HTTPS, set `COOKIE_SECURE: "true"` in the YAML.

**Keep the login on** if the site is reachable from the internet: the repo has serial numbers and, once you add them, receipts. If your proxy already does authentication (Cloudflare Access, Authelia, Authentik), you can remove `AUTH_PASSWORD` to rely on that instead. Wrong passwords are limited to 10 tries per 15 minutes per IP.

On iPhone or iPad, open the site in Safari and use Share → Add to Home Screen. It then opens full screen with its own icon, like a stock app. In a manual, the Share button hands the PDF to the iOS share sheet for Save to Files, Print, AirDrop or Books.

## Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `GIT_REPO_URL` | (none) | Repo to clone. Leave unset to read a folder mounted at `REPO_PATH` instead |
| `GIT_BRANCH` | `main` | Branch to follow |
| `GIT_TOKEN` | (none) | GitHub token for a private repo; sent as a header, never written to disk |
| `WEBHOOK_SECRET` | (none) | Enables `/webhook/github` for instant updates on push |
| `FRESHNESS_SECONDS` | `300` | A page view checks GitHub first if the last check is older than this |
| `SYNC_INTERVAL` | `1800` | Seconds between background checks (minimum 60). "Check for Updates" on the home screen checks right away |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | (none) | Login. No password means no login screen |
| `SECRET_KEY` | random | Signs the login cookie; set it so restarts don't sign you out |
| `COOKIE_SECURE` | `false` | `true` once served over HTTPS only |
| `GITHUB_REPO` | (none) | `owner/name`; shows "Edit" links to the README on GitHub |
| `SITE_TITLE` | `MyGarage` | App name: large title, first tab, Home Screen label |
| `REPO_PATH` / `CACHE_PATH` | `/data/repo`, `/data/cache` | Where the clone and thumbnails live |

## Run it locally

```
cd webapp
pip install -r requirements.txt
REPO_PATH=.. CACHE_PATH=/tmp/garage-cache uvicorn app.main:app --reload
```

Then open <http://localhost:8000>. Tests: `pip install pytest httpx && python -m pytest -q`.
