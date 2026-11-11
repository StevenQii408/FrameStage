# Security

FrameStage is a **local, single-user developer preview**. Keep the backend on loopback. Project ownership checks separate workspaces; they are not user authentication.

## Local API session

Normal startup enables these independent checks:

1. The request Host must match `STORYBOARD_ALLOWED_HOSTS`, defaulting to `127.0.0.1,localhost,::1`.
2. Browser writes from an untrusted Origin are rejected on the server, including simple form submissions. CORS is an additional browser read policy, not the write authorization mechanism.
3. `POST`, `PUT`, `PATCH` and `DELETE` require `X-FrameForge-Session`.

The frontend bootstraps `GET /api/session` from an allowed browser Origin, keeps the token in memory, and sends it in this header. A backend restart rotates the random token; the frontend may bootstrap and retry once **only** after an explicit token rejection. A network timeout is not permission to repeat a potentially submitted operation.

The token is separate from the Bailian API Key. It is not placed in localStorage, URLs, media links or health responses. Bootstrap responses are `Cache-Control: no-store`; ordinary navigation and unauthenticated CLI requests cannot retrieve the token. A same-origin frontend fetch is supported, but a trusted Origin remains a local trust decision rather than proof of a remote user's identity.

Read-only media, health and SSE endpoints do not require this token. Do not expose the service publicly assuming that write protection also authenticates every read. Requests originating from a local process are outside browser Origin enforcement: a process that can forge headers or read your private configuration is not isolated by this scheme.

## CLI access

For a local CLI client, generate a dedicated random token and store it privately as `STORYBOARD_LOCAL_API_TOKEN` in `backend/.env`, then restart the backend. Use at least 32 random characters. For example, `python -c 'import secrets; print(secrets.token_urlsafe(32))'` generates a value; do not paste it into an issue, source file or command-line URL.

The client must load that same value into its private environment and send `X-FrameForge-Session`. Example from the backend directory, with its virtual environment activated and the token already set in the process environment:

```python
import os
import httpx

response = httpx.post(
    "http://127.0.0.1:8000/api/projects",
    headers={"X-FrameForge-Session": os.environ["STORYBOARD_LOCAL_API_TOKEN"]},
    json={"name": "Local project"},
)
response.raise_for_status()
```

Do not use your cloud API Key as a local session token. `app.cloud_setup` and provider-only evaluation tools do not need a local API session because they do not submit HTTP requests to this backend.

`STORYBOARD_LOCAL_AUTH=0` disables token validation **only for deliberately isolated tests**; Host and Origin checks still apply. Do not use this setting for normal operation. The test suite configures its own explicit isolation; there is no automatic bypass based on Mock mode or the `testserver` hostname.

Custom frontend ports require an exact `STORYBOARD_CORS_ORIGINS` allowlist. Custom server hostnames require an exact `STORYBOARD_ALLOWED_HOSTS` entry. Do not use wildcards. Configuration examples are in `backend/.env.example`.

## Credentials, media and model requests

- Keep `backend/.env`, database files, original media, extracted frames and evaluation responses outside source control. The setup command saves private credentials with restricted file permissions.
- Analysis sends selected frames and text to the configured cloud provider. Confirm your right to upload the material and the provider's data terms before using sensitive media. Local storage does not mean cloud inference is offline.
- Cancellation stops further eligible work and publication; a request already sent may have incurred charges. Unknown usage or billing must remain unknown, not be reported as free or refunded.
- A valid frame reference proves that the evidence exists, not that every model claim is true. Review important creative, factual or commercial decisions.
- Generated assets and local image uploads are untrusted files. Keep media libraries patched and do not enable arbitrary URL or filesystem ingestion in a future provider adapter.
- Image/video generation is disabled by default. Enabling the cloud adapter can submit billable tasks; configure its credentials only on the backend. Test providers belong only to isolated test fixtures; do not mount them in a normal server.

## Network deployment

Before exposing the application outside the machine, add real identity and access control for reads **and** writes, TLS, appropriate rate/queue limits, a restrictive reverse proxy, per-user data ownership and secret management. Replacing the default Host allowlist is not sufficient. Do not expose the Vite development server as a production web server.

## Reporting

For a suspected security issue, use the repository's private vulnerability-reporting channel if enabled, or contact its maintainers privately. Include affected versions, minimal reproduction and impact without credentials or private media. Avoid publishing an active exploit with real keys or personal project data. This repository does not currently promise a response SLA or completed external security audit.
