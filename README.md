# Life Dashboard

Web dashboard over the `nutrition-log` data folder, plus a todo list, published at https://life.selimd.me.
It never writes to `NUTRITION_DIR`; the todo list at `/todo` is the only thing that writes, to its own SQLite file. The data rules come from the data handoff; `log_entry.py` is the source of truth.

## Config

- `NUTRITION_DIR`, `NUTRITION_DAY_START_HOUR` and `NUTRITION_WEIGHT_UNIT` come from the `env` block of `~/nutrition-agent/.claude/settings.json`.
- Environment variables with the same names override it, and `LIFE_DASHBOARD_SETTINGS` points at a different settings file.
- Daily targets live in `config.toml` in this folder and are re-read on change.
- `TODO_DB` is the todo list's SQLite file, default `~/.local/share/life-dashboard/todo.sqlite`. The app refuses to start if it is inside `NUTRITION_DIR`.
- `LIFE_DASHBOARD_TOKEN` is the login token for the home page and `/todo` (see Login), set only in the environment, never in this repo.

## Login

The home page `/` and everything under `/todo` need a token; Calories, Macros, Weight and Presets stay public.
A signed-out visit shows a single password box in place; the right token sets a cookie for a year that holds an HMAC of the token, never the token itself.
Changing the token logs out every device.
If `LIFE_DASHBOARD_TOKEN` is not set, the app still starts, logs a warning, and nothing unlocks.

Generate a token (about 26 characters) and save it in your password manager:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(20)[:26])"
```

On the VM, put it in an environment file outside the repo; `life-dashboard.service` loads it with `EnvironmentFile=`.
Unit files are not deployed automatically, so copy the unit once after this change:

```bash
install -m 600 /dev/null ~/.config/life-dashboard.env
read -rs t && echo "LIFE_DASHBOARD_TOKEN=$t" > ~/.config/life-dashboard.env   # paste the token; it stays out of history
cp ~/life-dashboard/life-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user restart life-dashboard
```

To change the token, edit the file and restart the service.

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
NUTRITION_DIR=~/path/to/Sync/nutrition TODO_DB=/tmp/todo.sqlite LIFE_DASHBOARD_TOKEN=local-dev-token .venv/bin/uvicorn app:app --reload --port 8090
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
