from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

DEFAULT_REGIONS = ("iad", "fra", "sin")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Sarvam streaming probe from Fly regions.")
    parser.add_argument("--app", default="orchet-voice")
    parser.add_argument("--regions", default=",".join(DEFAULT_REGIONS))
    parser.add_argument("--fly", default=os.environ.get("FLY_BIN", "~/.fly/bin/fly"))
    args = parser.parse_args()

    fly = os.path.expanduser(args.fly)
    probe_source = Path(__file__).with_name("sarvam_streaming_probe.py").read_text()
    summaries = []
    for region in [item.strip() for item in args.regions.split(",") if item.strip()]:
        command = [
            fly,
            "ssh",
            "console",
            "-a",
            args.app,
            "--region",
            region,
            "-C",
            f"/app/.venv/bin/python - --all --region {region}",
        ]
        result = subprocess.run(
            command,
            input=probe_source,
            text=True,
            capture_output=True,
            check=False,
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        summaries.append(_summary_from_output(result.stdout))

    print("MULTI_REGION_SUMMARY_JSON " + json.dumps(summaries, ensure_ascii=False))


def _summary_from_output(output: str) -> dict[str, object]:
    for line in output.splitlines():
        if line.startswith("SUMMARY_JSON "):
            return json.loads(line.removeprefix("SUMMARY_JSON "))
    raise RuntimeError("probe output did not include SUMMARY_JSON")


if __name__ == "__main__":
    main()
