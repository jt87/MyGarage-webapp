# Getting MyGarage into the TrueNAS App Catalog

`mygarage/` is the app package for the **community train** of
[truenas/apps](https://github.com/truenas/apps) (TrueNAS no longer accepts custom
catalogs, so this is the only route to an Install button). Their rules are in
[CONTRIBUTIONS.md](https://github.com/truenas/apps/blob/master/CONTRIBUTIONS.md);
inclusion is at iXsystems' discretion.

## What's ours and what's TrueNAS's

Every file in `mygarage/` was written for MyGarage. What it shares with other catalog
apps is what TrueNAS requires of all of them: the metadata fields in `app.yaml`, the
setting names its template library reads (`network.web_port`, `storage.data`, `run_as`,
`labels`, `resources`), the standard form groups, and calls to that library in the
compose template. `check.sh` vendors the library itself (`templates/library/`) from
truenas/apps; it's TrueNAS's code and isn't kept in this repo.

## Left out on purpose

The install form leaves out two options many catalog apps carry: SMB/NFS shares as extra
storage, and joining extra Docker networks. MyGarage keeps everything in its own data
storage and is reached through its port, so neither is needed. If a reviewer asks for
them, add them in our own wording.

## Before submitting

1. **Publish a release** so the image exists: see [RELEASING.md](../../RELEASING.md).
   `ghcr.io/jt87/mygarage` must be **public** (GitHub → Packages → mygarage →
   Package settings → Change visibility).
2. **Pin the image digest.** In `mygarage/ix_values.yaml` set the tag to
   `"<version>@sha256:<digest>"`; the digest is on the package page, or
   `docker buildx imagetools inspect ghcr.io/jt87/mygarage:<version>`.
3. **Run TrueNAS's checks**: `deploy/truenas-catalog/check.sh`. It copies the package
   into a checkout of truenas/apps, vendors their library, checks ports, renders
   the compose file, starts the app until it's healthy, and runs their validator.
4. **Screenshots.** Three 2560×1600 PNGs (a desktop browser at 1280×800, 2×) from
   a real garage: the home screen, a tool page with a photo, and the Log
   Maintenance or Edit screen. The icon is `assets/icon.png`. Reviewers upload
   them to TrueNAS's CDN; the URLs in `app.yaml` and `item.yaml` assume
   `apps/mygarage/icons/icon.png` and `apps/mygarage/screenshots/screenshotN.png`.

## Submitting

1. Fork truenas/apps and create a branch.
2. Copy the checked package from `.truenas-apps/ix-dev/community/mygarage` (made by
   `check.sh`, with the vendored `templates/library/`) to `ix-dev/community/mygarage`.
   Don't commit anything else; their CI regenerates the rest.
3. Open the pull request with their app-addition template
   (`.github/PULL_REQUEST_TEMPLATE/app_addition.md`) and attach the icon and screenshots.

## After it's in the catalog

- New releases reach TrueNAS through their Renovate bot, which opens a pull request
  bumping the image tag; TrueNAS then shows **Update** for MyGarage. A release that
  changes the install form (`questions.yaml`) or the compose template needs a pull
  request to truenas/apps from us, with the app's `version` bumped.
- Keep this folder in step with what's in truenas/apps so `check.sh` tests the real thing.

## Moving an existing install

A catalog install is a new TrueNAS app with its own storage. To keep your garage:
stop the old app, point the new app's **MyGarage Data Storage** at the same dataset
(Host Path), and install. MyGarage finds its settings and garage there.
