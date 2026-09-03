"""
night_runner.py — Windows local night partner for the AI trading bot.

The CLOUD (GitHub Actions) runs the live trader every 4h but has NO local
Ollama, so it can't use the local AI. This script runs on YOUR PC at night,
when it's the only active trader, and:

  1. Runs the local Ollama AI news pass (record_ai_sentiments) — the cloud
     cannot reach a localhost Ollama, so this is the night runner's job.
  2. Runs the EXACT same trading engine as the cloud (cloud_bot.run_cloud()).
  3. Commits and pushes DB changes back to the repo so the cloud's next run
     benefits from the locally-computed AI verdicts.

SCHEDULING (no overlap with cloud):
  Cloud slots run at :30 of 0,4,8,12,16,20 UTC (i.e. 00:30,04:30,.. UTC).
  This runner is scheduled by Windows Task Scheduler at SPECIFIC local times
  (Israel ~ UTC+3 night: 23:00, 02:00, 05:00) that fall in the gaps between
  cloud slots, so the two NEVER trade at the same instant. Because they sync
  through git (not a live connection), time-slicing is the safe design.

Run:  python night_runner.py
Fails closed (no trading) if it can't reach local Ollama or the market data.
"""
import sys
import os
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain
import news
import idle_check


def ollama_up():
    """Return True if local Ollama is reachable."""
    import socket
    try:
        s = socket.create_connection(("localhost", 11434), timeout=2)
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def _token_url():
    """Build an authenticated push URL using the GitHub CLI token (if available),
    else fall back to the plain origin URL."""
    url = "https://github.com/kovtunronil-bot/ai-trading-lab.git"
    gh = "C:/Program Files/GitHub CLI/gh.exe"
    if os.path.exists(gh):
        try:
            tok = subprocess.run([gh, "auth", "token"], capture_output=True,
                                 text=True, timeout=30).stdout.strip()
            if tok and "@" not in tok and len(tok) > 10:
                return "https://x-access-token:" + tok + "@github.com/kovtunronil-bot/ai-trading-lab.git"
        except Exception:
            pass
    return url


def git_push():
    """Commit any DB/config changes and push to origin master (cloud reads them).

    Handles the cloud-vs-local race: before pushing we fetch + rebase on top of
    the remote (the cloud pushes every 30 min, so a bare push is usually
    rejected). Conflicting data files (lab.db, config_*.json) are resolved in
    favor of the cloud (--theirs); the local AI verdicts are the only thing the
    night runner owns and those live in lab.db which rolls forward either way.
    Auth is done with the GitHub CLI token (no interactive prompt)."""
    import glob
    try:
        # Expand globs in Python (Windows subprocess does not shell-expand *)
        files = ["lab.db", "state.json", "proposals.json", "positions_snapshot.json"]
        files += glob.glob("config_*.json")
        files = [f for f in files if os.path.exists(f)]
        if not files:
            print("  git: nothing to add")
            return True
        subprocess.run(["git", "add"] + files, check=True)
        # exit 0 if nothing to commit
        r = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if r.returncode == 0:
            print("  git: no changes to commit")
            return True
        ts = brain.datetime.now().strftime("%Y-%m-%dT%H:%M")
        subprocess.run(
            ["git", "commit", "-m", f"night runner update {ts} (local AI)"],
            check=True)

        auth_url = _token_url()

        # Fetch the remote without touching the working tree.
        subprocess.run(["git", "fetch", auth_url, "master"], check=True)

        # Rebase our commit(s) on top of the remote; resolve data-file conflicts
        # in favor of the cloud (--theirs) so we never clobber cloud state.
        rebase = subprocess.run(["git", "rebase", "FETCH_HEAD"],
                                capture_output=True, text=True)
        if rebase.returncode != 0:
            print("  git: rebase conflicted, resolving data files in favor of cloud")
            conflicted = ["lab.db"] + [f for f in files if os.path.exists(f)]
            for f in conflicted:
                subprocess.run(["git", "checkout", "--theirs", f],
                               capture_output=True)
                subprocess.run(["git", "add", f], capture_output=True)
            subprocess.run(["git", "-c", "core.editor=true", "rebase", "--continue"],
                           check=True)

        push = subprocess.run(["git", "push", auth_url, "master:master"],
                              capture_output=True, text=True)
        if push.returncode != 0:
            raise RuntimeError(push.stderr.strip()[:400])
        print("  git: pushed")
        return True
    except Exception as e:
        # Roll back to the pre-push state so we're not left mid-rebase.
        try:
            if os.path.exists(os.path.join(".git", "rebase-merge")):
                subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        except Exception:
            pass
        print(f"  git push failed: {e}")
        return False


def run_night():
    print("=" * 60)
    print("NIGHT RUNNER (local) — %s" % brain.datetime.now().isoformat(timespec="seconds"))
    print("=" * 60)

    # 0) Idle gate: if the PC is busy (fullscreen game / GPU or CPU hogged),
    #    skip the heavy local AI pass so it never interferes with the user.
    #    The normal trading engine (lightweight, cloud-parity) still runs.
    #    We retry a few times (brief waits) in case the user closes the game.
    MAX_ATTEMPTS = 3
    WAIT_SECONDS = 40
    busy_reasons = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        busy_reasons = idle_check.is_machine_busy()
        if not busy_reasons:
            break
        print(f"\n[IDLE-CHECK attempt {attempt}/{MAX_ATTEMPTS}] busy: " + "; ".join(busy_reasons))
        if attempt < MAX_ATTEMPTS:
            print(f"   waiting {WAIT_SECONDS}s to re-check ...")
            import time as _t
            _t.sleep(WAIT_SECONDS)

    if busy_reasons:
        print("\n[IDLE-CHECK] machine still busy -> SKIPPING local AI pass this run.")
        print("   (AI verdicts skipped; trading engine still runs)")
    else:
        print("\n[IDLE-CHECK] machine free -> will run local AI pass.")

    # 1) Local AI pass. If Ollama is down, still trade the normal engine but
    #    note that AI verdicts were skipped (don't fail the whole run).
    if ollama_up() and not busy_reasons:
        print("\n[LOCAL OLLAMA AI — reading headlines for every market]")
        n = news.record_ai_sentiments(verbose=True)
        print(f"  recorded {n} AI verdicts to ai_news")
    else:
        print("\n[LOCAL OLLAMA] offline — skipping AI news pass (trading continues)")

    # 2) Run the exact same trading engine the cloud uses.
    print("\n[TRADING ENGINE (same as cloud)]")
    import cloud_bot
    cloud_bot.run_cloud()

    # 3) Sync results (incl. AI verdicts) back to the repo for the cloud.
    print("\n[SYNC TO REPO]")
    git_push()
    print("NIGHT RUNNER done.")


if __name__ == "__main__":
    run_night()
