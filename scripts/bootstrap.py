#!/usr/bin/env python3
"""Initialize a private workspace without replacing the user's settings."""
import argparse
import json
import os
import tempfile
from pathlib import Path


def main():
    source = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=Path.home() / "muse-job-workflow-data"
    )
    workspace = parser.parse_args().workspace.expanduser().resolve()
    if workspace.is_relative_to(source):
        parser.error("Choose a private data directory outside the source checkout.")

    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if workspace.stat().st_mode & 0o077:
        parser.error("Choose a data directory restricted to your user (mode 0700).")

    runs = workspace / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    config = workspace / "config.json"
    if not config.exists():
        template = (source / "config.example.json").read_bytes()
        with tempfile.NamedTemporaryFile(dir=workspace) as prepared:
            prepared.write(template)
            prepared.flush()
            try:
                os.link(prepared.name, config)
            except FileExistsError:
                pass  # Another bootstrap already installed the configuration.

    print(json.dumps({"workspace": str(workspace), "config": str(config), "runs": str(runs)}))


if __name__ == "__main__":
    main()
