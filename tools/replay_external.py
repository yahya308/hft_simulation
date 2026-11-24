"""Reproduce external OUCH commands and compare the resulting wire replies."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.external_replay import ReplayDifference, replay_external_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--tape", action="append", required=True, metavar="UDP_PORT=PATH")
    args = parser.parse_args()
    try:
        tapes = {}
        for entry in args.tape:
            port, path = entry.split("=", 1)
            port = int(port)
            if not 1 <= port <= 65535 or port in tapes:
                raise ValueError("UDP portları geçerli ve birbirinden farklı olmalı.")
            tapes[port] = Path(path)
        print(json.dumps(replay_external_run(args.run, tapes)))
    except ReplayDifference as error:
        parser.exit(1, f"{error}\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
