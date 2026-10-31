# In-App Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The packaged app finds a newer GitHub release, asks once, then downloads, verifies, swaps itself, restarts, and rolls back if the new copy doesn't start.

**Architecture:** All update logic is plain Python in a new `app/update.py` (no Qt, no network in tests: callers inject `urlopen`-style openers and paths). `app/feed.py` gains a poller that stops between batches and can be waited on. `app/ui.py` owns the check timer, the prompt, the update lock, and the handshake wait. `app/main.py` writes the confirmation file first thing and cleans leftovers. The release workflow stamps the version and adds a macOS zip.

**Tech Stack:** Python 3.12 stdlib (`urllib.request`, `hashlib`, `zipfile`, `subprocess`, `secrets`), PySide6 (`QLockFile`, `QTimer`, `QMessageBox`, `QDesktopServices`), pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-31-self-update-design.md`

## Global Constraints

- Releases API: `https://api.github.com/repos/darrenchen75/stockapp/releases/latest`. Releases page: `https://github.com/darrenchen75/stockapp/releases`.
- Assets by OS: Linux `StockBars-linux.AppImage`, Windows `StockBars-windows.zip` (contains `StockBars.exe`), macOS `StockBars-macos.zip` (new; contains `StockBars.app`).
- Digest field is `sha256:<hex>`. An asset with no digest, or not yet attached, means "no update offered".
- `app/version.py` committed as `VERSION = "dev"`; `dev` never checks. Versions compare as integer tuples from `vX.Y.Z`; only strictly newer prompts; unparsable tags are ignored.
- Check on launch, then every 24 hours. Failed checks are logged only.
- "Later" suppresses that version for the rest of the session only.
- Confirmation timeout: 30 seconds. Stale confirmation files: older than 60 seconds.
- Aside names: Linux `<name>.old`, Windows `StockBars.old.exe`, macOS `StockBars.old.app` (sibling of the bundle).
- Spawn the new Windows copy with env `PYINSTALLER_RESET_ENVIRONMENT=1`.
- macOS: `ditto -x -k` to unzip, `xattr -dr com.apple.quarantine` on the new bundle, launch `StockBars.app/Contents/MacOS/StockBars` directly.
- No new dependencies. Timeouts on every HTTP call (10s for the API, 60s per read for downloads).
- Rollback message copy: `The update didn't start, so you're still on {current}.`

## Review Focus

1. **Target folder not writable** (dmg mount, App Translocation, Program Files): the app falls back to opening the Releases page and keeps running, with no partial files left behind. Test: `test_prepare_fails_cleanly_on_readonly_dir` (Task 2).
2. **Download cut off or wrong bytes:** digest mismatch raises and deletes the temp file; nothing is swapped. Test: `test_download_rejects_digest_mismatch` (Task 2).
3. **Second rename fails** (file locked by antivirus on Windows): the old copy is moved back to the target path. Test: `test_swap_rolls_back_when_second_rename_fails` (Task 3).
4. **Another instance starts during an update:** it cannot confirm, and the lock stops it from starting a second update. Test: `test_wait_ignores_other_files` (Task 4); lock is verified manually in Task 6.
5. **A release where only some OS builds are uploaded yet:** no prompt on the OS whose asset is missing. Test: `test_latest_ignores_release_without_this_os_asset` (Task 1).

---

### Task 1: Version stamp and release lookup

**Files:**
- Create: `app/version.py`, `app/update.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Produces:
  - `app.version.VERSION: str` (`"dev"` in the repo)
  - `update.parse_version(tag: str) -> tuple[int, int, int] | None` (accepts `v1.2.3` and `1.2.3`)
  - `update.ASSETS: dict[str, str]` keyed by `sys.platform` (`"linux"`, `"win32"`, `"darwin"`)
  - `@dataclass(frozen=True) class Release: version: str; url: str; digest: str` (digest is bare hex)
  - `update.latest(current: str, platform: str = sys.platform, opener=urllib.request.urlopen) -> Release | None`
  - `update.RELEASES_PAGE: str`

- [ ] **Step 1: Write the failing tests.** Use a fake opener that returns an object with `.read()` giving JSON bytes and works as a context manager.

```python
def test_parse_version():
    assert update.parse_version("v0.1.3") == (0, 1, 3)
    assert update.parse_version("0.10.0") == (0, 10, 0)
    assert update.parse_version("dev") is None
    assert update.parse_version("v1.2") is None

def test_latest_offers_newer_release():
    r = update.latest("0.1.2", "linux", opener=fake({"tag_name": "v0.1.3", "assets": [
        {"name": "StockBars-linux.AppImage", "browser_download_url": "https://x/a", "digest": "sha256:ab"}]}))
    assert r == update.Release("0.1.3", "https://x/a", "ab")

def test_latest_ignores_same_or_older():  # tag v0.1.2 and v0.1.1 with current 0.1.2 -> None
def test_latest_never_checks_from_source():  # current "dev" -> None, opener never called
def test_latest_ignores_release_without_this_os_asset():  # only the linux asset, platform "darwin" -> None
def test_latest_ignores_asset_without_digest():  # digest missing or null -> None
def test_latest_returns_none_on_network_error():  # opener raises OSError -> None, no exception
```

- [ ] **Step 2: Run `uv run pytest tests/test_update.py -v`.** Expected: FAIL, `ModuleNotFoundError: app.update`.
- [ ] **Step 3: Implement.** Use `json.load`, a 10s `timeout=` on the opener, and a `User-Agent` header (GitHub rejects requests without one). `latest()` catches `OSError` and `ValueError`, logs a warning, and returns `None`.
- [ ] **Step 4: Run the tests again.** Expected: all pass. Then run `uv run pytest`: everything passes.
- [ ] **Step 5: Commit** `feat: look up the latest release for this OS`.

### Task 2: Download, verify, and prepare the new copy

**Files:**
- Modify: `app/update.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Consumes: `Release` (Task 1)
- Produces:
  - `class UpdateError(Exception)`
  - `update.target(platform: str, executable: str, environ: Mapping[str, str]) -> Path | None`. Linux: `Path(environ["APPIMAGE"])`, or `None` if unset. Windows: `Path(executable)`, or `None` unless it ends in `.exe` and `sys.frozen` is set (pass `frozen: bool = getattr(sys, "frozen", False)` as a keyword). macOS: the ancestor ending in `.app`, or `None`.
  - `update.download(release: Release, dest: Path, opener=urllib.request.urlopen) -> None`. Streams to `dest` and checks the SHA-256. On mismatch it deletes `dest` and raises `UpdateError`.
  - `update.prepare(release: Release, target: Path, platform: str, opener=...) -> Path`. Downloads into `target.parent`. Linux: `chmod 0o755`. Windows: extract `StockBars.exe` with `zipfile` as `StockBars.new.exe`. macOS: `ditto -x -k` into a temp folder beside the target, then `xattr -dr com.apple.quarantine`, giving `StockBars.new.app`. Returns the ready path. It deletes every temp file, then re-raises as `UpdateError` on any `OSError`, `zipfile.BadZipFile` or `subprocess.CalledProcessError`.

- [ ] **Step 1: Write the failing tests.** Fake opener serving bytes from memory. The digest is computed in the test with `hashlib.sha256`.

```python
def test_download_writes_verified_file(tmp_path):  # bytes land in dest, no exception
def test_download_rejects_digest_mismatch(tmp_path):
    with pytest.raises(update.UpdateError):
        update.download(update.Release("0.1.3", "u", "00" * 32), tmp_path / "f", opener=fake_bytes(b"x"))
    assert not (tmp_path / "f").exists()
def test_target_per_platform(tmp_path):  # linux with/without APPIMAGE; darwin /A/StockBars.app/Contents/MacOS/StockBars -> /A/StockBars.app; win32 frozen .exe vs not frozen -> None
def test_prepare_linux_is_executable(tmp_path):  # result mode & 0o111 != 0, result.parent == target.parent
def test_prepare_windows_extracts_exe(tmp_path):  # zip built in test containing StockBars.exe -> StockBars.new.exe with same bytes, zip temp removed
def test_prepare_fails_cleanly_on_readonly_dir(tmp_path):  # chmod parent 0o555 -> UpdateError, directory listing unchanged (skip if os.geteuid() == 0)
```

The macOS branch shells out to `ditto` and `xattr`, so it is covered by the manual check in Task 7 only.

- [ ] **Step 2: Run `uv run pytest tests/test_update.py -v`.** Expected: the new tests fail.
- [ ] **Step 3: Implement the three functions.**
- [ ] **Step 4: Run the tests again.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: download and verify update builds`.

### Task 3: Swap, restore, and clean up aside copies

**Files:**
- Modify: `app/update.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Produces:
  - `update.aside_path(target: Path, platform: str) -> Path` (names in Global Constraints)
  - `update.swap(target: Path, new: Path, platform: str) -> Path`: returns the aside path. Linux: `os.link(target, aside)`, then `os.replace(new, target)`. Windows/macOS: `os.rename(target, aside)`, then `os.rename(new, target)`; if the second fails, `os.rename(aside, target)` and raise `UpdateError`. Remove any stale aside first (file: `unlink`, bundle: `shutil.rmtree`).
  - `update.restore(target: Path, aside: Path) -> None`: delete whatever is at `target` (file or dir), then `os.replace` the aside back.
  - `update.cleanup(target: Path | None, platform: str) -> None`: delete the aside copy if present. Ignore `OSError`, because Windows may still hold it.

- [ ] **Step 1: Write the failing tests.** All run on Linux by passing `platform`; a "bundle" is just a directory.

```python
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_swap_then_restore_files(tmp_path, platform):  # target bytes == new after swap, aside == old; restore -> target == old, no aside
def test_swap_bundle_directory(tmp_path):  # platform "darwin", dirs with a marker file inside
def test_swap_linux_target_never_missing(tmp_path):  # monkeypatch os.replace to assert target.exists() when called
def test_swap_rolls_back_when_second_rename_fails(tmp_path, monkeypatch):  # win32; second os.rename raises -> UpdateError, target holds old bytes
def test_cleanup_removes_aside_and_ignores_errors(tmp_path):
```

- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: swap update builds with rollback`.

### Task 4: Start handshake

**Files:**
- Modify: `app/update.py`, `app/main.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Produces:
  - `update.CONFIRM_FLAG = "--confirm-update"`
  - `update.new_confirm_path(data_dir: Path) -> Path`: `data_dir / f"update-{secrets.token_hex(8)}.ok"`, not yet created
  - `update.launch(target: Path, platform: str, confirm: Path) -> subprocess.Popen`. The command is `[exe, CONFIRM_FLAG, str(confirm)]`, where `exe` is the target itself, or `target/Contents/MacOS/StockBars` on macOS. On win32 the env adds `PYINSTALLER_RESET_ENVIRONMENT=1`.
  - `update.confirm_from_argv(argv: list[str]) -> None`: if the flag is present, write the file named after it. Ignore `OSError`.
  - `update.wait_confirmed(path: Path, timeout: float = 30, poll: float = 0.2, sleep=time.sleep, clock=time.monotonic) -> bool`
  - `update.clean_confirmations(data_dir: Path, now: float | None = None, max_age: float = 60) -> None`
- Modify `app/main.py`. Call `update.confirm_from_argv(sys.argv)` right after `QApplication(sys.argv)` is created and before `settings.load_keys()`. After `data_dir` exists, call `update.clean_confirmations(data_dir)` and `update.cleanup(update.target(sys.platform, sys.executable, os.environ), sys.platform)`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_confirm_writes_only_the_named_file(tmp_path):  # argv with flag -> file exists; argv without -> no files
def test_wait_confirmed_true_when_file_appears(tmp_path):  # fake sleep creates the file on 2nd call
def test_wait_confirmed_times_out(tmp_path):  # fake clock advances 1s per sleep; returns False after 30 ticks
def test_wait_ignores_other_files(tmp_path):  # another update-*.ok present, ours absent -> False
def test_clean_confirmations_keeps_fresh_files(tmp_path):  # mtime 120s ago removed, 10s ago kept
def test_launch_command(tmp_path, monkeypatch):  # monkeypatch subprocess.Popen to capture args/env per platform
```

- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement it and wire up `main.py`.**
- [ ] **Step 4: Run `uv run pytest`.** Expected: all pass. Then run `uv run python -m app.main --confirm-update /tmp/claude-x.ok` and close the window. Check that `/tmp/claude-x.ok` exists, then delete it.
- [ ] **Step 5: Commit** `feat: confirm a new copy started before the old one quits`.

### Task 5: Poller stops between batches and can be waited on

**Files:**
- Modify: `app/feed.py` (`backfill`, `Poller.start`, add `Poller.halt`)
- Test: `tests/test_feed.py`

**Interfaces:**
- Produces:
  - `backfill(..., stop: threading.Event | None = None)`: checks `stop.is_set()` before each batch and returns early with the count so far.
  - `Poller.halt(timeout: float | None = None) -> bool`: sets `stop`, joins the thread, and returns whether it ended. `start()` keeps the thread on `self.thread`. `run()` passes `self.stop` into `backfill`.
  - A halted poller is not restarted. The UI builds a new one with its existing `start_poller(keys)`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_backfill_stops_between_batches(tmp_path):
    # 25 symbols, first run -> 3 batches of 10; progress callback sets stop after batch 1
    # -> len(client.requests) == 1
def test_poller_halt_waits_for_thread(tmp_path):
    # Poller(FakeClient(), db, ["AAPL"], poll_seconds=3600).start(); assert p.halt(timeout=5) and not p.thread.is_alive()
```

- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `uv run pytest`.** Expected: all pass.
- [ ] **Step 5: Commit** `feat: let the poller stop mid-backfill and be joined`.

### Task 6: Update flow in the window

**Files:**
- Modify: `app/ui.py`
- Test: manual (Qt and real processes). The logic it calls is covered by Tasks 1–5.

**Interfaces:**
- Consumes: everything above, plus `app.version.VERSION`.
- Produces: in `MainWindow`, a signal `update_found = Signal(object)` carrying a `Release`, and methods `check_for_update()`, `offer_update(release)`, `run_update(release)`. `MainWindow.__init__` gains a `data_dir: Path` parameter, and `main.py` passes it. Keep `self.keys` so rollback can call `start_poller(self.keys)`. `on_auth_failed` updates `self.keys`.

Behavior, in order. Each item is one step: implement it, then run it.

- [ ] **Step 1: Check.** In `__init__`, when `VERSION != "dev"`: create a `QTimer` with an interval of `24 * 3600 * 1000` and call `check_for_update` now and on each timeout. `check_for_update` runs `update.latest(VERSION)` on a daemon thread and emits `update_found` only for a non-`None` result.
- [ ] **Step 2: Prompt.** `offer_update` skips releases whose version is in `self.declined: set[str]`. It takes `QLockFile(str(data_dir / "update.lock"))` with `tryLock(0)` and returns quietly if the lock is busy. It asks `Stock Bars {new} is available (you have {VERSION}). Update now?` with **Update** and **Later**. **Later** adds the version to `declined` and unlocks.
- [ ] **Step 3: Update.**
  1. Show a busy `QProgressDialog`.
  2. Find the target with `update.target(...)`. If it is `None`, use the fallback.
  3. Call `update.prepare` on a worker thread.
  4. Call `self.poller.halt()`, then `update.swap`.
  5. Call `update.launch` with `update.new_confirm_path(data_dir)`.
  6. Hide the window.
  7. Run `update.wait_confirmed` on a worker thread. Bring the result back with a signal; never block the UI thread.
  8. If confirmed: delete the confirm file, unlock, and `QApplication.quit()`.
  9. If it times out: `proc.terminate()`, `update.restore`, `start_poller(self.keys)`, unlock, show the window, and show the rollback message with an **Open download page** button.
- [ ] **Step 4: Fallback.** On any `UpdateError`, or a `None` target: restore if the swap already happened, restart the poller if it was halted, and unlock. Then show `Couldn't update automatically.` with an **Open download page** button (`QDesktopServices.openUrl(QUrl(update.RELEASES_PAGE))`).
- [ ] **Step 5: Verify locally.**
  1. `uv run pytest` passes.
  2. Run `uv run python -m app.main`: with `VERSION == "dev"`, no check happens. The log has no update lines.
  3. Temporarily set `VERSION = "0.0.1"`. The prompt for `0.1.2` appears.
  4. Click **Update**. With no `$APPIMAGE` set, the fallback message appears and the app keeps polling.
  5. Revert `VERSION`.
- [ ] **Step 6: Commit** `feat: offer and run in-app updates`.

### Task 7: Release workflow, README, first release

**Files:**
- Modify: `.github/workflows/release.yml`, `README.md`

- [ ] **Step 1: Workflow.** In the build job, before PyInstaller, add `run: echo "VERSION = \"${GITHUB_REF_NAME#v}\"" > app/version.py` with `shell: bash` so it also runs on Windows. In the macOS package step, also run `ditto -c -k --keepParent dist/StockBars.app StockBars-macos.zip`. The existing `files: StockBars-*` glob already uploads it.
- [ ] **Step 2: README.** Add an `## Updates` section in plain language before "Something wrong?". Three points:
  - When a new version is out, the app asks. Click **Update** and it restarts by itself.
  - If the update can't install, the app keeps working and offers the download page.
  - Anyone on version 0.1.2 or older downloads once more by hand (Step 2 of Setup). After that, updates come through the app.
- [ ] **Step 3: Verify.** Run `uv run pytest`. Expected: all pass. Run `git diff .github/workflows/release.yml` and check that the version line comes before PyInstaller in every matrix job.
- [ ] **Step 4: Commit** `feat: stamp version and ship macOS update zip`.
- [ ] **Step 5: Hand off to the user.** Releasing is outward-facing, so ask the user before tagging. Then:
  1. Tag `v0.1.3`.
  2. Install it on each OS.
  3. Tag `v0.1.4` with any small change.
  4. Confirm that each OS offers the update, swaps, restarts on `0.1.4`, keeps its key and data, and leaves no `.old` copy after the next launch.
