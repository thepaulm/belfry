import Flutter
import UIKit
import FirebaseMessaging

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  // Retained so its method-call handler stays alive for the app's lifetime.
  private var cookieChannel: FlutterMethodChannel?

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    // With Flutter's implicit-engine model, plugins register on the engine's
    // own pluginRegistry (see didInitializeImplicitFlutterEngine) rather than
    // on the app delegate — so firebase_messaging is NOT wired into the
    // UIApplicationDelegate lifecycle and never kicks off APNs registration.
    // Do it ourselves, and forward the device token to Firebase in the
    // didRegister callback below. Without this, getToken() fails forever with
    // `apns-token-not-set`. Also claim the notification-center delegate so
    // foreground presentation + notification taps reach the plugin (the
    // implicit-engine registry doesn't wire that up either).
    UNUserNotificationCenter.current().delegate = self
    application.registerForRemoteNotifications()
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  override func application(
    _ application: UIApplication,
    didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
  ) {
    // Hand the APNs device token to Firebase. Use the plain property setter:
    // it both stores the token (so the Dart getAPNSToken()/getToken() succeed)
    // and auto-detects the APNs environment by reading the app's actual
    // aps-environment entitlement at runtime — so a development-signed build
    // correctly registers as sandbox. (setAPNSToken(_:type:) was tried and
    // left getAPNSToken() reporting `apns-token-not-set`.)
    Messaging.messaging().apnsToken = deviceToken
    super.application(
      application, didRegisterForRemoteNotificationsWithDeviceToken: deviceToken)
  }

  override func application(
    _ application: UIApplication,
    didFailToRegisterForRemoteNotificationsWithError error: Error
  ) {
    NSLog("belfry: APNs registration failed: \(error.localizedDescription)")
    super.application(
      application, didFailToRegisterForRemoteNotificationsWithError: error)
  }

  // Present alerts even while the app is foregrounded. We own the
  // UNUserNotificationCenter delegate (the implicit-engine plugin registry
  // doesn't wire firebase_messaging in), so the Dart-side
  // setForegroundNotificationPresentationOptions request never reaches the
  // system — without answering willPresent here, iOS shows nothing while the
  // app is open and only renders banners when backgrounded.
  override func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
  ) {
    if #available(iOS 14.0, *) {
      completionHandler([.banner, .list, .sound, .badge])
    } else {
      completionHandler([.alert, .sound, .badge])
    }
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    registerCookieChannel(engineBridge.pluginRegistry)
  }

  // iOS AVPlayer (HLS live + mp4 playback) will not attach our Authorization
  // header to its segment / range sub-requests, so those hit the server
  // unauthenticated. NSURLSession *does* send cookies on every request, and
  // AVURLAsset uses the shared cookie store — so we mirror the session JWT
  // into a `belfry_jwt` cookie that Caddy accepts (see cloud/Caddyfile).
  // Dart drives this over the `belfry/cookies` channel on sign-in/out.
  private func registerCookieChannel(_ registry: FlutterPluginRegistry) {
    guard let messenger = registry.registrar(forPlugin: "BelfryCookies")?.messenger() else {
      return
    }
    let channel = FlutterMethodChannel(name: "belfry/cookies", binaryMessenger: messenger)
    channel.setMethodCallHandler { call, result in
      switch call.method {
      case "set":
        guard let args = call.arguments as? [String: Any],
              let token = args["token"] as? String,
              let host = args["host"] as? String
        else {
          result(FlutterError(code: "bad_args", message: "expected token + host", details: nil))
          return
        }
        let props: [HTTPCookiePropertyKey: Any] = [
          .name: "belfry_jwt",
          .value: token,
          .domain: host,
          .path: "/",
          .secure: "TRUE",
        ]
        if let cookie = HTTPCookie(properties: props) {
          HTTPCookieStorage.shared.setCookie(cookie)
        }
        result(nil)
      case "clear":
        HTTPCookieStorage.shared.cookies?
          .filter { $0.name == "belfry_jwt" }
          .forEach { HTTPCookieStorage.shared.deleteCookie($0) }
        result(nil)
      default:
        result(FlutterMethodNotImplemented)
      }
    }
    cookieChannel = channel
  }
}
