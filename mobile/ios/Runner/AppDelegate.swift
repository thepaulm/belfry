import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  // Retained so its method-call handler stays alive for the app's lifetime.
  private var cookieChannel: FlutterMethodChannel?

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
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
