# Changelog

Versions follow [semantic versioning](https://semver.org): a patch release (1.1.1) only fixes things, a minor release (1.2.0) adds features, and a major release (2.0.0) needs something from you when you update. Each release's image is `ghcr.io/jt87/mygarage:<version>`; `latest` is the newest release.

## 1.1.0 (unreleased)

### Added
- **Edit from your phone.** Log maintenance (and edit or delete entries), add photos, receipts and manuals, edit a tool's details and parts, add and delete tools, manage files (rename, change type, set the cover photo, delete), create and edit projects, and add or edit reference pages. Every change is one commit to your garage's history.
- **History and Undo** for every tool and project, and for the whole garage in Settings.
- **Works offline.** Pages and photos are kept on your device; manuals and 3D files once opened, or all at once from Settings. Edits made offline are sent when you're back online. Needs HTTPS.
- **Keep your garage on your own server.** The first-run screens ask where it lives: on this server, on this server backed up to GitHub, or with GitHub as the primary copy. Switch any time in Settings → Storage.
- Start with an empty garage or a few made-up examples.
- Photos are saved at 2560 pixels unless you choose Full Resolution.
- The app's version is shown at the bottom of Settings.
- The container listens on `PORT` (default 8000).

### Changed
- Installing needs only the storage folder in the YAML; everything else is set up in the app.
- PDF.js and three.js ship with the app instead of loading from a CDN.
- The `latest` image now moves only when a release is published. Builds of `main` are tagged `edge`.

### Upgrading from 1.0
- Nothing is required. A `GIT_REPO_URL` in your YAML keeps working and keeps GitHub as the primary copy.
- To edit from the app with GitHub, give the GitHub token **Contents: Read and write**.

## 1.0.0

- First release: a read-only site for your garage repo, with tool pages, manuals, a shopping list, an insurance inventory, projects with a 3D viewer, and Sonarr-style login.
