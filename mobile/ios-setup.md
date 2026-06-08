# iOS build & release setup

One-time setup to get the belfry Flutter app building, running, and pushing on
iOS. Android is already live; iOS reuses the same backend (`/auth/exchange` JWT,
`/api/*`, HLS) and the same Firebase project for push.

## Status (2026-06-07): brought up on a physical iPad (iOS 26.5)

App builds, installs, signs in (Google → JWT), and plays live HLS + playback on
iOS. Push (ROI alert) wired but final on-device delivery still being verified.
Hard-won gotchas that aren't obvious from the checklist below:

- **Toolchain must be current for iOS 26.** Flutter 3.32 / Xcode 15 segfault at
  launch in `flutter_secure_storage_darwin`'s plugin `register()` on iOS 26.
  Fixed by **Flutter 3.44.1 + Xcode 26** (Swift 6). The Flutter upgrade also
  migrated the iOS project to the **UIScene lifecycle** (`AppDelegate` now
  registers plugins in `didInitializeImplicitFlutterEngine`) and to **Swift
  Package Manager** (Firebase/GoogleSignIn resolve via SPM; only
  `flutter_local_notifications` stays on CocoaPods). Xcode 26 also needs the iOS
  platform downloaded once: `xcodebuild -downloadPlatform iOS`.
- **iOS OAuth client lives in the *auth* project, not Firebase.** The
  `belfry-alerts` Firebase plist legitimately has no `CLIENT_ID` — create the iOS
  OAuth client in the project that holds the Web/Android clients (`850889416010`)
  and pass it as `clientId` in `lib/auth.dart` (`serverClientId` stays the Web id);
  reversed form is the `CFBundleURLTypes` scheme in `Info.plist`.
- **iOS HLS/playback auth = cookie, not header.** `AVPlayer` won't attach the
  bearer to segment/range sub-requests. Caddy gained a `@jwtcookie` handler
  (`cloud/Caddyfile`) and the app stashes the JWT in a `belfry_jwt` cookie via a
  `belfry/cookies` MethodChannel (`AppDelegate.swift` → `HTTPCookieStorage`,
  driven from `lib/auth.dart`). Without this, every tile shows "stream error".
- **Build/install workflow (don't use `flutter run` for device — its install/
  launch step hangs on this Mac's devicectl):**
  `flutter build ios --release --dart-define-from-file=env.json`
  then `xcrun devicectl device install app --device <udid> build/ios/iphoneos/Runner.app`,
  then tap the icon. If a stuck half-grey icon appears, uninstall
  (`devicectl device uninstall app --device <udid> io.yellowchicken.belfry`) or
  reboot to clear SpringBoard, then reinstall.

## Constants (already true in this repo)

- Bundle id: `io.yellowchicken.belfry` (matches Android `applicationId`)
- Firebase project: `belfry-alerts` (project number `863042116403`)
- Backend base: `https://yellowchicken.io`
- Google **web** client id (the JWT-exchange audience, shared across platforms):
  `850889416010-v69eig9u20o9028ejdn76up9qv0uo7vf.apps.googleusercontent.com`
  — stays as `serverClientId`; the backend validates the ID token against it, so
  it does **not** change for iOS.

## Local toolchain

- [x] Apple Silicon Mac, Xcode 15.2, Flutter 3.32.6 — present.
- [ ] **Fix CocoaPods.** It currently crashes (`Could not find 'ffi'`) under the
      Homebrew Ruby 4.0.1 it's linked against, which will block `pod install` on
      the first iOS build. Reinstall against current Ruby:
      ```bash
      brew reinstall cocoapods
      # or, to repair the existing install:  gem install ffi
      pod --version   # should print 1.x cleanly
      ```
  - Note: Xcode 15.2 builds fine today; only an issue if a future iOS/device or
    an App Store submission demands a newer SDK.

## Apple Developer (you — blocked on membership activation)

Paid program is **required**, not optional: iOS FCM push rides on APNs, and APNs
is unavailable on a free Apple ID.

1. [ ] **Register the App ID.** Developer portal → Identifiers → `io.yellowchicken.belfry`,
   enable the **Push Notifications** capability.
2. [ ] **Create an APNs Auth Key (.p8).** Developer portal → Keys → new key with
   **Apple Push Notifications service (APNs)** enabled. Download the `.p8`
   (one-time download — store it safely) and note the **Key ID** and **Team ID**.

## Firebase / Google (you)

3. [ ] **Add an iOS app** to the `belfry-alerts` Firebase project with bundle id
   `io.yellowchicken.belfry`. This also provisions the iOS OAuth client for
   Google Sign-In. Download **`GoogleService-Info.plist`**.
4. [ ] **Upload the APNs key** to Firebase: Project Settings → Cloud Messaging →
   Apple app config → upload `.p8` + Key ID + Team ID. (This is what bridges
   FCM → APNs; without it push silently no-ops on iOS.)
5. [ ] Confirm the iOS OAuth client exists in Google Cloud Console under the same
   project as the web client id (Firebase step 3 normally creates it).

## Code-side wiring (Claude can do — no account access needed)

These can be staged now; some need the `GoogleService-Info.plist` from step 3 to
verify.

6. [ ] Drop `GoogleService-Info.plist` into `ios/Runner/` and add it to the Runner
   target in Xcode (requires the file from step 3).
7. [ ] Add the **reversed client id** URL scheme to `ios/Runner/Info.plist`
   (`CFBundleURLTypes`) so the Google sign-in redirect returns to the app. The
   value is `REVERSED_CLIENT_ID` from the plist.
8. [ ] Pass an iOS `clientId` to `GoogleSignIn.instance.initialize()` in
   `lib/auth.dart` (the plugin reads it from `GoogleService-Info.plist`, or pass
   explicitly). `serverClientId` stays the web client id above.
9. [ ] Verify `firebase_core` initializes on iOS (`AppDelegate.swift` is currently
   bare; the Flutter plugins register it, but confirm push permission prompt +
   token registration fire).

## Xcode project config (you, in Xcode; Claude can pre-stage what's scriptable)

10. [ ] Open `ios/Runner.xcworkspace`. Runner target → Signing & Capabilities →
    set **Team** (automatic signing).
11. [ ] Add the **Push Notifications** capability.
12. [ ] Add **Background Modes → Remote notifications**.

## First build / smoke test

13. [ ] `cd mobile && flutter pub get && flutter build ios --dart-define-from-file=env.json`
    (env.json is required — see project convention).
14. [ ] Run on a real device (`flutter run --dart-define-from-file=env.json`):
    - Google sign-in → JWT exchange succeeds.
    - HLS live tiles play.
    - Trigger an ROI alert and confirm an APNs/FCM push arrives (foreground
      banner + backgrounded tray notification, tap → playback deep-link).

## Later: distribution

- TestFlight: archive in Xcode → upload to App Store Connect. Needs a distribution
  cert + provisioning profile (automatic signing handles most of this) and an app
  record in App Store Connect under the same bundle id.
