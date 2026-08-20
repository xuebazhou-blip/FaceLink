# Security policy

FaceLink is alpha software. Do not expose the Blender bridge to another network interface;
it is designed to bind only to `127.0.0.1`. Treat instance-discovery records as secrets
because they contain a short-lived bearer token.

## Supported versions

Only the latest GitHub release receives security fixes. Versions older than 0.3.6 are not
supported. Blender 4.2 LTS and newer are in scope; Windows x64 is the only fully verified
platform in the current release.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/xuebazhou-blip/FaceLink/security/advisories/new)
rather than a public issue. Include the affected FaceLink version, Blender version, reproduction
steps and whether untrusted local processes or files are required. You should receive an initial
acknowledgement within seven days; remediation timing depends on severity and reproducibility.

Never include a real model-provider API key in a report. FaceLink reads keys from process
environment variables and does not intentionally store them in `.blend` files.
