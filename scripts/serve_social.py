"""
Standalone production entry point for the GigaLib social API.

Linux example:
    SOCIAL_DATABASE_URL=sqlite:////opt/gigalib-social/gigalib-social.db \
    uv run python scripts/serve_social.py --host 0.0.0.0 --port 8081
"""

import argparse
import os

from gigalib_social_service import create_app

app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the GigaLib social API")
    parser.add_argument(
        "--host",
        default=os.getenv("SOCIAL_HOST", "127.0.0.1"),
        help="Host to bind to (default: SOCIAL_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("SOCIAL_PORT", "8081")),
        help="Port to listen on (default: SOCIAL_PORT or 8081)",
    )
    args = parser.parse_args()

    try:
        from waitress import serve

        print(f"Starting GigaLib social API on http://{args.host}:{args.port}")
        serve(app, host=args.host, port=args.port)
    except ImportError:
        print("waitress not installed, using Flask dev server")
        app.run(host=args.host, port=args.port, debug=False)
