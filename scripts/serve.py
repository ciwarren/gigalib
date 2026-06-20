"""
Production server entry point for running GigaLib as a service.
Uses waitress (production WSGI server) instead of Flask's dev server.

Install as Windows service (via Task Scheduler):
    uv run python install_service.py install

Or run directly:
    uv run python serve.py [--host 0.0.0.0] [--port 8080]
"""

import argparse

from gigalib import create_app

app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run GigaLib server")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=5000, help="Port to listen on (default: 5000)"
    )
    args = parser.parse_args()

    try:
        from waitress import serve

        print(f"Starting GigaLib on http://{args.host}:{args.port}")
        serve(app, host=args.host, port=args.port)
    except ImportError:
        print("waitress not installed, using Flask dev server")
        app.run(host=args.host, port=args.port, debug=False)
