# Changelog

## Unreleased

- Preserve the built-in OpenAI provider for Dot compatibility by changing only `openai_base_url`; migrate custom retry providers with backups and exact provider-line restoration.
- Keep logs, rollback state, and configuration backups beside the source under `local-diagnostics/transient-retry`, with `CODEX_RETRY_RUNTIME_DIR` support and read-only legacy-state migration.
- Decode gzip/deflate response copies for retry checks while preserving compressed bytes, and pass unknown encodings through.
- Preserve successful JSON/SSE at Python parser resource boundaries and avoid replaying success responses with false error fields.
- Preserve duplicate request headers on every attempt.
- Verify recovery through the built-in provider and native retry after one exhausted proxy request; document that native retries can multiply attempts and advance the service revision to 12.

- Remove local authentication/origin gates, request and response size caps, I/O timeouts, retry deadlines, and model catalog caching.
- Remove retry error-code allowlists and denylists; retry all HTTP errors and explicit JSON/SSE failures with the existing attempt count and backoff.
- Preserve upstream redirects, compressed payloads, and the last upstream error when retries are exhausted.
- Accept chunked uploads and absolute-form targets; relay HTTP upgrades instead of forcing WebSocket fallback.
- Upgrade existing managed configurations with backups and rollback, enabling WebSocket support and removing the extra stream timeout.

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
