# Option: Offload Heavy Tasks to a Grok Bot Agent via Webhook

## Idea
Keep SportNerdBot on GCP (`sportnerd-bot-vm`, e2-micro) as the main Telegram bot for all
chat traffic. Offload only heavy or rare tasks (video analysis, code/flow changes) to a
Grok Bot agent that is woken by a webhook routine.

## Flow (video analysis example)
1. A video / video note arrives in the Telegram group.
2. SportNerdBot POSTs a small JSON payload to the Grok Bot routine webhook URL:
   `{"chat_id": ..., "message_id": ..., "user": "...", "file_id": "...", "date": "..."}`
3. The Grok Bot agent wakes, downloads the file via the Telegram Bot API (`getFile`),
   validates it with `ffprobe` and runs pose estimation (MoveNet / MediaPipe) on its own
   machine (8 vCPU, 15 GB RAM, no GPU).
4. The result is returned either:
   - directly to the chat via Telegram `sendMessage` (bot token stored as a secret), or
   - back to SportNerdBot through a callback endpoint, so Gemini writes the verdict and
     calls `mark_participant_done`.

## Admin / flow changes
- Small data fixes (mark done, change a norm) stay in the bot via Gemini AFC: instant and cheap.
- Code / prompt / trigger changes go through a dev agent working on this repo, which tests
  changes and pushes to GitHub.
- Deployment: outbound SSH from the agent machine is blocked, so the VM should pull changes
  itself (GitHub Actions -> `git pull` + `systemctl restart sportnerdbot`, or a cron `git pull`).

## Trade-offs
| Pros | Cons |
|---|---|
| Strong CPU for CV, no load on e2-micro | Each wake consumes Grok Bot usage quota |
| No Gemini video tokens | Latency of seconds to minutes, not instant |
| Agent can also fix code and data | Extra moving part (webhook, secret token) |

**Recommendation:** use only for videos and admin/dev tasks, never for every chat message.
