"""Run with: python -m minicode.web --port 8765."""

import argparse


def main():
    parser = argparse.ArgumentParser(prog="minicode.web", description="minicode local Web interface")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file")
    parser.add_argument("--workdir", help="Default workspace shown when creating a session")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn
    from ..config import load_settings
    from .app import create_app

    settings = load_settings(args.env_file, workdir=args.workdir)
    app = create_app(settings, port=args.port)
    print(f"minicode Web: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
