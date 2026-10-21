# Life Dashboard

Web dashboard over the `nutrition-log` data folder, plus a todo list, published at https://life.selimd.me.
It never writes to `NUTRITION_DIR`; the todo list at `/todo` is the only thing that writes, to its own SQLite file. The data rules come from the data handoff; `log_entry.py` is the source of truth.

## Config

- `NUTRITION_DIR`, `NUTRITION_DAY_START_HOUR` and `NUTRITION_WEIGHT_UNIT` come from the `env` block of `~/nutrition-agent/.claude/settings.json`.
- Environment variables with the same names override it, and `LIFE_DASHBOARD_SETTINGS` points at a different settings file.
- Daily targets live in `config.toml` in this folder and are re-read on change.
- `TODO_DB` is the todo list's SQLite file, default `~/.local/share/life-dashboard/todo.sqlite`. The app refuses to start if it is inside `NUTRITION_DIR`.

## Layout on the VM

| Piece | Location |
| --- | --- |
| Deploy clone (never edit by hand) | `~/life-dashboard` |
| App service | `~/.config/systemd/user/life-dashboard.service`, `127.0.0.1:8090` |
| Auto-deploy | `life-dashboard-deploy.timer` runs `deploy.sh` every 2 minutes |
| Public access | Cloudflare Tunnel route `life.selimd.me` to `http://127.0.0.1:8090` |
| Todo list | `~/.local/share/life-dashboard/todo.sqlite`, the only copy (see Backup) |

## Workflow

```bash
# on the Mac
NUTRITION_DIR=~/path/to/Sync/nutrition TODO_DB=/tmp/todo.sqlite .venv/bin/uvicorn app:app --reload --port 8090
.venv/bin/python -m unittest
git commit -am "describe the change" && git push    # live within ~2 minutes
```

`deploy.sh` fast-forwards to `origin/main`, installs dependencies if `requirements.txt` changed, runs the tests, checks that `app.py` imports, restarts the service, and health-checks it. Any failure rolls back to the previous commit, and a failed commit is not retried until the next push.

```bash
# on the VM
~/life-dashboard/deploy.sh                 # deploy now instead of waiting
~/life-dashboard/deploy.sh --force         # redeploy the current commit
journalctl --user -u life-dashboard-deploy -n 20
journalctl --user -u life-dashboard -n 30
```

Rules: never edit `~/life-dashboard` on the VM, and never force-push `main`. Unit files are not deployed automatically; after changing one, copy it to `~/.config/systemd/user/` and run `systemctl --user daemon-reload`.

## Backup

The todo list lives only in `TODO_DB`. Copy it nightly with a crontab line on the VM (needs `apt install sqlite3`):

```bash
0 3 * * * sqlite3 ~/.local/share/life-dashboard/todo.sqlite ".backup $HOME/todo-backup.sqlite"
```

## Acceptance check

Compare the Today, Calories and Weight numbers with the read-only commands, run from `~/nutrition-agent`:

```bash
python3 .claude/skills/nutrition-log/scripts/log_entry.py totals
python3 .claude/skills/nutrition-log/scripts/log_entry.py totals --days 7
python3 .claude/skills/nutrition-log/scripts/log_entry.py weight show --days 30
python3 .claude/skills/nutrition-log/scripts/log_entry.py preset list
```
