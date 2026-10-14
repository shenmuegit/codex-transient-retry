# Changelog

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
