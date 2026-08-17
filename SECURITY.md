# Security policy

FaceLink is alpha software. Do not expose the Blender bridge to another network interface;
it is designed to bind only to `127.0.0.1`. Treat instance-discovery records as secrets
because they contain a short-lived bearer token.

Please report vulnerabilities using GitHub's private vulnerability reporting rather than a
public issue. Include the affected FaceLink version, Blender version, reproduction steps and
whether untrusted local processes or files are required.

Never include a real model-provider API key in a report. FaceLink reads keys from process
environment variables and does not intentionally store them in `.blend` files.

