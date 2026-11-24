"""Verify sealed replays and inspect or export a durable interrupted prefix."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.run_audit import inspect_run, recover_run, seal_interrupted, verify_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("repeat", type=Path, nargs="?")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true", help="Koşu durumunu kaynak dosyaları değiştirmeden incele.")
    mode.add_argument("--seal-interrupted", action="store_true", help="Yazarı kalmamış koşuyu kesintili olarak kaydet.")
    mode.add_argument("--recover-to", type=Path, metavar="DIZIN", help="Doğrulanan öneki yeni bir dizine çıkar.")
    args = parser.parse_args()
    if args.repeat is not None and (args.inspect or args.seal_interrupted or args.recover_to is not None):
        parser.error("Kesinti işlemleri yalnız tek koşu dizini kabul eder.")
    try:
        if args.inspect:
            print(json.dumps(inspect_run(args.run), ensure_ascii=False))
            return
        if args.seal_interrupted:
            result = seal_interrupted(args.run)
            print(json.dumps({"id": result["id"], "status": result["status"], "checkpoint": result["checkpoint"],
                              "uncheckpointed_bytes": result["uncheckpointed_bytes"]}, ensure_ascii=False))
            return
        if args.recover_to is not None:
            result = recover_run(args.run, args.recover_to)
            print(json.dumps({"id": result["id"], "status": result["status"], "checkpoint": result["checkpoint"],
                              "destination": str(args.recover_to)}, ensure_ascii=False))
            return
        first = verify_run(args.run)
        if args.repeat is not None:
            repeat = verify_run(args.repeat)
            if first["scenario_sha256"] != repeat["scenario_sha256"]:
                raise ValueError("Koşular aynı girdiler ve ayarlarla başlatılmamış.")
            if (first["status"], first["events_sha256"], first["results_sha256"]) != (
                repeat["status"], repeat["events_sha256"], repeat["results_sha256"]
            ):
                raise ValueError("Emir dizisi veya portföy sonucu farklı.")
        print(json.dumps({"id": first["id"], "status": first["status"], "records": first["records"],
                          "events_sha256": first["events_sha256"],
                          "results_sha256": first["results_sha256"],
                          "repeat_verified": args.repeat is not None}))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
