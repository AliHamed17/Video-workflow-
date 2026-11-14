# Setup: tools and credentials

You don't need every credential to start. With just one LLM and the `mock` renderer you can
run the whole pipeline locally. Add the others when you need them. Everything secret goes in
`.env` (template: `.env.example`). Run `vw doctor` to see what is detected.

## 1. Local tools

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or plain pip)
- ffmpeg with libass (`apt install ffmpeg` / `brew install ffmpeg`)

```bash
uv venv && . .venv/bin/activate && uv pip install -e ".[all,dev]"
vw init && vw doctor
```

## 2. LLMs (any subset)

| Provider | Where to get access | `.env` / config |
|---|---|---|
| Claude | console.anthropic.com → API keys (or `ant auth login`) | `ANTHROPIC_API_KEY` |
| OpenAI (ChatGPT models) | platform.openai.com → API keys | `OPENAI_API_KEY` |
| Gemini | aistudio.google.com → Get API key | `GEMINI_API_KEY` |
| OpenRouter / Groq / Ollama ... | the provider's console | declare under `llm.compat_providers`, key env var of your choice |
| Claude Code CLI (subscription) | `npm i -g @anthropic-ai/claude-code`, then `claude` to log in | route `cli:claude` |
| OpenAI Codex CLI (ChatGPT subscription) | `npm i -g @openai/codex`, then `codex login` | route `cli:codex` |
| Gemini CLI | `npm i -g @google/gemini-cli`, then `gemini` to log in | route `cli:gemini` |

Model ids change often. Update `llm.routes` in `config/channel.yaml` from each provider's
current model list.

## 3. Higgsfield (video)

1. Create a Higgsfield account and buy credits (a 30 s video at 1080p is roughly 480 credits on
   `seedance_2_5`; check with a cost preflight).
2. **Claude Code:** the repo's `.mcp.json` adds the server. Run `/mcp` and authenticate.
   **claude.ai:** add the Higgsfield connector.
3. Headless only: put a bearer token in `HIGGSFIELD_MCP_TOKEN` and set
   `video.generator: higgsfield_mcp`. Details in [HIGGSFIELD.md](HIGGSFIELD.md).

## 4. YouTube Shorts

1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen (External, add yourself as a test user) and create an
   **OAuth client ID** of type **Desktop app**.
3. Put `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` in `.env`, then run:
   ```bash
   vw auth-youtube      # opens the browser and prints YOUTUBE_REFRESH_TOKEN=...
   ```
4. Add the refresh token to `.env`.

Notes:
- Videos uploaded from an **unverified** API project are locked to private. To publish
  publicly, the project has to pass YouTube's API compliance audit. Until then keep
  `publish.youtube.privacy: private` and switch videos to public by hand.
- Each upload costs a large share of the default 10,000-unit daily API quota. Check the
  current cost per upload and request more quota in Cloud Console if you post often.
- Vertical videos up to 3 minutes are classified as Shorts automatically.

## 5. Instagram Reels

Requirements: an Instagram **professional** account (Business or Creator) and a Meta developer app.

1. developers.facebook.com → Create app → add the **Instagram** product.
2. Choose a login flavour:
   - **Instagram Login** (simplest, no Facebook Page): permissions
     `instagram_business_basic`, `instagram_business_content_publish`;
     set `INSTAGRAM_GRAPH_HOST=graph.instagram.com`.
   - **Facebook Login for Business** (account linked to a Page): permissions
     `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`;
     `INSTAGRAM_GRAPH_HOST=graph.facebook.com`.
3. Generate a **long-lived** access token (about 60 days; refresh it before it expires) and find
   your IG user id. Fill `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`, `INSTAGRAM_GRAPH_VERSION`.
4. For accounts other than your own (or production use), the app needs Meta App Review for
   the publishing permission.

The publisher uses resumable upload (`rupload.facebook.com`), so videos don't need a public
URL. API publishing is rate-limited per account per 24 h.

## 6. TikTok

1. developers.tiktok.com → create an app → add **Login Kit** and **Content Posting API**.
2. Scopes: `video.upload` (send to drafts) and/or `video.publish` (direct post).
3. Complete the OAuth flow for your account (your redirect URI must be registered on the app)
   to get an access token and refresh token. Fill `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`,
   `TIKTOK_ACCESS_TOKEN`, `TIKTOK_REFRESH_TOKEN`. Access tokens last ~24 h; the publisher
   refreshes them and saves the rotated tokens to `data/tiktok.token.json`.
4. **Unaudited apps** can only direct-post with `SELF_ONLY` visibility. Keep
   `publish.tiktok.mode: draft` (lands in your TikTok inbox to post from the app) until the app
   passes TikTok's audit, then switch to `direct`.

Alternative: Higgsfield's MCP can publish its own hosted renders to TikTok
(`tiktok_connect` / `tiktok_prepare_publish`), with a review form, from Claude.

## 7. GitHub Actions (optional automation)

Add the same keys as **repository secrets** (Settings → Secrets and variables → Actions), commit
your `config/channel.yaml`, and trigger **Content pipeline** from the Actions tab. It is manual
by default; uncomment the `schedule` block when you trust it. Headless rendering in Actions
needs `video.generator: higgsfield_mcp`.
