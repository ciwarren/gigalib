"""
Install/manage the GigaLib Windows startup tasks using Task Scheduler.

Usage:
    uv run python scripts/install_service.py install [--target app|social|all]
    uv run python scripts/install_service.py uninstall [--target app|social|all]
    uv run python scripts/install_service.py status [--target app|social|all]
"""

import argparse
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_EXE = os.path.join(APP_DIR, ".venv", "Scripts", "python.exe")
SERVE_SCRIPT = os.path.join(APP_DIR, "scripts", "serve.py")
SERVE_SOCIAL_SCRIPT = os.path.join(APP_DIR, "scripts", "serve_social.py")

TASKS = {
    "app": {
        "name": "GigaLib",
        "script": SERVE_SCRIPT,
        "host": "127.0.0.1",
        "port": 5000,
    },
    "social": {
        "name": "GigaLib Social",
        "script": SERVE_SOCIAL_SCRIPT,
        "host": "127.0.0.1",
        "port": 8081,
    },
}


def build_command(script_path, host, port):
    return f'"{PYTHON_EXE}" "{script_path}" --host {host} --port {port}'


def selected_targets(target):
    if target == "all":
        return ["app", "social"]
    return [target]


def create_task(task_name, command):
    subprocess.run(["schtasks", "/delete", "/tn", task_name, "/f"], capture_output=True)
    result = subprocess.run(
        [
            "schtasks",
            "/create",
            "/tn",
            task_name,
            "/tr",
            command,
            "/sc",
            "onlogon",
            "/rl",
            "limited",
            "/f",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"Failed to create {task_name}: {result.stderr.strip() or result.stdout.strip()}")


def remove_task(task_name):
    subprocess.run(["schtasks", "/delete", "/tn", task_name, "/f"], capture_output=True)


def show_status(task_name):
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "list"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"Task '{task_name}' not found.")


def install(target):
    for key in selected_targets(target):
        task = TASKS[key]
        print(f"Installing {task['name']}...")
        create_task(task["name"], build_command(task["script"], task["host"], task["port"]))
        print(f"  OK: {task['name']} starts on login")
        print(f"  Access: http://{task['host']}:{task['port']}")


def uninstall(target):
    for key in selected_targets(target):
        task = TASKS[key]
        remove_task(task["name"])
        print(f"Removed {task['name']}")


def status(target):
    for key in selected_targets(target):
        show_status(TASKS[key]["name"])


def main():
    parser = argparse.ArgumentParser(description="Manage GigaLib startup tasks")
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument(
        "--target",
        choices=["app", "social", "all"],
        default="all",
        help="Which task(s) to manage",
    )
    args = parser.parse_args()

    if not os.path.exists(PYTHON_EXE):
        raise SystemExit(
            f"Missing virtual environment Python: {PYTHON_EXE}. Run 'uv sync' first."
        )

    if args.action == "install":
        install(args.target)
    elif args.action == "uninstall":
        uninstall(args.target)
    else:
        status(args.target)


if __name__ == "__main__":
    main()
