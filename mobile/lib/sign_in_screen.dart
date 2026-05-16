import 'package:flutter/material.dart';

import 'auth.dart';

class SignInScreen extends StatelessWidget {
  const SignInScreen({super.key, required this.auth});
  final AuthService auth;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: AnimatedBuilder(
              animation: auth,
              builder: (context, _) => Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Text(
                    'belfry',
                    style: TextStyle(fontSize: 36, fontWeight: FontWeight.w300),
                  ),
                  const SizedBox(height: 48),
                  FilledButton.icon(
                    onPressed: auth.busy ? null : auth.signIn,
                    icon: const Icon(Icons.login),
                    label: Text(
                      auth.busy ? 'signing in…' : 'sign in with Google',
                    ),
                  ),
                  if (auth.error != null) ...[
                    const SizedBox(height: 24),
                    Text(
                      auth.error!,
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Theme.of(context).colorScheme.error),
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
