# Static Message Editor Dashboard

This folder contains a minimal Flask helper app and a Tailwind-powered HTML editor
for managing the static Discord channel content that `/set_static` and
`/update_static` maintain in `main.py`.

## Hosting on GitHub Pages
The dashboard is a static HTML file, so you can host it directly on GitHub Pages:
1. Push these files to a public repo.
2. In GitHub, go to **Settings → Pages**, choose **Deploy from a branch**, pick the branch (e.g., `main`), and set the folder to `/` (root).
3. The site will appear at `https://<username>.github.io/<repo>/`.
4. Point the dashboard’s API calls to your backend (e.g., your Cybrance/Cloudflare URL).

## Files
- `editor.html` – Single-file dashboard that mirrors Discord’s dark UI and
  shows live client-side chunk previews.
- `server.py` – Lightweight Flask server that serves the editor and exposes JSON
  endpoints to read/write `static_message.json`.
- `channel_names.json` (optional) – Provide manual channel name overrides if the
  helper cannot fetch names from Discord (one `{"channel_id": "friendly-name"}` map).

## Running the helper
1. Install Flask if it is not already available:
   ```bash
   pip install flask
   ```
2. Start the helper:
   ```bash
   python web_dashboard/server.py
   ```
3. Open http://localhost:5000/ to use the editor.

## API overview
- `GET /` – Serve the editor UI.
- `GET /api/channels` – Raw contents of `static_message.json`.
- Discord channel names are pulled automatically when `DISCORD_BOT_TOKEN`
  (and optionally `GUILD_ID`) are present. Add manual overrides by editing
  `channel_names.json` if some channels should display a custom label.
- If `COMMAND_LOG_CHANNEL_ID` is set, every publish from the dashboard is
  logged to that Discord channel for auditing.
- `POST /api/preview` – Returns the server-side chunking (matching
  `_split_for_discord`) for any `{title, content}` payload.
- `POST /api/save` – Upserts a channel entry in `static_message.json` and
  returns how many Discord messages it will occupy.

The helper only updates the JSON file. You can trigger the actual Discord
messages by either:
1. Running `/update_static` inside Discord after saving, or
2. Wiring a small bot-side hook that listens for file changes and calls
   `StaticMessageCog.apply_static_message_update`.
