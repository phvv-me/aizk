# aizk web

SvelteKit frontend for AIZK. Signs users in through Logto and reads everything else from the aizk browser API with the caller's access token.

Run from the monorepo root with `chefe run aizk-web-dev`, `chefe run aizk-web-build` and `chefe run aizk-web-check`.

Runtime configuration comes from `AIZK_LOGTO_URL`, `AIZK_WEB_CLIENT_ID`, `AIZK_WEB_CLIENT_SECRET`, `AIZK_WEB_PUBLIC_URL`, `AIZK_WEB_SESSION_SECRET`, `AIZK_WEB_API_URL` and `AIZK_MCP_PUBLIC_URL`. File uploads request only a capability grant through a form action and the browser PUTs the raw bytes to that same-origin capability path, so the adapter-node `BODY_SIZE_LIMIT` stays at its 512K default.
