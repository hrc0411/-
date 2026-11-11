#!/usr/bin/env bash
# Runs inside GitHub Actions: evaluate at :07 every hour for ~5.5h, then the
# workflow re-dispatches itself. Independent of GitHub's (unreliable) cron.
set -u
BR="${GITHUB_REF_NAME:-claude/ecstatic-hawking-mdyk0r}"
git config user.name "paper-sim-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
END=$(( $(date +%s) + 330 * 60 ))

tick() {
  git pull -q --rebase origin "$BR" || git rebase --abort || true
  python3 engine.py || echo "engine failed"
  if [ "$(TZ=Asia/Singapore date +%u%H)" = "612" ] && [ ! -f "weekly/$(TZ=Asia/Singapore date +%F).md" ]; then
    python3 report.py || echo "report failed"
  fi
  git add -A accounts docs weekly
  if ! git diff --cached --quiet; then
    git commit -qm "sim: $(TZ=Asia/Singapore date '+%Y-%m-%d %H:%M') SGT"
    for i in 1 2 3 4; do
      git pull -q --rebase -X theirs origin "$BR" && git push -q origin "HEAD:$BR" && break
      sleep $(( i * 5 ))
    done
  fi
}

tick
while :; do
  now=$(date +%s)
  next=$(( now / 3600 * 3600 + 420 ))
  [ "$next" -le "$now" ] && next=$(( next + 3600 ))
  [ "$next" -gt "$END" ] && break
  sleep $(( next - now ))
  tick
done
