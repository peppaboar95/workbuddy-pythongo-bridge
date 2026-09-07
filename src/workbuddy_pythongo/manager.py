import argparse
import json
import sys

from .bootstrap import initialize
from .console import clear_halt, set_mode, sign_profile
from .doctor import run_doctor
from .errors import BridgeError
from .modes import RUN_MODES
from .margin_reference import refresh_margin_reference
from .worker import build_runtime, main as worker_main


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="WorkBuddy-PythonGO local manager")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="generate a fail-closed deployment")
    init.add_argument("--root", default=".")
    init.add_argument("--force", action="store_true")
    start = sub.add_parser("start", help="start the loopback Worker")
    start.add_argument("--mode", choices=RUN_MODES, default="OBSERVE_ONLY")
    start.add_argument("--confirm")
    sub.add_parser("status")
    sub.add_parser("doctor")
    mode = sub.add_parser("set-mode")
    mode.add_argument("mode", choices=RUN_MODES)
    mode.add_argument("--confirm")
    clear = sub.add_parser("clear-halt")
    clear.add_argument("--confirm", required=True)
    sign = sub.add_parser("sign-profile")
    sign.add_argument("account_alias")
    sign.add_argument("--confirm", required=True)
    margin = sub.add_parser("refresh-margin-reference", help="refresh the local 9qihuo margin and commission CSV")
    margin.add_argument("--source-csv", help="import an existing 9qihuo-format CSV instead of accessing the network")
    margin.add_argument("--if-due", action="store_true", help="skip network refresh when today's signed file is still fresh")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.root, args.force)
        elif args.command == "doctor":
            result = run_doctor(args.config)
        elif args.command == "status":
            _, _, core = build_runtime(args.config)
            result = core.pythongo_health()
        elif args.command == "set-mode":
            result = set_mode(args.config, args.mode, args.confirm)
        elif args.command == "clear-halt":
            result = clear_halt(args.config, args.confirm)
        elif args.command == "sign-profile":
            result = sign_profile(args.config, args.account_alias, args.confirm)
        elif args.command == "refresh-margin-reference":
            result = refresh_margin_reference(args.config, args.source_csv, args.if_due)
        elif args.command == "start":
            set_mode(args.config, args.mode, args.confirm)
            worker_args = ["--config", args.config] if args.config else []
            if args.mode != "OBSERVE_ONLY":
                worker_args.extend(["--confirm-mode", args.mode])
            return worker_main(worker_args)
        else:
            raise BridgeError("INVALID_REQUEST", "unknown manager command")
        _print(result)
        return 0 if result.get("ok", True) else 2
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
