# Changelog

Versions follow [semantic versioning](https://semver.org): a patch release (1.1.1) only fixes things, a minor release (1.2.0) adds features, and a major release (2.0.0) needs something from you when you update. Each release's image is `ghcr.io/jt87/mygarage:<version>`; `latest` is the newest release.

## 1.4.0 (unreleased)

### Added
- **Photos are shown square everywhere in the app**: the home screen, lists, the tool page gallery and full-screen view, and the insurance inventory. When you add a photo that isn't square, the app goes straight to Adjust Photo, where you drag the photo to choose what's in the square and pinch or use the slider to zoom. Any photo can be adjusted later from Manage Photos & Files → Adjust. The photo itself is never changed; the chosen square is saved in a small `.photo-crops.json` next to it, and Download and Open still give the whole photo.

### Fixed
- Make Cover could overwrite the photo being made the cover if it had been added in the same second.

## 1.3.0 (2026-10-16)

### Changed
- **A new home screen built for finding a tool.** One search field at the top narrows your tools as you type, by name, model number, category, or the parts a tool takes (type "blade" to find the saw). Press Return, or tap "Search everything", to search parts, manuals, projects and reference too. Shopping, Projects and Reference are one tap away below it. The total value now sits on the Insurance row.
- **Tool pages open with what you came to do:** Manual, Parts and Log Work, then the parts, manuals and maintenance log. Purchase and insurance details are further down, and a "No receipt" flag at the top jumps to them.
- A cleaner look: monochrome icons, color only for actions and warnings, titles in Barlow Condensed, and model numbers, serials and dates in a fixed-width font. A tool with no photo shows its model number as a label instead of a placeholder wrench.
- The Tools list shows each tool's model number instead of its value.

### Upgrading from 1.2
- Nothing is required: open the app in TrueNAS Apps and choose Update.

## 1.2.1 (2026-10-16)

### Fixed
- The README now explains how to move from an older YAML (one pinned to a version, or with `GIT_REPO_URL`) to the current one, keeping your garage, login and settings.

### Upgrading from 1.2.0
- Nothing is required: open the app in TrueNAS Apps and choose Update.

## 1.2.0 (2026-10-16)

### Added
- **One-click updates.** The install YAML now follows `ghcr.io/jt87/mygarage:1`, so a new release is one click: open the app in TrueNAS Apps and choose Update. No reinstalling.
- When a new version is out, a banner says so and how to update.
- **Settings → About** shows the version, when it was installed, and whether an update is available. What's New has the notes for every version, and works offline.
- After an update, a banner says what version you're on, with a link to What's New.
- When a release that needs something from you (2.0) comes out, a notice says what to do. `:1` never picks it up on its own.

### Upgrading from 1.1
- Change the image to `ghcr.io/jt87/mygarage:1` once (Apps → mygarage → Edit). After that, every update is Apps → mygarage → Update.

## 1.1.0 (2026-10-16)

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
