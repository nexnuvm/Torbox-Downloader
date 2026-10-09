# TorBox Downloader (standalone Android app)

Builds entirely through GitHub Actions -- no local Android Studio setup
required to get an installable APK. I can't run this workflow myself (no
GitHub repo/Actions access from where I write code), so the first real run
is where anything I couldn't verify locally would surface -- more on that
below.

## What this is

The same download-queue engine, SQLite persistence, TorBox API logic, and
HTML/JS UI from the Termux version, running inside a real standalone Android
app via [Chaquopy](https://chaquo.com/chaquopy/) (an embedded CPython
interpreter). Only the 5 things that were actually Termux-specific changed:

| Termux version | This version |
|---|---|
| `termux-wake-lock` / `-unlock` | `PowerManager.WakeLock` (`AndroidBridge.kt`) |
| `termux-notification` | `NotificationManager` (`AndroidBridge.kt`) |
| `termux-wifi-connectioninfo` | `ConnectivityManager` (`AndroidBridge.kt`) -- no Location permission needed, unlike the Termux version |
| `curl` subprocess | Python's own `urllib` (dropped the external binary) |
| `aria2c` subprocess | Same aria2c, built for Android by CI (see below) -- still shelled out to exactly as before |

The web UI (`HTML_TEMPLATE` inside `torbox_server.py`) is **unchanged** --
`MainActivity` just points a `WebView` at `http://127.0.0.1:8080`, which the
embedded Python server still serves.

## Getting this into a repo and building it

1. Create a new (private, if you want) GitHub repo and push this entire
   folder to it as-is -- the workflow is already at
   `.github/workflows/build.yml`.
2. Push to `main`, or go to the repo's **Actions** tab and run **Build APK**
   manually (`workflow_dispatch` is enabled).
3. When it finishes, download the `torbox-downloader-debug-apk` artifact
   from the workflow run's summary page. That's your installable APK --
   sideload it (you'll need to allow installs from unknown sources, or just
   `adb install app-debug.apk`).

### What the workflow actually does

Two jobs:

- **`build-aria2c`** -- builds `aria2c` for `arm64-v8a` using **aria2's own
  official Android build recipe** (`Dockerfile.android` from
  `github.com/aria2/aria2`, fetched fresh each run) -- not a third-party
  prebuilt binary. This statically links OpenSSL, libexpat, zlib, c-ares and
  libssh2 via NDK r25c, exactly as aria2's maintainers document for Android.
  Result is cached (`actions/cache`, keyed on `ARIA2C_CACHE_VERSION` in the
  workflow file) so it only rebuilds when you bump that version -- the
  OpenSSL compile alone takes a few minutes, no reason to redo it on every
  push that only touches the Kotlin or Python side.
- **`build-apk`** -- downloads that binary into
  `app/src/main/jniLibs/arm64-v8a/libaria2c.so`, sets up JDK 17 + the
  Android SDK/NDK + Gradle, and runs `gradle assembleDebug`.

### Why there's no `gradlew` committed

`gradle-wrapper.jar` is a compiled binary I can't produce from source text,
and I don't have network access to download Gradle and generate one in the
environment I write code in. The workflow sidesteps this entirely by
installing Gradle directly via the official `gradle/actions/setup-gradle`
action instead of relying on a wrapper. If you open this project in Android
Studio for local development, it'll offer to generate a wrapper for you
automatically on first open -- that's unrelated to CI and only matters if
you want to build locally too.

## Known gaps / deliberate simplifications

- **Storage**: `AndroidBridge.getDownloadDir()` points at app-private
  external storage (`Android/data/com.torbox.downloader/files/downloads`),
  not the public Downloads folder. This is deliberate: aria2c needs a plain
  filesystem path to write to, and on modern Android only app-private
  storage (or a user-granted Storage Access Framework folder) gives you that
  without extra permission prompts. If you want completed files in the real
  Downloads folder, add a SAF "export" step that copies each file there
  after it finishes -- aria2c itself can't write through a SAF
  `DocumentFile` URI directly.
- **Launcher icon** is a plain placeholder vector (blue background, white
  download-arrow glyph) in `res/drawable/ic_launcher_foreground.xml` -- just
  there so the build doesn't fail on a missing resource. Swap it for real
  art whenever you like; it's plain XML, no image tooling needed to edit it.
- **No release signing config** -- the workflow builds the **debug** variant
  only, auto-signed with Android's default debug keystore (fine for
  sideloading onto your own device, not for distributing further). If you
  want a signed release build, that needs a keystore + GitHub Secrets for
  the passwords -- tell me if you want that added.
- **Single ABI** (`arm64-v8a`). Covers the overwhelming majority of real
  devices; add `armeabi-v7a` to both the workflow (a second aria2c build
  target) and `app/build.gradle`'s `abiFilters` if you need older 32-bit
  hardware too.
- **Open-on-tap for notifications** isn't implemented (`action_file` is
  accepted but unused in `send_notification`) -- cosmetic, not functional.

## Debug instrumentation still in place

`torbox_server.py` still has the `[debug]` checkpoint logging and the
watchdog thread added while diagnosing the aria2c RPC-server hang in the
Termux version. They're harmless here (same-process Kotlin calls can't hang
the way the old termux-* subprocess calls could), but strip them out once
you've confirmed everything behaves as expected on a real device.

## If the first CI run fails

This is the one part I genuinely can't guarantee without it actually
running -- tell me the failing step and its log output and I'll fix it. The
most likely sources of a first-run hiccup, roughly in order of likelihood:
Gradle/AGP/Chaquopy version compatibility, the NDK version string Android
Studio's setup-android action expects vs. what's actually available, or a
Chaquopy quirk around Python stdlib module availability (everything used
here -- `sqlite3`, `http.server`, `urllib`, `json`, `threading`,
`subprocess` -- is plain stdlib, which Chaquopy supports, but I haven't
been able to verify that combination builds clean end-to-end myself).
