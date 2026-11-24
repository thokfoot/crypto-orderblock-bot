# Time-critical market workflow scheduling

## Why the workflows have time guards

GitHub Actions scheduled workflows are best-effort and can start late. The US and NSE scripts therefore reject delayed starts instead of scanning old candles and presenting a stale signal as live.

- US script accepts a live scan only from 10:03 to 10:15 in America/New_York.
- NSE script accepts a live scan only from 09:34 to 09:42 Asia/Kolkata.
- Both engines require the full 20-minute opening range and fresh completed 1-minute bars.
- Both monitors begin after the candidate is locked. They do not replay candles that closed before the monitor started.
- Both scripts send signal alerts only. They do not place broker orders.

## Optional external dispatch

The workflows accept GitHub `repository_dispatch` events:

- US: event type `us_scan`
- NSE: event type `nse_scan`

An external scheduler may POST to:

`https://api.github.com/repos/thokfoot/crypto-orderblock-bot/dispatches`

Headers:

```
Accept: application/vnd.github+json
Authorization: Bearer <fine-grained-PAT>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

US payload:

```json
{"event_type":"us_scan"}
```

NSE payload:

```json
{"event_type":"nse_scan"}
```

The token must be stored only in the scheduler's secret store, never in this repository. Use a fine-grained token restricted to this repository with the repository permission required to dispatch repository events (Contents: write).

Schedule the US event for 10:05 on weekdays using the America/New_York timezone. Schedule the NSE event for 09:36 on weekdays using Asia/Kolkata. Do not hard-code one UTC time for the US market because New York observes daylight saving time.

## Important limitation

External dispatch removes dependence on GitHub's scheduled-cron timing, but it does not guarantee that a GitHub-hosted runner starts within 5–10 seconds. GitHub can still queue workflow runs. For strict execution deadlines, use a dedicated always-on worker with its own health monitoring and market-data feed.

The repository's scheduled workflows remain as a fallback. If both a scheduled trigger and external dispatch occur, the workflow concurrency group serializes them and the script's time guard rejects a late duplicate.
