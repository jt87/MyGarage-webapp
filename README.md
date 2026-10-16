# MyGarage web app

A self-hosted website for your garage: browse tools with photos and specs, open manuals and other documents, follow buy links for consumables, view proof of purchase, print an insurance inventory, and read project plans (including a 3D viewer for STL files). From your phone you can log maintenance, add photos and receipts, edit a tool's details, and add new tools; each change is committed to your data repo. It works offline too, and edits made without a connection are sent when you're back.

This repo is only the app. It contains no garage data. Your tools and projects are a folder of Markdown files and PDFs with a git history of every change (a **data repo**; see the sample at [jt87/MyGarage-dataset](https://github.com/jt87/MyGarage-dataset)). The first-run screen asks where it lives: **on your server**, where it never needs another account, optionally backed up to a private GitHub repo; or **in a GitHub repo** you own, where the server keeps a copy and renders every page from it, so browsing never waits on GitHub and keeps working if GitHub is down.

### How it stays current with GitHub (and inside GitHub's free limits)

This applies when GitHub is connected; a garage kept only on the server never talks to GitHub.

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

1. **Create a dataset** for the app, e.g. `tank/apps/mygarage`, and give the `apps` user (uid 568) full control of it. It holds your garage (if you keep it on the server), a thumbnail cache, and your settings (`config/`).
2. **Install.** Apps → Discover Apps → ⋮ (top right) → Install via YAML. Name it `mygarage`, paste [`deploy/truenas-compose.yaml`](deploy/truenas-compose.yaml), and change the one `CHANGE_ME`: the dataset path.
3. **Open it on your home Wi-Fi** at `http://<truenas-ip>:8088` and follow the two setup screens:
   - **Create your login.** Like Sonarr and Radarr there's no default login. For safety this screen only works from your home network, so nobody can claim the app if it's reachable from the internet before you've set it up.
   - **Choose where your garage lives.** *On This Server* keeps everything in the dataset, with a history of every change, and needs no other accounts. *On This Server, Backed Up to GitHub* does the same and also sends every change to a private GitHub repo. *GitHub as the Primary Copy* makes a GitHub repo the source and the server keeps a copy. The two GitHub choices ask for the repo (`owner/name`) and a token; see [Using GitHub](#using-github). Then start empty, or with a few made-up example tools to see how it looks. You can switch later in Settings → Storage.

That's it. Add it to your iPhone's Home Screen (Share → Add to Home Screen), and see [Put it on your domain](#put-it-on-your-domain) to use it away from home and offline.

To update the app later, open it in Apps and choose Update (or Pull Image, then restart).

### Using GitHub

GitHub is optional, and can be either the backup or the primary copy:

| | This server primary, GitHub backup | GitHub primary |
|---|---|---|
| Where edits are saved first | This server, then sent to GitHub | GitHub, then copied to the server |
| If GitHub is down | Nothing changes; edits go up when it's back | Browsing works from the saved copy; edits wait on the device |
| Edits made on GitHub | Brought in | Brought in |
| History on the server | All of it | Latest version only |

**Switching** is in Settings → Storage and asks for your password:

| From | Button | What happens |
|---|---|---|
| This server only | Connect GitHub Backup | Everything goes up to an empty GitHub repo; every change after that follows |
| This server only or with a backup | Make GitHub the Primary Copy | Everything is sent to GitHub first; refuses (and changes nothing) if GitHub can't be reached or doesn't match |
| GitHub primary | Make This Server the Primary Copy | GitHub's full history comes down first, and GitHub stays connected as the backup |
| Backup connected | Disconnect GitHub Backup | The server stops sending; nothing is deleted on GitHub |
| A plain folder (1.0) | Keep History on This Server | The folder becomes a garage with history, so a backup can be connected |

If a GitHub garage already exists, *On This Server, Backed Up to GitHub* on the setup screen brings all of it to the server and keeps it backed up there.

Either way you need a **fine-grained token**: GitHub → Settings → Developer settings → Fine-grained tokens → Generate, access to only your data repo, Contents: **Read and write**. Pick a long expiry and set a reminder; replace it in Settings → Storage when it runs out. For a new garage, create an **empty private repo** (don't tick "Add a README") and the app fills it; a repo that already holds a garage is used as is.

**Instant updates (optional).** Settings → Storage shows a webhook secret. On GitHub open the data repo's Settings → Webhooks → Add webhook: payload URL `https://garage.example.com/webhook/github`, content type `application/json`, that secret, "Just the push event". Without it, changes made on GitHub show up within 5 minutes of opening the app. If Cloudflare Access or another login sits in front of the site, let `/webhook/github` through, since it checks GitHub's signature itself.

**Already set up with `GIT_REPO_URL` in the YAML (1.0)?** Nothing changes: the YAML wins and Settings shows it as set there. To manage storage in the app instead (and be able to switch), first start 1.1 once with your YAML as it is, then remove `GIT_REPO_URL`, `GIT_TOKEN` and `WEBHOOK_SECRET` and restart. Your garage carries on as kept on this server, with the history it already has here (older history stays on GitHub); reconnect the same repo in Settings → Storage (Connect GitHub Backup, then Make GitHub the Primary Copy if you want GitHub to stay primary). If you removed them before 1.1 had started once, Settings → Storage shows a plain folder: tap Keep History on This Server, then connect the repo.

**TrueNAS App Catalog.** A catalog listing is being prepared (see [deploy/truenas-catalog/SUBMITTING.md](deploy/truenas-catalog/SUBMITTING.md)); until it's accepted, install with the YAML above.

**Versions.** `latest` is the newest release and `edge` is the newest build of `main`; each release also has its own tag (`1.1.0`). See [CHANGELOG.md](CHANGELOG.md) and [RELEASING.md](RELEASING.md).

**Building your own image.** Pushing a version tag runs the GitHub Action that builds `ghcr.io/<owner>/mygarage`. Set the package's visibility to Public (profile → Packages → `mygarage` → Package settings) so TrueNAS can pull it; it holds only app code, never garage data.

## Editing and offline use (1.1)

**Editing.** A tool page has **Edit** (name, category, paid and replacement values, the details table, parts), **Log Maintenance** (tap an entry to change or delete it), **Add Photo**, **Add Receipt**, **Add Manual**, and **Manage Photos & Files** (rename, change a file's type, make a photo the cover, delete). Projects have **Edit** (title, status, dates, tools used), **Add Files** and **Edit Notes**; reference pages have **Edit**, and Tools, Projects and Reference each have a **+**. Tools and projects can be deleted from their Edit screen. Every change is one commit, written in the same Markdown formats the app reads, by "<your username> via MyGarage". With GitHub as the primary copy, the server pulls the latest commit, applies your change and pushes, so edits made on GitHub in the meantime aren't lost. If you and someone else changed the same field, nothing is saved and the app asks you to reload. Photos are saved at 2560 pixels (about 1 MB; iPhone HEIC becomes JPEG) unless you turn on Full Resolution; receipts, manuals and other files are kept exactly as they are. Files can be up to `MAX_UPLOAD_MB` each (default 50; GitHub refuses files over 100 MB).

**History and undo.** Each tool and project has a History screen, and Settings → History lists every change. **Undo** adds a new change that reverses the old one, so nothing is erased and the undo can itself be undone. If a later change rewrote the same lines, the app says so instead of guessing. A plain folder (no history) can turn history on from Settings → Storage.

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
| `GIT_REPO_URL` | (none) | Optional: a GitHub data repo as the source, instead of choosing on the setup screen. Locks the choice |
| `GIT_BRANCH` | `main` | Branch to use |
| `GIT_TOKEN` | (none) | GitHub token (Contents: Read and write for editing); sent as a header, never written to disk |
| `WEBHOOK_SECRET` | (made up) | Secret for `/webhook/github`; by default one is made when GitHub is connected and shown in Settings |
| `FRESHNESS_SECONDS` | `300` | A page view checks GitHub first if the last check is older than this |
| `SYNC_INTERVAL` | `1800` | Seconds between background checks (minimum 60). "Check for Updates" on the home screen checks right away |
| `CONFIG_PATH` | `/data/config/auth.json` | Where the login settings are kept |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | (none) | Optional first login instead of the setup screen; ignored once one exists |
| `READ_ONLY` | `false` | `true` hides every way to change the data repo from the app |
| `MAX_UPLOAD_MB` | `50` | Largest photo or file you can add (GitHub's hard limit is 100) |
| `GIT_AUTHOR_EMAIL` | `mygarage@users.noreply.github.com` | Email on commits made from the app |
| `GITHUB_REPO` | (none) | Your data repo as `owner/name`; shows "Edit" links to the README on GitHub |
| `PORT` | `8000` | Port the app listens on inside the container |
| `SITE_TITLE` | `MyGarage` | App name: large title, first tab, Home Screen label |
| `REPO_PATH` / `CACHE_PATH` | `/data/repo`, `/data/cache` | Where the clone and thumbnails live |

## Credits

MyGarage's own code is MIT-licensed (see LICENSE). It ships three open-source libraries, each with its license next to it: [Feather](https://feathericons.com) icons (MIT, `app/static/feather-LICENSE.txt`), [PDF.js](https://github.com/mozilla/pdf.js) for the manual reader (Apache 2.0, `app/static/vendor/pdfjs/`), and [three.js](https://threejs.org) for the STL viewer (MIT, `app/static/vendor/three/`).

## Run it locally

```
pip install -r requirements.txt
REPO_PATH=/tmp/garage/repo CACHE_PATH=/tmp/garage/cache CONFIG_PATH=/tmp/garage/config/auth.json \
  uvicorn app.main:app --reload
```

Then open <http://localhost:8000> and go through the setup screens (start with the examples). To try an existing data repo instead, point `REPO_PATH` at a clone of it. Tests: `pip install pytest httpx && python -m pytest -q`. They build a small made-up data repo in a temp folder (`tests/fixture.py`), so they need no real data.
