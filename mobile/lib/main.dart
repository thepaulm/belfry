import 'package:flutter/material.dart';

import 'auth.dart';
import 'events_screen.dart';
import 'sets_screen.dart';
import 'sign_in_screen.dart';

void main() {
  runApp(const BelfryApp());
}

class BelfryApp extends StatefulWidget {
  const BelfryApp({super.key});

  @override
  State<BelfryApp> createState() => _BelfryAppState();
}

class _BelfryAppState extends State<BelfryApp> {
  final AuthService _auth = AuthService();

  @override
  void initState() {
    super.initState();
    _auth.bootstrap();
  }

  @override
  void dispose() {
    _auth.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'belfry',
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
              : _HomeShell(auth: _auth);
        },
      ),
    );
  }
}

class _HomeShell extends StatefulWidget {
  const _HomeShell({required this.auth});
  final AuthService auth;

  @override
  State<_HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<_HomeShell> {
  int _index = 0;

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
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.grid_view_outlined),
            selectedIcon: Icon(Icons.grid_view),
            label: 'Sets',
          ),
          NavigationDestination(
            icon: Icon(Icons.event_note_outlined),
            selectedIcon: Icon(Icons.event_note),
            label: 'Events',
          ),
        ],
      ),
    );
  }
}
