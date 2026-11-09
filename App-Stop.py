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


def _sigterm_first(pattern: str, wait_s: float = 10.0) -> bool:
    """Send SIGTERM to the matched pids and wait up to wait_s for exit.

    The app now installs a SIGTERM handler (S-M6-08) that sets its shutdown
    event, so the FIRST SIGTERM triggers a graceful shutdown (MCP servers
    stopped, memory flushed).  Returns True when everything exited in time.
    """
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    pids = [p for p in result.stdout.split() if p]
    for pid in pids:
        subprocess.run(["kill", "-TERM", pid], check=False)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        if result.returncode != 0:      # no match — all gone
            return True
        time.sleep(0.25)
    return False


def stop_tars_ai():
    # Ensure DISPLAY is set for GUI applications
    display = os.getenv("DISPLAY", ":0")

    subprocess.run(["killall", "xterm"], check=False)
    # Kill the autostart launcher (lp.sh → lxterminal → App-Start.py).
    # Self-protection check: this script runs as `python3 App-Stop.py`, so its
    # own cmdline contains "App-Stop.py". The pattern "App-Start.py" differs
    # from it at char 7 ('a' vs 'o'), and pkill/pgrep -f patterns are matched
    # literally — no self-match, App-Stop cannot kill itself.
    # Graceful first: SIGTERM the app and give it up to 10s to run its
    # shutdown path (MCP servers, memory flush). The pygame double-SIGTERM
    # note below still applies as the escalation safety net.
    _sigterm_first("python app.py", wait_s=10.0)
    _pkill_escalate("App-Start.py")
    # Kill the app process: App-Start.py:482 spawns it as
    # `cd src && source .venv/bin/activate && python app.py show_ui=true …`,
    # so the cmdline contains "python app.py" — the same pattern App-Start.py
    # itself uses to kill it (App-Start.py:474). "python app.py" also cannot
    # match this script's own cmdline ("python3 App-Stop.py").
    _pkill_escalate("python app.py")
    # Final sweep: a HARD-killed app (SIGKILL before its finally could run)
    # leaves its MCP stdio children alive — they run in their own session
    # and match neither pattern above. Kill them PRECISELY (audit B6): the
    # shipped example server by name, plus every TARS-spawned stdio child
    # by pid from the manager's pidfile (a bare `mcp_` pattern killed
    # unrelated processes and missed custom servers).
    _pkill_escalate("mcp_example_server")
    # the pidfile lives next to the MCP registry — derived from THIS
    # script's own location so a non-standard checkout works (audit
    # B6-residual; the manager writes it beside src/mcp_servers.json)
    pidfile = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "mcp_pids.txt")
    if os.path.exists(pidfile):
        try:
            with open(pidfile) as fh:
                for line in fh:
                    pid = line.strip()
                    if pid.isdigit():
                        subprocess.run(["kill", "-KILL", pid], check=False)
        except OSError:
            pass
        try:
            os.remove(pidfile)
        except OSError:
            pass


if __name__ == "__main__":
    stop_tars_ai()
