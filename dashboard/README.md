# Mori Dashboard

A standalone, offline-friendly static web dashboard for browsing mori shared memory instances. Single HTML file with vanilla JavaScript and no external dependencies — works anywhere.

## Quick Start

To test locally: `cd dashboard && python -m http.server 8080`, then open http://localhost:8080. Enter your mori instance base URL (e.g. http://localhost:8970) and API key in the settings modal. The dashboard connects to any mori instance's CORS-enabled read API (`/api/memories`, `/api/events`), retrieving memory entries and event logs with full-text search, filtering, and tagging.

## Deployment

Designed for Cloudflare Pages (or similar) hosting at a custom domain like `moriapp.dev`. No server-side logic required — all interaction is direct API calls to your mori instance with header-based authentication.
