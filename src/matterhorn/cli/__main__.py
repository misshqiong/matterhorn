"""Support ``python -m matterhorn.cli`` as an installed CLI entry point."""

from matterhorn.cli.app import app

if __name__ == "__main__":
    app()
