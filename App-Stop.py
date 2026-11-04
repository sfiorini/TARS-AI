import os
import subprocess

def stop_tars_ai():
    # Ensure DISPLAY is set for GUI applications
    display = os.getenv("DISPLAY", ":0")

    subprocess.run(["killall", "xterm"], check=False)
    # Kill the autostart launcher (lp.sh → lxterminal → App-Start.py).
    # "App-Start.py" cannot match this script's own cmdline ("App-Stop.py") —
    # pkill -f patterns are case-sensitive.
    subprocess.run(["pkill", "-f", "App-Start.py"], check=False)
    # Kill the app process: App-Start.py:482 spawns it as
    # `cd src && source .venv/bin/activate && python app.py show_ui=true …`,
    # so the cmdline contains "python app.py" — the same pattern App-Start.py
    # itself uses to kill it (App-Start.py:474).
    subprocess.run(["pkill", "-f", "python app.py"], check=False)

if __name__ == "__main__":
    stop_tars_ai()