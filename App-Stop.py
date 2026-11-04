import os
import subprocess
import time


def _pkill_escalate(pattern: str, wait_s: float = 2.0) -> None:
    """SIGTERM first (graceful), then SIGKILL if the process survives.

    pygame/SDL installs a SIGTERM handler that swallows the FIRST SIGTERM,
    so a single pkill leaves the app running; a second signal (or SIGKILL)
    terminates it (verified on the robot, M10)."""
    subprocess.run(["pkill", "-f", pattern], check=False)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        if result.returncode != 0:      # no match — all gone
            return
        time.sleep(0.25)
    subprocess.run(["pkill", "-9", "-f", pattern], check=False)


def stop_tars_ai():
    # Ensure DISPLAY is set for GUI applications
    display = os.getenv("DISPLAY", ":0")

    subprocess.run(["killall", "xterm"], check=False)
    # Kill the autostart launcher (lp.sh → lxterminal → App-Start.py).
    # Self-protection check: this script runs as `python3 App-Stop.py`, so its
    # own cmdline contains "App-Stop.py". The pattern "App-Start.py" differs
    # from it at char 7 ('a' vs 'o'), and pkill/pgrep -f patterns are matched
    # literally — no self-match, App-Stop cannot kill itself.
    _pkill_escalate("App-Start.py")
    # Kill the app process: App-Start.py:482 spawns it as
    # `cd src && source .venv/bin/activate && python app.py show_ui=true …`,
    # so the cmdline contains "python app.py" — the same pattern App-Start.py
    # itself uses to kill it (App-Start.py:474). "python app.py" also cannot
    # match this script's own cmdline ("python3 App-Stop.py").
    _pkill_escalate("python app.py")


if __name__ == "__main__":
    stop_tars_ai()
