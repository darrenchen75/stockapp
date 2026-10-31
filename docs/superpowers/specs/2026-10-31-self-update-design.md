# In-App Updates — Design

Date: 2026-10-31

## Problem

People install Stock Bars by downloading a file from GitHub Releases and, on macOS
and Windows, clicking through security warnings. To get a new version they must
repeat all of it, so most will stay on whatever they first installed. Users are
non-technical (see README).

## Goal

When a newer release exists, the app offers it in one click: it downloads the new
build, replaces itself, and restarts. Nothing else changes for the user: the API key
(OS keychain), `bars.db`, and logs live outside the app and carry over. (User decision:
"prompt, then auto-update".)

## Non-goals

- Silent updates with no prompt.
- Release notes, changelog display, or a "skip this version forever" option.
- Downgrades or choosing a version.
- Code signing / notarization.
- Updating copies run from source: they never check.

## Anti-goals

- Never leave someone with a broken or half-replaced app. Any failure keeps the
  running version intact and usable. One exception is accepted: a crash or power loss
  in the millisecond between the two path changes (see "Per-OS swap").
- Never install a file that doesn't match GitHub's published SHA-256 digest.
- This includes after the swap: if the new version doesn't start, the old one is put back
  and keeps running (user decision; see "Start confirmation").

## Behavior

**When it checks** (user decision): on launch, then once every 24 hours while the app
stays open. The check runs off the UI thread and never blocks startup or bar polling.
A failed check (offline, rate limited, bad JSON) is logged and ignored, with no
message to the user.

**The prompt:** a dialog: "Stock Bars <new> is available (you have <current>).
Update now?" with **Update** and **Later**.
- **Later**: no further prompt for that version during this session. Next launch asks
  again. (Builder's call, accepted in design.)
- **Update**: show progress, download, verify, swap, restart. If the swap can't happen
  (see failures), show a message with a button that opens the Releases page, and keep
  running the current version.

**Version source:** `GET https://api.github.com/repos/darrenchen75/stockapp/releases/latest`
(unauthenticated; 60 requests/hour per IP is far above need). Use `tag_name` and, per
asset, `name`, `browser_download_url`, and `digest` (`sha256:<hex>`, already present on
v0.1.2 assets). An asset with no digest is treated as unavailable. Don't prompt until
this OS's asset is attached: the three builds upload separately, so for a few minutes
after a tag the latest release can be missing one. (Builder's call, accepted in design.)

**Knowing its own version:** the release workflow writes the tag (e.g. `0.1.3`) into
`app/version.py` before PyInstaller runs. The committed file says `VERSION = "dev"`;
`dev` means "running from source": no update checks. Compare as integer tuples from
`vX.Y.Z`; only strictly newer triggers a prompt. A tag that doesn't parse is ignored.

## Per-OS swap

Only one update runs at a time per install: take a lock file (`QLockFile`) in the data
folder before downloading and hold it until the update confirms or rolls back. If
another instance holds it, don't prompt.

Download to a temp file in the same directory as the target (so every step is a
same-filesystem rename), verify the digest, then on every OS: keep the current copy
aside, put the new one into place, launch it, and wait for start confirmation (below).
Nothing is deleted until the new copy has confirmed. The two path changes run back to
back with nothing in between, so the installed path is missing for at most milliseconds
on Windows and macOS (none on Linux). A crash or power loss in exactly that window
leaves no app at the path; the aside copy is still on disk beside it. Accepted risk: a
journal or separate launcher would cost more than this rare case is worth.

- **Linux (AppImage):** target is `$APPIMAGE` (set by the AppImage runtime). Write the
  new file beside it and `chmod +x`. Hard-link the old one to `<name>.old`, then
  `os.replace` the new one over the target. The target path always holds a working file.
  Replacing is safe while the old image is mounted. No `$APPIMAGE`: not updatable
  (source or unpacked run), so open the Releases page.
- **Windows (onefile exe):** asset is `StockBars-windows.zip`; extract `StockBars.exe`.
  Target is `sys.executable`. Windows allows renaming a running exe: rename current to
  `StockBars.old.exe` and move new into place. If a rename fails, move the old one back.
  Spawn the new process with `PYINSTALLER_RESET_ENVIRONMENT=1` so it unpacks fresh
  instead of reusing the old instance's temp folder.
- **macOS (.app bundle):** the release adds a new asset, `StockBars-macos.zip`
  (`ditto -c -k --keepParent dist/StockBars.app`), because unpacking a zip is simpler and
  more reliable than mounting a dmg. The `.dmg` stays for first installs. Target is the
  `.app` that contains `sys.executable`. Unzip with `ditto -x -k` (keeps symlinks and
  permissions in the bundle), then run `xattr -dr com.apple.quarantine` on the new bundle,
  rename the old bundle aside, and move the new one into place. Launch the bundle's
  executable directly (`Contents/MacOS/StockBars`) so the old process holds a handle to it.
  Clearing the quarantine flag skips Gatekeeper's prompt for the update. The SHA-256 match
  against GitHub's digest is the safeguard, the same trust as the first install. (User
  decision.) A file the app downloads itself probably has no flag anyway, so this is
  usually a no-op.

**Failures** that fall back to "open Releases page, keep running": target directory not
writable (e.g. app run from the mounted dmg, macOS App Translocation from Downloads,
Windows exe under Program Files), download error, digest mismatch, unzip error, any
rename failure. Each rolls back any partial swap and deletes temp files.

**Start confirmation** (user decision: confirm and roll back):
1. Before launching, the old instance stops its poller and waits for it to finish any
   batch in progress, so only one poller ever writes `bars.db`. This needs the poller to
   check its stop flag between backfill batches, not only between polls; otherwise a
   first-run backfill could take minutes to stop. An old fetch that commits after the new
   poller repairs a split would put unadjusted prices back.
2. The old instance picks a random token and launches the new copy with
   `--confirm-update <path>`, where `<path>` is a file in the data folder named after the
   token. It hides its window and keeps its process alive. No other instance knows the
   path, so no other instance can confirm by mistake.
3. The new copy writes that file as soon as `main()` starts: after `QApplication` exists,
   before the API-key prompt and the main window. "Confirmed" means the new build launched
   and its Python and Qt load. It does not wait for a person to type a key, which could
   take longer than the timeout when no keychain is available.
4. The old instance waits up to 30 seconds for the file to appear.
   - **Appeared:** delete it, release the update lock, and quit normally (SQLite closes).
     The new copy deletes the aside copy (`.old` file or old bundle) a few seconds after
     confirming, retrying on its next launch if the old process still held it.
   - **Timeout:** terminate the new process. Delete the new copy, restore the aside copy,
     restart the poller, release the lock, and show the window again with a message ("The
     update didn't start, so you're still on <current>.") and a button to the Releases
     page. Keep running.
5. On startup, delete confirmation files older than one minute. A crash mid-update leaves
   these behind.

Two instances overlap for a few seconds, but only the new one polls.

## Release workflow changes

- Before PyInstaller: write `app/version.py` with the tag minus its leading `v`.
- macOS job: also produce `StockBars-macos.zip` and attach it.
- Release steps for the maintainer don't change: push a `vX.Y.Z` tag.

## README

Add a short "Updates" section in plain language: the app tells you when a new version
is out; click Update. Note that anyone on v0.1.2 or older must download once more by hand.

## Testing

Unit tests (no network, no Qt): version parsing/comparison, picking the right asset per
OS, digest verification rejects a mismatch, and the swap logic for each OS run against
temp directories (rename-aside, rollback on failure, rollback on confirmation timeout,
leftover cleanup), the token handshake (only the launched copy's file counts), the update
lock, and the poller stopping between backfill batches. Manual check per
OS after the first release that contains this: install vN, publish vN+1, click Update.

## Left to the builder

Module layout (suggest `app/update.py` for the plain-Python logic, dialog in `ui.py`),
timer mechanism for the daily check, progress UI details, HTTP library (stdlib
`urllib` is enough; no new dependency), and log wording.
