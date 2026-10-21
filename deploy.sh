#!/usr/bin/env bash
# Deploy the latest origin/main if it changed: install deps when needed, run tests,
# restart the service, health-check, and roll back on any failure.
# Usage: ./deploy.sh            (no-op when already up to date)
#        ./deploy.sh --force    (redeploy the current commit)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

SERVICE=life-dashboard
HEALTH_URL=http://127.0.0.1:8090/calories  # public; / and /todo answer with the login form when signed out

git fetch --quiet origin main
old=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)
failed_file=.git/deploy-failed
if [[ "${1:-}" != "--force" ]]; then
  [[ "$old" == "$new" ]] && exit 0
  # Don't retry a commit that already failed; wait for a new push.
  [[ -f "$failed_file" && "$(cat "$failed_file")" == "$new" ]] && exit 0
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "refusing to deploy: uncommitted changes in $(pwd)" >&2
  exit 1
fi

install_deps() {
  [[ -x .venv/bin/python ]] || python3 -m venv .venv
  .venv/bin/pip install --quiet -r requirements.txt
}

healthy() {
  for _ in 1 2 3 4 5; do
    sleep 1
    [[ "$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)" == 200 ]] && return 0
  done
  return 1
}

rollback() {
  echo "deploy of ${new:0:7} failed: $1; rolling back to ${old:0:7}" >&2
  echo "$new" > "$failed_file"
  git reset --hard --quiet "$old"
  install_deps
  systemctl --user restart "$SERVICE" || true
  exit 1
}

git merge --ff-only --quiet origin/main || { echo "cannot fast-forward to origin/main" >&2; exit 1; }

if [[ ! -x .venv/bin/python ]] || ! git diff --quiet "$old" "$new" -- requirements.txt; then
  install_deps || rollback "dependency install"
fi

if ! out=$(.venv/bin/python -m unittest --quiet 2>&1); then
  echo "$out" | tail -n 20 >&2
  rollback "tests"
fi

if ! out=$(.venv/bin/python -c "import app" 2>&1); then
  echo "$out" | tail -n 5 >&2
  rollback "app import"
fi

systemctl --user restart "$SERVICE" || rollback "service restart"
healthy || rollback "health check"

rm -f "$failed_file"
echo "deployed ${new:0:7}: $(git log -1 --format=%s)"
