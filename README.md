# Clipping Studio

A free, self-hosted, all-in-one live + VOD clipping studio. It watches streams,
finds viral moments with a dual-judge detector, cuts 9:16 clips with burned
captions, publishes them, and tracks clipping-campaign earnings — all from one
dashboard on `http://127.0.0.1:8765`.

Works with **no API keys** in demo mode. Add an OpenAI-compatible endpoint and
Whisper for transcript-aware detection, AI hooks, titles, and agent reports.

---

## What it does

- **Live monitor** — watches Twitch/YouTube streamers, records rolling audio +
  chat, and detects hype spikes as they happen.
- **Dual-judge detector** — a transcript judge and a loudness/chat judge must
  agree before a moment becomes a clip (same idea as PeakCut's live brain).
- **VOD pipeline** — paste any video URL or local file: download → transcribe →
  detect windows → cut → caption.
- **9:16 auto-reframe** — blurred background, centered foreground, face-friendly
  crop, burned word-by-word captions.
- **One-click publish** — YouTube Shorts (resumable upload), TikTok (chunked
  upload), Instagram Reels. No keys? It builds a ready-to-post package.
- **Whop earnings tracker** — campaigns, pay-per-view math, CSV import, RPM,
  daily chart, and 30-day projections.
- **11 creator agents** — hooks, titles, thumbnails, retention, channel audit,
  posting calendar, monetization, comments, competitors, repurposing.

---

## Requirements

- macOS, Windows, or Linux
- Python **3.10+**
- `ffmpeg` and `ffprobe` on your `PATH`
- `yt-dlp` on your `PATH` (or `python -m yt_dlp` via the venv)
- Optional: an OpenAI-compatible API endpoint for AI + Whisper

### macOS

```bash
brew install ffmpeg yt-dlp
```

### Windows

```powershell
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp
```

---

## Quick start

```bash
cd clipping-studio

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python app.py
```

Open **http://127.0.0.1:8765**.

Or use the launcher:

```bash
./start.sh          # macOS / Linux
start.bat           # Windows
```

Demo mode is on by default. You should see a "Demo Runner" streamer, simulated
loudness/chat, and a sample earnings chart.

---

## Configuration

Copy `.env.example` to `.env` and edit. Keys are read on startup and never sent
to the browser. `POST /api/settings/reload` reloads them without a restart.

### Your AI (optional)

Any OpenAI-compatible endpoint works:

```env
MY_AI_ENDPOINT_URL=https://api.groq.com/openai/v1
MY_AI_API_KEY=...
MY_AI_MODEL=llama-3.3-70b-versatile
MY_AI_FAST_MODEL=llama-3.1-8b-instant
```

Or point at a local server:

```env
MY_AI_ENDPOINT_URL=http://127.0.0.1:11434/v1
MY_AI_MODEL=llama3.2
```

Whisper defaults to the same endpoint:

```env
WHISPER_BASE_URL=
WHISPER_API_KEY=
WHISPER_MODEL=whisper-1
```

Provider shortcuts are also detected (`OPENAI_API_KEY`, `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `NVIDIA_API_KEY`, `TOGETHER_API_KEY`,
`FIREWORKS_API_KEY`, `MISTRAL_API_KEY`, `XAI_API_KEY`, `CEREBRAS_API_KEY`).

Without an endpoint, detection falls back to loudness + chat heuristics with
local keyword scoring. Clipping, captions, and publishing still work.

### Live recording

1. Add a Twitch or YouTube streamer in **Settings**.
2. Click **Start recording** on the Live tab.
3. For Twitch live checks, add `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET`
   (client credentials) or a `TWITCH_TOKEN`. Without them the monitor falls
   back to `yt-dlp` live checks.

### Publishing

| Platform | What you need |
|---|---|
| YouTube | OAuth client + refresh token (`YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`) |
| TikTok | `TIKTOK_ACCESS_TOKEN` (Content Posting API) |
| Instagram | `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`, and a public HTTPS `PUBLIC_BASE_URL` |

Set `ALLOWED_STREAMERS` to a comma-separated allowlist if you want to restrict
auto-posting to specific creators.

---

## Using the studio

### Live tab
- **Record** starts the rolling recorder (segmented audio + chat replay).
- **Clip now** cuts the last `SPIKE_PRE_SECONDS` and keeps `SPIKE_POST_SECONDS`.
- Detected spikes appear automatically (cooldown + per-stream limits apply).

### VOD tab
- Paste a URL or choose a local file and press **Process**.
- The pipeline downloads, transcribes, detects windows, then renders clips.
- Set `MAX_VOD_MINUTES` to cap long VODs.

### Clips tab
- Preview, download, copy caption, or build a post package.
- **Publish** posts to the selected platform when credentials exist.

### Earnings tab
- Add a campaign (`rate` per `per` views) and log views.
- Import a Whop or platform CSV — headers are matched loosely.
- `revenue_override` lets you paste the actual payout.

### AI Agents tab
- Pick an agent, give it a topic or clip context, run it.
- Reports are saved to `agent_outputs/` and shown in the dashboard.

---

## Project layout

```
clipping-studio/
├── app.py            Flask server + API routes
├── config.py         env loading, settings, directories
├── state.py          thread-safe in-memory + JSON state
├── monitor.py        always-on live monitor and demo ticker
├── recorder.py       segment recorder, loudness analysis, stitching
├── chat.py           live chat capture + replay download
├── transcriber.py    Whisper API + subtitle parsing
├── detector.py       dual-judge live/VOD detection
├── clipper.py        9:16 render, captions, thumbnails
├── captioner.py      ASS/SRT word captions
├── vod_mode.py       download → detect → render pipeline
├── campaigns.py      campaigns, earnings, CSV import, summary
├── publisher.py      YouTube / TikTok / Instagram uploads
├── agents/           11 creator agents
├── static/           CSS + vanilla JS dashboard
├── templates/        dashboard HTML
├── data/             streamers, campaigns, earnings, packages
├── clips/            rendered media
├── recordings/       rolling and VOD audio
├── transcripts/      saved transcripts
├── agent_outputs/    agent markdown reports
└── tests/            smoke tests
```

---

## Tests

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

Tests cover imports, state snapshots, detection heuristics, earnings math, and
read-only API routes. They do not require network access.

---

## Troubleshooting

**`ffmpeg` not found** — install it and confirm `ffmpeg -version`. Set
`FFMPEG_BIN` to an absolute path if needed.

**`yt-dlp` fails or returns 403** — upgrade it (`pip install -U yt-dlp` or
`brew upgrade yt-dlp`). Some platforms require cookies for logged-in content.

**No transcripts** — check `MY_AI_ENDPOINT_URL` + `WHISPER_API_KEY`. The
detector still works from loudness and chat alone.

**Instagram upload fails** — Instagram downloads the media from Meta's side, so
`PUBLIC_BASE_URL` must be a reachable HTTPS URL for this machine (tunnel or
reverse proxy).

**Captions look plain** — the studio uses Arial Black when present
(`/System/Library/Fonts/Supplemental/Arial Black.ttf` on macOS) and falls back to
DejaVu Sans.

**Port already in use** — set `PORT=8766` in `.env`.

---

## Credits and licenses

This project is MIT licensed (see `LICENSE`). It was designed after studying
these MIT-licensed projects; their code informed the architecture but is not
bundled:

- **PeakCut** — live dual-judge detection model.
- **OpenShorts** — VOD pipeline, reframing, caption styling.
- **ClipIT** — desktop clipping workflow inspiration.

Only clip and republish content you have permission to use. You are responsible
for complying with platform terms and campaign rules.
