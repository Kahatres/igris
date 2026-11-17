# Security policy

## Supported versions

Only the latest commit on `main` is supported. Fixes are not backported.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub Security Advisories: open the
repository's **Security** tab and choose **Report a vulnerability**. Do not open a public
issue, pull request or discussion for a security problem. Include the affected commit, the
steps to reproduce and the impact you expect.

## Deployment boundary

Igris is a single-owner, self-hosted assistant. The web server binds to loopback
(`127.0.0.1`) by default. Exposing it to a network or the internet is unsupported unless it
sits behind TLS (for example a reverse proxy with `IGRIS_PUBLIC_ORIGIN` set to its HTTPS
origin) and every request goes through the built-in login. Reports that require an
unsupported deployment, or an attacker who already controls the owner's machine or OS
account, are out of scope.

## Security rules

These rules apply to all code in this repository and are what a report should be measured
against:

- **Owner-scoped data.** Every stored item belongs to one account; one account can never
  read or change another account's data.
- **Untrusted content never acts on its own.** Mail, web pages, uploaded files and model
  output are data, not instructions; they never trigger an action (sending mail, writing
  files, running commands) without the owner's explicit approval.
- **No secrets in the repository.** Settings come from `IGRIS_*` environment variables;
  secrets live in the OS keyring (see `.env.example`).
- **No paid usage by default.** Model calls that would bill an API account instead of a
  subscription are blocked unless `IGRIS_ALLOW_PAID=1` is set.
