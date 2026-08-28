"""
run_forever.py — The self-improving loop.
Runs: bot -> upgrade -> sleep -> repeat.
This is the brain that never stops learning.
"""
import sys
import time
import subprocess
import os
from datetime import datetime

WORKDIR = os.path.dirname(os.path.abspath(__file__))
LOOP_INTERVAL = 3600 * 4
UPGRADE_INTERVAL = 3600 * 8


def run_bot():
    print(f"\n{'#'*50}")
    print(f"BOT CYCLE — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'#'*50}")
    result = subprocess.run(
        [sys.executable, "bot.py", "quiet"],
        cwd=WORKDIR, capture_output=True, text=True, timeout=900
    )
    output = result.stdout + result.stderr
    lines = [l for l in output.split("\n") if l.strip()]
    for line in lines[-20:]:
        print(f"  {line}")
    if result.returncode != 0:
        print(f"  BOT EXITED WITH CODE {result.returncode}")
    return result.returncode


def run_upgrade():
    print(f"\n{'*'*50}")
    print(f"UPGRADE CYCLE — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'*'*50}")
    result = subprocess.run(
        [sys.executable, "upgrade.py"],
        cwd=WORKDIR, capture_output=True, text=True, timeout=300
    )
    output = result.stdout + result.stderr
    for line in output.split("\n"):
        if line.strip():
            print(f"  {line}")
    return result.returncode


def main():
    print("=" * 50)
    print("SELF-IMPROVING BOT — LOOP STARTED")
    print(f"Bot runs every {LOOP_INTERVAL // 3600}h, upgrades every {UPGRADE_INTERVAL // 3600}h")
    print("Press Ctrl+C to stop")
    print("=" * 50)

    cycle = 0
    last_upgrade = 0

    while True:
        cycle += 1
        now = time.time()
        print(f"\n>>> CYCLE {cycle} at {datetime.now():%H:%M}")

        try:
            run_bot()
        except Exception as e:
            print(f"  BOT CRASHED: {e}")

        if now - last_upgrade > UPGRADE_INTERVAL:
            try:
                run_upgrade()
                last_upgrade = now
            except Exception as e:
                print(f"  UPGRADE CRASHED: {e}")

        next_run = datetime.fromtimestamp(now + LOOP_INTERVAL)
        print(f"\n  Next cycle at {next_run:%H:%M} (sleeping {LOOP_INTERVAL // 3600}h)")
        try:
            time.sleep(LOOP_INTERVAL)
        except KeyboardInterrupt:
            print("\n  Loop stopped by user")
            break


if __name__ == "__main__":
    main()
