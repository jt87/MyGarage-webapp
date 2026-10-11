# Garage web app

A read-only website for this repo: browse tools with photos and specs, open manuals and other documents, follow buy links for consumables, view proof of purchase, print an insurance inventory, and read project plans (including a 3D viewer for STL files).

The repo stays the source of truth. The app clones it from GitHub and pulls every few minutes, so editing a README or adding a file on GitHub shows up on the site without redeploying anything.

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

On iPhone or iPad, open the site in Safari and use Share → Add to Home Screen for an app-style icon.

## Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `GIT_REPO_URL` | (none) | Repo to clone. Leave unset to read a folder mounted at `REPO_PATH` instead |
| `GIT_BRANCH` | `main` | Branch to follow |
| `GIT_TOKEN` | (none) | GitHub token for a private repo; sent as a header, never written to disk |
| `SYNC_INTERVAL` | `300` | Seconds between pulls (minimum 30). The footer's "Refresh now" pulls immediately |
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
