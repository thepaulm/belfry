import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';

import 'alerts_screen.dart';
import 'auth.dart';
import 'events_screen.dart';
import 'push_service.dart';
import 'sets_screen.dart';
import 'sign_in_screen.dart';

// Used by PushService to navigate from a notification tap (which arrives
// outside any screen's BuildContext) and to push the playback deep-link.
final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // Firebase backs FCM push. Degrade gracefully: if it can't initialize
  // (e.g. a build without google-services.json wired up), the rest of the
  // app still runs — just without alert push.
  try {
    await Firebase.initializeApp();
    FirebaseMessaging.onBackgroundMessage(firebaseMessagingBackgroundHandler);
  } catch (e) {
    debugPrint('Firebase init failed; push disabled: $e');
  }
  runApp(const BelfryApp());
}

class BelfryApp extends StatefulWidget {
  const BelfryApp({super.key});

  @override
  State<BelfryApp> createState() => _BelfryAppState();
}

class _BelfryAppState extends State<BelfryApp> {
  final AuthService _auth = AuthService();
  late final PushService _push = PushService(
    auth: _auth,
    navigatorKey: navigatorKey,
  );

  @override
  void initState() {
    super.initState();
    _auth.addListener(_onAuthChanged);
    _auth.bootstrap();
  }

  // Drive push off every auth transition — device registration is a bearer
  // call so it can't run before sign-in, and a sign-out → sign-in must
  // re-register the token. PushService dedupes the work internally.
  void _onAuthChanged() {
    _push.onAuthChanged();
  }

  @override
  void dispose() {
    _auth.removeListener(_onAuthChanged);
    _auth.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'belfry',
      navigatorKey: navigatorKey,
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.blueGrey),
      home: AnimatedBuilder(
        animation: _auth,
        builder: (context, _) {
          if (!_auth.initialized) {
            return const Scaffold(
              body: Center(child: CircularProgressIndicator()),
            );
          }
          return _auth.session == null
              ? SignInScreen(auth: _auth)
              : _HomeShell(auth: _auth, push: _push);
        },
      ),
    );
  }
}

class _HomeShell extends StatefulWidget {
  const _HomeShell({required this.auth, required this.push});
  final AuthService auth;
  final PushService push;

  @override
  State<_HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<_HomeShell> {
  int _index = 0;

  void _onSelect(int i) {
    setState(() => _index = i);
    // Opening Alerts acknowledges the pending pushes.
    if (i == 2) widget.push.clearUnread();
  }

  @override
  Widget build(BuildContext context) {
    // IndexedStack keeps each tab alive so video state (and events scroll
    // position) survives tab switches — switching from Events to Sets and
    // back shouldn't re-fetch the events list or reset the grid.
    return Scaffold(
      body: IndexedStack(
        index: _index,
        children: [
          SetsScreen(auth: widget.auth),
          EventsScreen(auth: widget.auth),
          AlertsScreen(auth: widget.auth, push: widget.push),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: _onSelect,
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.grid_view_outlined),
            selectedIcon: Icon(Icons.grid_view),
            label: 'Sets',
          ),
          const NavigationDestination(
            icon: Icon(Icons.event_note_outlined),
            selectedIcon: Icon(Icons.event_note),
            label: 'Events',
          ),
          NavigationDestination(
            icon: ValueListenableBuilder<int>(
              valueListenable: widget.push.unread,
              builder: (_, n, child) => Badge(
                isLabelVisible: n > 0,
                label: Text('$n'),
                child: child,
              ),
              child: const Icon(Icons.notifications_outlined),
            ),
            selectedIcon: const Icon(Icons.notifications),
            label: 'Alerts',
          ),
        ],
      ),
    );
  }
}
