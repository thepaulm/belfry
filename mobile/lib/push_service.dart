import 'dart:convert';
import 'dart:io' show Platform;

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import 'api.dart';
import 'auth.dart';
import 'playback_screen.dart';

// Must match the channel id in AndroidManifest's
// `default_notification_channel_id` meta-data so backgrounded FCM
// `notification` messages and our foreground local notifications land on
// the same high-importance channel.
const _channelId = 'roi_alerts';

const _androidChannel = AndroidNotificationChannel(
  _channelId,
  'ROI Alerts',
  description: 'Someone or something entered a watched zone',
  importance: Importance.high,
);

// Backgrounded messages carry a `notification` block, so Android shows the
// tray banner itself via the default channel — this handler only exists so
// the plugin has a registered entry point and for any future data-only
// handling. Must be a top-level function (runs in its own isolate).
@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {}

/// Owns FCM: permission, token registration, and turning incoming alert
/// messages into an audible + visible signal plus a tap-to-playback
/// deep-link. One instance lives for the authenticated session.
class PushService {
  PushService({required this.auth, required this.navigatorKey})
    : _api = ApiClient(auth);

  final AuthService auth;
  final GlobalKey<NavigatorState> navigatorKey;
  final ApiClient _api;
  final FlutterLocalNotificationsPlugin _local =
      FlutterLocalNotificationsPlugin();

  // Bumped on every foreground alert so AlertsScreen can reload, and a
  // running unread count for the nav-tab badge (reset when the tab opens).
  final ValueNotifier<int> ping = ValueNotifier(0);
  final ValueNotifier<int> unread = ValueNotifier(0);

  String? _token;
  bool _started = false;
  bool _registeredThisSession = false;

  // Called by main on every auth change. Splits the one-time messaging
  // setup (_ensureStarted) from per-session token registration: sign-out
  // deregisters the token, so a sign-out → sign-in within the same app run
  // must re-register or push silently stays off until an app restart.
  Future<void> onAuthChanged() async {
    if (auth.session == null) {
      _registeredThisSession = false; // re-register on the next sign-in
      return;
    }
    await _ensureStarted();
    if (!_registeredThisSession) {
      _registeredThisSession = true;
      await _registerToken();
    }
  }

  Future<void> _ensureStarted() async {
    if (_started) return;
    _started = true;

    final messaging = FirebaseMessaging.instance;
    await messaging.requestPermission();

    // iOS-only; no-op on Android (we present foreground alerts ourselves
    // via flutter_local_notifications below).
    await messaging.setForegroundNotificationPresentationOptions(
      alert: true,
      badge: true,
      sound: true,
    );

    const initSettings = InitializationSettings(
      android: AndroidInitializationSettings('@mipmap/ic_launcher'),
    );
    await _local.initialize(
      initSettings,
      onDidReceiveNotificationResponse: (resp) =>
          _deepLink(_decodePayload(resp.payload)),
    );
    await _local
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >()
        ?.createNotificationChannel(_androidChannel);

    messaging.onTokenRefresh.listen((t) {
      _token = t;
      _register(t);
    });

    FirebaseMessaging.onMessage.listen(_onForeground);
    FirebaseMessaging.onMessageOpenedApp.listen((m) => _deepLink(m.data));

    // Cold start from a notification tap: handle once the navigator exists.
    final initial = await messaging.getInitialMessage();
    if (initial != null) {
      WidgetsBinding.instance.addPostFrameCallback(
        (_) => _deepLink(initial.data),
      );
    }

    auth.addSignOutHook(_deregister);
  }

  void clearUnread() => unread.value = 0;

  // -- token registration ---------------------------------------------
  Future<void> _registerToken() async {
    try {
      // On iOS the FCM token isn't available until APNs has handed the app a
      // device token (AppDelegate kicks off registration + forwards it). A
      // getToken() called too early in launch loses that race and throws
      // `apns-token-not-set`, so wait for the APNs token first. Returns
      // immediately once it's set; no-op on Android.
      if (Platform.isIOS) {
        var apns = await FirebaseMessaging.instance.getAPNSToken();
        for (var i = 0; i < 20 && apns == null; i++) {
          await Future.delayed(const Duration(milliseconds: 500));
          apns = await FirebaseMessaging.instance.getAPNSToken();
        }
      }
      _token = await FirebaseMessaging.instance.getToken();
      if (_token != null) await _register(_token!);
    } catch (e) {
      debugPrint('push: token fetch/register failed: $e');
    }
  }

  Future<void> _register(String token) async {
    try {
      await _api.registerDevice(token, Platform.isIOS ? 'ios' : 'android');
    } catch (e) {
      debugPrint('push: registerDevice failed: $e');
    }
  }

  Future<void> _deregister() async {
    if (_token == null) return;
    try {
      await _api.unregisterDevice(_token!);
    } catch (_) {}
  }

  // -- foreground presentation ----------------------------------------
  void _onForeground(RemoteMessage m) {
    final n = m.notification;
    final title = n?.title ?? _titleFor(m.data);
    final body = n?.body ?? _bodyFor(m.data);
    // Key the tray notification on the camera (31-bit positive int), not the
    // alert id, so a new alert for the same camera replaces the prior one
    // instead of stacking. Undismissed per-alert notifications otherwise pile
    // up and hit Android's hard 50-per-app cap, after which the system
    // silently drops every further push. Mirrors the per-camera `tag` set on
    // the backgrounded FCM path; the Alerts tab is the history of record.
    final cam = (m.data['cam'] ?? '').toString();
    final id = cam.isEmpty ? 0 : (cam.hashCode & 0x7fffffff);
    // iOS presents the foreground banner itself (setForegroundNotification-
    // PresentationOptions); flutter_local_notifications is Android-only here
    // (initialized with no Darwin settings), so only drive it on Android to
    // avoid a no-op/throw and a double banner.
    if (Platform.isAndroid) {
      _local.show(
        id,
        title,
        body,
        const NotificationDetails(
          android: AndroidNotificationDetails(
            _channelId,
            'ROI Alerts',
            importance: Importance.high,
            priority: Priority.high,
          ),
        ),
        payload: jsonEncode(m.data),
      );
    }
    unread.value += 1;
    ping.value += 1;
  }

  String _titleFor(Map<String, dynamic> d) =>
      '${d['class'] ?? 'object'} in ${d['roi'] ?? 'zone'}';
  String _bodyFor(Map<String, dynamic> d) => '${d['cam'] ?? ''}';

  Map<String, dynamic> _decodePayload(String? payload) {
    if (payload == null || payload.isEmpty) return const {};
    try {
      return (jsonDecode(payload) as Map).cast<String, dynamic>();
    } catch (_) {
      return const {};
    }
  }

  // -- deep-link to the playback moment -------------------------------
  Future<void> _deepLink(Map<String, dynamic> data) async {
    final camName = data['cam'] as String?;
    final tsStr = data['ts'] as String?;
    if (camName == null) return;
    final ts = double.tryParse(tsStr ?? '');
    final at = ts == null
        ? null
        : DateTime.fromMillisecondsSinceEpoch((ts * 1000).round());

    Camera? cam;
    try {
      final cams = await _api.getAllCameras();
      for (final c in cams) {
        if (c.name == camName) {
          cam = c;
          break;
        }
      }
    } catch (e) {
      debugPrint('push: camera lookup failed: $e');
    }
    if (cam == null) return;

    navigatorKey.currentState?.push(
      MaterialPageRoute(
        builder: (_) =>
            PlaybackScreen(auth: auth, camera: cam!, initialTs: at),
      ),
    );
  }
}
