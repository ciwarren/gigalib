"""
Install/manage GigaLib as a Windows service using Windows Task Scheduler.
No admin rights required (runs at user login).

Usage (run as admin for service, or normal user for Task Scheduler):
    uv run python install_service.py install [--host 0.0.0.0] [--port 8080]
    uv run python install_service.py uninstall
    uv run python install_service.py status
"""

import subprocess
import sys
import os

TASK_NAME = "GigaLib"
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_EXE = os.path.join(APP_DIR, ".venv", "Scripts", "python.exe")
SERVE_SCRIPT = os.path.join(APP_DIR, "serve.py")


def install():
    """Create a Windows Task Scheduler task that starts GigaLib on login."""
    # Parse optional --host and --port from args
    host = "127.0.0.1"
    port = "5000"
    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg == "--host" and i + 1 < len(args):
            host = args[i + 1]
        elif arg == "--port" and i + 1 < len(args):
            port = args[i + 1]

    serve_cmd = f'"{PYTHON_EXE}" "{SERVE_SCRIPT}" --host {host} --port {port}'
    # Delete existing task if any
    subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True,
    )

    # Create task that runs at logon
    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", TASK_NAME,
            "/tr", serve_cmd,
            "/sc", "onlogon",
            "/rl", "limited",
            "/f",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' created successfully.")
        print(f"GigaLib will start automatically on login.")
        print(f"Access at: http://{host}:{port}")
        print(f"\nTo start now: schtasks /run /tn {TASK_NAME}")
    else:
        print(f"Failed to create task: {result.stderr}")
        print("Try running as Administrator.")
        sys.exit(1)


def uninstall():
    """Remove the scheduled task."""
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' removed.")
    else:
        print(f"Failed: {result.stderr}")


def status():
    """Check if the task exists and its state."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "list"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"Task '{TASK_NAME}' not found. Run 'install' first.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python install_service.py [install|uninstall|status]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "status":
        status()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
