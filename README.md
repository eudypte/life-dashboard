# Life Dashboard

Life Dashboard exists to make logging food, weight, and reading effortless: I chat with an agent from my phone or desktop, and it records the entry.
The dashboard then shows the results, along with my shows and todo list, on one page.
It is built with FastAPI and Jinja, styled like a terminal, and runs on an Oracle Cloud Always Free VM.

## What it does

- **Logging** - log food, weight, and reading by chatting with a Claude Code agent on the VM, from phone or desktop (see [Logging](#logging)).
- **Today** - what I ate today, with calories and protein against daily goals, the last 7 days, and a 30-day weight chart.
- **Calories** and **Macros** - daily totals and protein/carbs/fat split over 7, 30, or 90 days.
- **Weight** - weigh-ins, trend chart, and stats in kg or lb.
- **Todo** - a task list with projects, labels, priorities, due dates, and an upcoming-week view.
- **Reading** - books in progress, finished books, pages per day, and reading streaks.
- **Media** - shows and movies I am watching, with one-click episode progress and a finished list.
- **Themes** - ten color themes (Solarized, Gruvbox, Nord, Dracula, Tokyo Night, Amber CRT, and more) in IBM Plex Mono.

The site is public, but anyone who is not signed in sees a censored view.
Food names, carbs, fat, and todo titles are blacked out, and Calories, Macros, Weight, and Presets need a login.
Signing in with a secret token unlocks everything and the edit controls.

## Logging

A Claude Code remote session runs on the same VM, so I can log from my phone or desktop in seconds.
Its `log` skill turns a label photo, a food name, a scale photo, or "on page 120" into an entry, looks up restaurant and packaged food nutrition from official sources, and supports one-word presets and undo.
Every write goes through one script that appends to the CSV files, so the agent never edits them by hand or does its own math.

## How it works

Nutrition, weight, reading, and media entries are plain CSV files in a data folder that the logging agent writes to.
The dashboard only reads that folder and never changes it.
Todos and media actions taken in the dashboard are saved to a separate SQLite file.
Daily calorie and protein goals live in [config.toml](config.toml).

## Run it locally with fake data

Needs Python 3.11 or newer.
This creates throwaway example data in a temp folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

demo="$(mktemp -d)" && mkdir "$demo/nutrition"
printf 'id,time,food,source,calories,protein_g,carbs_g,fat_g\n1,08:00,Oatmeal,manual,320,12,48,9\n' > "$demo/nutrition/$(date +%F).csv"
printf 'id,date,time,weight,unit,weight_kg\n1,%s,07:30,70,kg,70\n' "$(date +%F)" > "$demo/nutrition/weight.csv"

NUTRITION_DIR="$demo/nutrition" TODO_DB="$demo/todo.sqlite" \
LIFE_DASHBOARD_SETTINGS="$demo/none.json" LIFE_DASHBOARD_TOKEN=demo-token \
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8090
```

Open http://127.0.0.1:8090 to see the public view, then log in with `demo-token` to see everything.

Run the tests with `.venv/bin/python -m unittest`.

## Configuration

All settings are environment variables, listed with example values in [.env.example](.env.example).
The app does not load `.env` files itself, so export them in your shell or service.
Generate a real token with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.

## Deploying

`life-dashboard.service`, `life-dashboard-deploy.timer`, and `deploy.sh` are the systemd setup that deploys it: it pulls `main` every two minutes, runs the tests, restarts the app, and rolls back if it fails.
Behind HTTPS, set `LIFE_DASHBOARD_ORIGIN` to your public URL, or logins and edits will be refused.
Keep the data folder and SQLite file outside the checkout, and back up the SQLite file.

## License

[MIT](LICENSE).
The bundled IBM Plex Mono font is under the [SIL Open Font License 1.1](static/OFL.txt).
