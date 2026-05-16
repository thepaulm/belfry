import 'package:flutter/material.dart';

import 'auth.dart';
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
              : SetsScreen(auth: _auth);
        },
      ),
    );
  }
}
