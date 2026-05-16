import 'package:flutter/material.dart';

import 'auth.dart';

class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key, required this.auth});
  final AuthService auth;

  @override
  Widget build(BuildContext context) {
    final session = auth.session!;
    return Scaffold(
      appBar: AppBar(
        title: const Text('belfry'),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout),
            tooltip: 'sign out',
            onPressed: auth.signOut,
          ),
        ],
      ),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Icon(Icons.check_circle, size: 64, color: Colors.green),
              const SizedBox(height: 16),
              Text('signed in as ${session.email}'),
              const SizedBox(height: 8),
              Text(
                'jwt expires ${session.expiresAt.toLocal()}',
                style: const TextStyle(fontSize: 12, color: Colors.grey),
              ),
              const SizedBox(height: 32),
              const Text(
                'live grid + events screens coming in Phase C/D',
                style: TextStyle(color: Colors.grey),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
