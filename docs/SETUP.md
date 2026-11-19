# Setup: OpenShorts on your GPU with Claude

`install.sh` clones [OpenShorts](https://github.com/mutonby/openshorts) into `openshorts/` at
commit `0069894`, applies `patches/openshorts/openshorts.patch`, and adds two files. It runs
in Docker on an NVIDIA GPU and uses **Claude** to pick the best moments from the transcript.

## What the patch changes in OpenShorts

| File | Change |
|---|---|
| `llm_backend.py` | Two Claude options. **`LLM_PROVIDER=claude-code`**: each scoring/detail call runs one headless `claude -p` (the official Claude Code CLI) with `--json-schema`, no tools, no MCP, no settings, so it runs on your **Claude Pro/Max subscription**. **`LLM_PROVIDER=anthropic`**: the same calls through the Anthropic API with the `anthropic` SDK. Both are validated against OpenShorts' own schemas. |
| `Dockerfile` | One added line: `pip install anthropic` (for the API option). |
| `app.py`, `main.py` | Two new job fields, `brief` and `must_say`, and three small hooks into the moment picker. |
| `campaign_brief.py` (new) | Adds the campaign's rules to Claude's prompts and makes sure required words are spoken inside every clip. |
| `docker-compose.override.yml` (new) | GPU build (`GPU: "1"`), NVIDIA device with NVENC, loads `.env`, mounts your Claude Code program read-only into the backend, runs the backend as your own user so it can write `uploads/` and `output/`. |

Claude only sees transcript text (never video): the scoring pass, and the detail pass that
writes titles, descriptions and hooks.

## Settings (`openshorts/.env`)

| Setting | Example | Notes |
|---|---|---|
| `LLM_PROVIDER` | `claude-code` | `claude-code` = your Claude subscription, `anthropic` = API key, unset = Gemini |
| `CLAUDE_CODE_OAUTH_TOKEN` | (your token) | `claude setup-token`, sign in, paste the printed token. Never share it |
| `ANTHROPIC_API_KEY` | (empty) | Only for `LLM_PROVIDER=anthropic` (pay-as-you-go, separate from Pro/Max) |
| `LLM_MODEL` | `claude-haiku-5-5` | Lightest on your limits; `claude-sonnet-5-5` judges better and uses more |
| `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE` | `large-v3-turbo` / `cuda` / `int8_float16` | GPU transcription sized for 6 GB |
| `FFMPEG_ENCODER` | `auto` | NVENC on the GPU, x264 fallback |

**Cost on the subscription:** nothing extra. A one-hour stream is roughly 6-8 Claude calls
(about 1.5K tokens in and 0.5K out each). They count toward the same usage limits as your
chats and Claude Code; when a limit is hit the job stops with "usage limit reached", so run
it again after the reset. On the API, Claude Haiku 5.5 costs about $0.01 per hour of footage.

The subscription option is for running the kit yourself with your own account, under
[Anthropic's terms](https://www.anthropic.com/legal/consumer-terms). If you build a service
for other people on top of it, use an API key.

## The GPU

- Install the NVIDIA driver and the NVIDIA Container Toolkit
  (`setup/install_nvidia_container_toolkit.sh` for Ubuntu), then restart Docker.
- Check: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
- The first job downloads the Whisper model once (cached in `openshorts/.cache/`).
- **If transcription suddenly falls back to the CPU** ("no CUDA-capable device" in the job
  log, very slow jobs), the container lost the GPU: `docker compose restart backend` inside
  `openshorts/`. Check with
  `docker exec openshorts-backend python3 -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"` (should print 1).

## Start and stop

```bash
cd openshorts
docker compose up -d --build backend frontend   # dashboard: http://localhost:5175
docker compose logs -f backend                  # watch a job
docker compose down                             # stop
```

## Without a Gemini key

OpenShorts' frame-based extras (automatic layout picker, on-screen content detector,
clipping silent videos) need Gemini. The kit doesn't use them: its own layouts replace the
layout picker.

## Campaign-safe habits

- Clip only the streams and time window the campaign allows.
- Auto hook line off; captions on unless the campaign bans text.
- No dubbing or AI actors. The kit never posts: review each clip and post it yourself.
