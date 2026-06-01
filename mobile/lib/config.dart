// Configuration values that get baked into the build. Web client ID is
// passed via `--dart-define=BELFRY_WEB_CLIENT_ID=...` at build/run time
// so it isn't committed (it isn't a secret in the classic sense — the
// public part of a Google OAuth web client — but treating it as build
// config keeps it out of the repo).
class AppConfig {
  // Public frontdoor base URL. Passed via --dart-define so the real
  // domain stays out of the repo (the committed default is a placeholder);
  // set BELFRY_BACKEND_BASE in env.json.
  static const String backendBase = String.fromEnvironment(
    'BELFRY_BACKEND_BASE',
    defaultValue: 'https://example.com',
  );

  // The Web OAuth client ID is what Google embeds as the ID token `aud`,
  // and what the backend's BELFRY_GOOGLE_CLIENT_IDS allow-list checks.
  // Reused from oauth2-proxy on EC2.
  static const String webClientId = String.fromEnvironment(
    'BELFRY_WEB_CLIENT_ID',
  );

  static const Duration tokenRefreshSlack = Duration(hours: 12);
}
