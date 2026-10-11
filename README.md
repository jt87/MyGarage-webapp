# Garage web app

A read-only website for this repo: browse tools with photos and specs, open manuals and other documents, follow buy links for consumables, view proof of purchase, print an insurance inventory, and read project plans (including a 3D viewer for STL files).

The repo stays the source of truth, and the site always shows its latest commit. Before rendering a page, the app asks GitHub whether the branch has moved (one quick `git ls-remote`, at most every 10 seconds) and pulls first if it has. A background pull every minute and an optional GitHub webhook keep that pull ready ahead of time. Editing a README or adding a file on GitHub shows up on the next page load, with no redeploy. If GitHub can't be reached, the site keeps showing the last copy and says so in the footer.

It's built for Safari on iPhone first, then iPad and Mac: a bottom tab bar on iPhone, swipeable edge-to-edge photos, a built-in PDF reader (Safari can't scroll PDFs inside a page, and a Home Screen app has no back button out of a raw PDF), 44-point tap targets, safe-area support for the notch and home indicator, and light/dark mode. iPhone `.heic` photos work as tool photos. When the app comes back to the foreground after a minute or more, it reloads so it never shows a stale page.

## What's where

| Page | What it shows |
|------|---------------|
| Home | Totals, every tool, active projects, recent maintenance |
| Tools | Filter by category, sort by name or value |
| Tool page | Photo gallery, specs, purchase and insurance details, proof of purchase, consumables with store links, manuals and documents, notes, maintenance log, the projects that use it |
| Shopping | Every consumable in one list with store search links; tick what you need (saved on that device) |
| Projects | Project write-ups, files (PDF viewer, STL 3D viewer), tools used |
| Insurance | Printable inventory with model, serial, paid and replacement values, receipt status; CSV download |
| Reference | Pages from `reference/` |

Search (top right, or press `/`) covers tools, projects, reference pages and consumables.

### How the content is read

The app follows the conventions in `CLAUDE.md`. It reads the inventory table in the main `README.md` for category, paid and replacement value, and each tool's `README.md` for the title (`# Brand Model — Name`), the spec table, and the `## Consumables & parts` and `## Maintenance log` tables. Other sections show up as written.

Files in a tool folder are sorted by name: images become the photo gallery (`photo.jpg` first, nameplates last), PDFs with "manual" or "guide" in the name are manuals, other PDFs are documents, and anything whose name starts with or contains **`receipt`**, `invoice`, `proof`, `order` or `bill-of-sale` is proof of purchase (kept out of the gallery).

In the consumables table, **bold** part numbers (e.g. `Acme **TS-547**`) become store searches for that exact part. Store buttons appear for the stores named in the "Where to buy" column (Amazon, Home Depot, Lowe's, Walmart, eBay, Harbor Freight, Menards), plus a general web shopping search.

## Deploy on TrueNAS SCALE

You need TrueNAS SCALE 24.10 (Electric Eel) or newer, which runs apps with Docker.

1. **Make the image pullable.** Merging this to `main` runs the GitHub Action that builds `ghcr.io/jt87/mygarage`. On GitHub, open your profile → Packages → `mygarage` → Package settings, and set visibility to **Public**. The image holds only the app code, never your garage data. (If you'd rather keep it private, add a GHCR registry credential in TrueNAS instead.)
2. **Create a read-only GitHub token.** GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Repository access: only `your-name/your-data-repo`. Permissions: Contents → Read-only. Pick a long expiry and set a reminder; when it expires the site keeps showing the last copy and the footer says the sync failed.
3. **Create a dataset** for the app's data, e.g. `tank/apps/garage`, and give the `apps` user (uid 568) full control of it. It only holds a clone of the repo and a thumbnail cache, so it doesn't need snapshots.
4. **Install.** Apps → Discover Apps → ⋮ (top right) → Install via YAML. Name it `garage` and paste [`deploy/truenas-compose.yaml`](deploy/truenas-compose.yaml), filling in each `CHANGE_ME`: the token, a password, a random `SECRET_KEY` (run `openssl rand -hex 32` in a Mac Terminal) and the dataset path.
5. Open `http://<truenas-ip>:8088` and sign in.
6. **Optional, for instant updates:** once the site is on your domain, add a webhook on GitHub (repo → Settings → Webhooks → Add webhook). Payload URL: `https://garage.example.com/webhook/github`, content type `application/json`, secret: the same value as `WEBHOOK_SECRET`, events: just the push event. Without it, pages still check GitHub on every load; the webhook only saves that first page view a moment of waiting.

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

**Keep the sign-in on** if the site is reachable from the internet: the repo has serial numbers and, once you add them, receipts. If your proxy already does authentication (Cloudflare Access, Authelia, Authentik), you can remove `AUTH_PASSWORD` to rely on that instead. Wrong passwords are limited to 10 tries per 15 minutes per IP.

On iPhone or iPad, open the site in Safari and use Share → Add to Home Screen. It then opens full screen like an app, with its own icon. Manuals open in the built-in reader; "Open in Safari viewer" hands the PDF to Safari for searching, sharing or saving to Files.

## Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `GIT_REPO_URL` | (none) | Repo to clone. Leave unset to read a folder mounted at `REPO_PATH` instead |
| `GIT_BRANCH` | `main` | Branch to follow |
| `GIT_TOKEN` | (none) | GitHub token for a private repo; sent as a header, never written to disk |
| `FRESHNESS_SECONDS` | `10` | A page view checks GitHub for a new commit if the last check is older than this. `0` checks on every page view |
| `SYNC_INTERVAL` | `60` | Seconds between background pulls (minimum 30). "Refresh now" pulls immediately |
| `WEBHOOK_SECRET` | (none) | Enables `/webhook/github` for instant pulls on push |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | (none) | Sign-in. No password means no sign-in |
| `SECRET_KEY` | random | Signs the login cookie; set it so restarts don't sign you out |
| `COOKIE_SECURE` | `false` | `true` once served over HTTPS only |
| `GITHUB_REPO` | (none) | `owner/name`; shows "Edit" links to the README on GitHub |
| `SITE_TITLE` | `Garage` | Name in the header |
| `REPO_PATH` / `CACHE_PATH` | `/data/repo`, `/data/cache` | Where the clone and thumbnails live |

## Run it locally

```
cd webapp
pip install -r requirements.txt
REPO_PATH=.. CACHE_PATH=/tmp/garage-cache uvicorn app.main:app --reload
```

Then open <http://localhost:8000>. Tests: `pip install pytest httpx && python -m pytest -q`.
