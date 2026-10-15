# Changelog

## 0.6.0

- Remove the route and HTTP method whitelist; preserve incoming paths and queries when forwarding to the fixed ChatGPT origin.
- Apply the existing retry budget and backoff to every forwarded request, including images, model catalogs, writes, and new routes.
- Preserve binary, text, JSON array, empty, and HEAD responses while retaining model stream validation and request guards.
- Document repeated-operation risks, catalog delays during outages, and the boundary between gateway traffic and separate desktop or plugin connections.
- Advance the service revision to 8.

## 0.5.2

- Forward built-in image generation and editing routes to the fixed Codex upstream, resolving local `404 / Unknown route` errors.
- Keep image calls single-attempt while preserving authentication, payloads, size limits, browser-origin rejection, and redirect protection.
- Advance the service revision to 7; model and compaction retry policies remain unchanged.

## 0.5.1

- Retry HTTP 507 responses containing `exceeded request buffer limit while retrying upstream`, using the existing attempt limit and backoff.
- Preserve explicit non-retryable errors and avoid retrying other 507 responses solely on their status.
- Advance the service revision to 6 and add recovery, exhaustion, and exclusion regressions.

## 0.5.0

Initial public release.

- Buffered retries for temporary inference and remote compaction failures.
- Existing ChatGPT authentication and built-in OpenAI provider compatibility.
- Recognition of structured capacity errors, truncated gzip/JSON, invalid UTF-8, and SSE framing.
- Validated model catalog caching isolated by credential and account header.
- Windows sign-in task, configuration backups, guarded upgrades, and rollback.
- Regression tests, automated lint/format checks, source and wheel builds.
- MIT license.
