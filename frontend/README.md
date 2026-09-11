# KeyAcross web console

React 19 + TypeScript + Vite. The interface supports Chinese and English, responsive navigation, and the three account roles defined in requirement V2. All authorization and ownership checks are also enforced by FastAPI.

## Development

```sh
npm ci
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` to `http://127.0.0.1:8000`. Override the destination with the server-side `API_PROXY_TARGET` environment variable when starting Vite. Keep the backend configured with the browser origin in its CORS/CSRF allowlist.

There is no public signup or fixed login password. Use the account initialized during backend deployment, then create administrator and member accounts through the permitted hierarchy.

## Validation and production

```sh
npm run build
npm run format:check
npm audit --omit=dev --registry=https://registry.npmjs.org
```

The production build is served by the included multistage Docker image. Nginx forwards `/api` to the Compose service `api:8000`, serves SPA navigation, and caches hashed assets. Page modules load on demand through route-level lazy imports; Nginx applies gzip to eligible static HTML, CSS, JavaScript, and SVG responses. `index.html` is not cached. Deploy behind a TLS reverse proxy and enable secure cookies in the backend for production.

## Data and security behavior

- Authentication uses HttpOnly cookies and an in-memory CSRF token; write requests send `X-CSRF-Token`.
- Only the language preference is kept in browser local storage. Keys, passwords, seller tokens and payment data are never persisted there.
- Full-key reads use the dedicated audited endpoint. Raw key display is cleared when its dialog closes.
- User management requests one server page of 50 accounts through `/users?view=management&search=...&offset=...&limit=50`; it never implicitly downloads the remaining pages. Channels and settlement history also use server pagination and the shared pagination control. The usage-fact API retains server pagination.
- Known missing remote capabilities remain explicit; no production charts or amounts are seeded with examples.
- Channel consumption comes from background scheduled synchronization. Refresh only reads saved local data, and settlement previews/confirmations do not trigger a remote sync. Manual Sync controls and sync resubmission are removed. Missing successful observations remain unknown, not zero; see [automatic synchronization](../docs/AUTOMATIC_SYNC.md).
- User management opens rate cards, group settlement and per-user history dialogs. The settlement-history page and dialogs share immutable order headers/details. A valid 0% rate can produce a zero-amount order; new settlements use incremental remote consumption and current rates. Legacy bills, manual payment registration and history-backfill UI are removed.
- The ledger provides evidence-backed imports only to superadministrators. It does not infer consumption from seller identity or private request logs.

The backend is the final authority for credential formats, remote capabilities, file checks, rules and settlement amounts.

## Request lifecycle and page data

- `api(path, method, data, signal)` accepts an optional `AbortSignal`. The shared `useData` hook cancels superseded reads and reads from unmounted components. Its data is isolated by request path and authenticated session, with no shared response cache; stale completions cannot replace a newer resource or session.
- Authentication changes invalidate responses from earlier sessions before they can update the in-memory CSRF token or emit a session-expired event. Cancellation is not presented as a request failure.
- User management and channel searches wait 250 ms after editing stops before requesting a new page. Filters reset pagination; an out-of-range page after a refresh moves to the last valid page.
- Only the active channel tab requests its main list: tag groups use `/channels`, list details use `/channel-distributions`. Shared category/filter data remains separately scoped. Refreshing or closing a task refreshes the active view; switching tabs loads the newly active view.
- A `/users?account_id=...` settlement dialog can resolve an account outside the current page using `/users?view=management&user_id=...&limit=1`, without fetching every account. The backend applies the same account permissions to this lookup.
- Settlement history, its per-user dialog, and settlement-order details use the same `useData` lifecycle. History search remains an explicit Search-button/Enter action, and the per-user dialog keeps its own search and page state.
- Both `/users` views (`full` and `management`) now return current management fields without old billing aggregates. The initial request optimization needed no migration; the subsequent scope reduction retires nine empty tables with a nonempty-data guard. Current order history and its JSON baselines remain intact. See [current functions and schema](../docs/CORE_FUNCTIONS.md).
