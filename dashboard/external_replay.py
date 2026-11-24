"""Re-execute a sealed OUCH session against the exact recorded historical inputs."""

import json
from pathlib import Path

from .multi_simulation import MultiSimulation
from .ouch import Reply, decode_command, encode_reply
from .replay_stream import ReplayStream
from .run_audit import canonical_json, file_sha256, source_sha256, verify_run


class ReplayDifference(RuntimeError):
    pass


def run_results(simulation: MultiSimulation) -> dict:
    keys = ("book_id", "position", "cash", "realized_pnl", "unrealized_pnl", "total_pnl", "mark_price",
            "fills", "ouch_sent", "ouch_received")
    return {symbol: {key: snapshot[key] for key in keys}
            for symbol, snapshot in simulation.snapshot_all().items()}


def submit_command(simulation: MultiSimulation, command, sequence: int) -> None:
    try:
        simulation.external_order(command, sequence=sequence)
    except ValueError:
        simulation.reply_sink(encode_reply(Reply("J", simulation.exchange_ns, command.token, reason=-4)))


def _reply_records(stream):
    sequence = 0
    for line in stream:
        entry = json.loads(line)
        if entry["kind"] == "external_reply":
            sequence += 1
            if entry["data"]["sequence"] != sequence:
                raise ValueError("OUCH yanıt dizisinin sıra numarası geçersiz.")
            yield bytes.fromhex(entry["data"]["frame_hex"])


def _actions(stream):
    for line in stream:
        entry = json.loads(line)
        kind, fields = entry["kind"], entry["data"]
        if kind == "external_input":
            yield fields["after_sequence"], int(fields["time_ns"]), kind, fields
        elif kind == "strategy_control":
            yield fields["sequence"], int(fields["exchange_ns"]), kind, fields
        elif kind == "risk_stop" and fields["reason"] == "manual":
            if not fields.get("at_boundary"):
                raise ValueError("Emir işlenirken uygulanan manuel durdurma birebir yeniden yürütülemiyor. "
                                 "Bu koşuyu bütünlük doğrulamasıyla incele.")
            yield fields["sequence"], int(fields["time_ns"]), kind, fields


def replay_external_run(directory: Path, tapes: dict[int, Path]) -> dict:
    manifest = verify_run(directory)
    scenario = manifest["scenario"]
    if (manifest["status"] != "finished" or scenario.get("external_ledger") != 2
            or scenario["strategy"]["id"] != "external_ouch"):
        raise ValueError("Tamamlanmış ve harici OUCH yanıt dizisi içeren bir koşu seç.")
    if scenario["engine_sha256"] != source_sha256(Path(__file__).parent):
        raise ValueError("Koşuda kullanılan motor kaynak özeti bu sürümden farklı.")
    routes = scenario["sources"]["routes"]
    if set(routes.values()) != set(tapes):
        raise ValueError("Manifestteki her UDP kanalı için bir olay dosyası ver.")
    for port, tape in tapes.items():
        if file_sha256(tape) != scenario["coverage"][str(port)]["tape_sha256"]:
            raise ValueError(f"UDP {port} olay dosyası SHA-256 özeti uyuşmuyor.")
    settings = {**scenario["settings"], "enabled": scenario["enabled"]}
    settings["max_position"] = settings.pop("position_limit")
    sim = MultiSimulation(dict.fromkeys(routes, settings), sources=routes)
    ledger = directory / "events.ndjson"
    replies = commands = 0
    with ledger.open("rb") as wire_stream, ledger.open("rb") as action_stream:
        expected_replies = _reply_records(wire_stream)
        actions = _actions(action_stream)
        action = next(actions, None)

        def compare(frame):
            nonlocal replies
            replies += 1
            if frame != next(expected_replies, None):
                raise ReplayDifference(f"{replies} numaralı OUCH yanıtı orijinal diziyle aynı değil.")

        def apply_actions():
            nonlocal action, commands
            while action is not None and action[0] == sim.sequence:
                _, time_ns, kind, fields = action
                try:
                    sim.advance_recorded(time_ns, fields["dispatch_cursor"])
                except ValueError as error:
                    raise ReplayDifference(str(error)) from error
                if kind == "external_input":
                    commands += 1
                    if fields["intake"] != commands:
                        raise ValueError("OUCH komut dizisinin sıra numarası geçersiz.")
                    action = next(actions, None)
                    if (action is not None and action[2] == "risk_stop"
                            and action[3].get("before_intake") == commands):
                        stop_sequence, stop_time, _, stop_fields = action
                        if (stop_sequence != sim.sequence or stop_time != time_ns
                                or stop_fields["dispatch_cursor"] != sim.scheduled_dispatches):
                            raise ReplayDifference("Manuel durdurma kaydı yanlış OUCH giriş sınırına bağlı.")
                        sim.emergency_stop()
                        action = next(actions, None)
                    submit_command(sim, decode_command(bytes.fromhex(fields["frame_hex"])), commands)
                    continue
                elif kind == "strategy_control":
                    sim.set_enabled(fields["enabled"])
                else:
                    sim.emergency_stop()
                action = next(actions, None)
            if action is not None and action[0] < sim.sequence:
                raise ReplayDifference("Kayıtlı kontrolün ITCH sıra sınırı bulunamadı.")

        sim.set_reply_sink(compare)
        stream = ReplayStream(tapes)
        try:
            apply_actions()
            while (record := stream.read()) is not None:
                sim.apply(record.event, source=record.port, source_sequence=record.source_sequence)
                apply_actions()
            if action is not None:
                raise ReplayDifference("Olay dosyaları son kayıtlı kontrolü içermiyor.")
            sim.finish()
        finally:
            stream.close()
            sim.close_strategies()
        if next(expected_replies, None) is not None:
            raise ReplayDifference(f"{replies} yanıttan sonra orijinal dizide ek yanıtlar var.")
    if canonical_json(run_results(sim)) != canonical_json(manifest["results"]):
        raise ReplayDifference("Son portföy veya emir sayaçları orijinal koşudan farklı.")
    return {"run_id": manifest["id"], "commands": commands, "replies": replies,
            "wire_reproduced": True, "results_reproduced": True}
