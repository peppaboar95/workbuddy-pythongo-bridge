import argparse
import ctypes
import datetime as dt
import json
import locale
import os
import shutil
import socket
import sys

from .bootstrap import initialize
from .config import load_config
from .console import bind_investor, get_non_observe_mode_blockers, set_mode
from .doctor import run_doctor
from .errors import BridgeError
from .mcp_server import WorkerClient
from .util import atomic_write_bytes
from .worker import main as worker_main


START_MODE_SELECTIONS = {
    "1": "OBSERVE_ONLY",
    "2": "SIM_SIGNAL",
    "3": "MANUAL_LIVE",
    "4": "LIMITED_AUTO",
}

START_MODE_DESCRIPTIONS = {
    "OBSERVE_ONLY": "查询、预览和空跑闭环，不执行普通报单（默认）",
    "SIM_SIGNAL": "仅用于已完成P0的无限易模拟柜台",
    "MANUAL_LIVE": "人工交易；还需要逐笔或限时授权",
    "LIMITED_AUTO": "P1有限自动；还需要结构化策略许可",
}


def _default_mcp_path():
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")


def _desktop_dir():
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer)
            if result == 0 and buffer.value:
                return buffer.value
        except (AttributeError, OSError):
            pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _cmd_value(value):
    return str(value).replace("%", "%%").replace('"', '""')


def _write_changed(path, data):
    path = os.path.abspath(path)
    backup = None
    if os.path.exists(path):
        with open(path, "rb") as stream:
            current = stream.read()
        if current == data:
            return {"path": path, "changed": False, "backup": None}
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = "%s.bak.%s" % (path, timestamp)
        shutil.copy2(path, backup)
    atomic_write_bytes(path, data)
    return {"path": path, "changed": True, "backup": backup}


def _retire_obsolete_shortcut(path):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return None
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = "%s.bak.%s" % (path, timestamp)
    os.replace(path, backup)
    return {"path": path, "backup": backup}


def create_shortcuts(config_path, target_dir=None, python_executable=None):
    config = load_config(config_path)
    config_path = os.path.abspath(config.path)
    target_dir = os.path.abspath(target_dir or _desktop_dir())
    python_executable = os.path.abspath(python_executable or sys.executable)
    os.makedirs(target_dir, exist_ok=True)
    command = '"%s" -m workbuddy_pythongo.desktop --config "%s"' % (
        _cmd_value(python_executable),
        _cmd_value(config_path),
    )
    manager_command = '"%s" -m workbuddy_pythongo.manager --config "%s"' % (
        _cmd_value(python_executable),
        _cmd_value(config_path),
    )
    prefix = "@echo off\r\nsetlocal\r\n"
    scripts = {
        "启动PythonGO桥接.cmd": (
            prefix
            + "title WorkBuddy PythonGO Bridge\r\n"
            + "echo Select the Worker mode first, then the bridge will start.\r\n"
            + "echo Press Enter at the mode prompt to use the safe OBSERVE_ONLY default.\r\n"
            + "echo Keep this window open while the Worker is running.\r\n"
            + "echo.\r\n"
            + manager_command
            + " refresh-margin-reference --if-due >nul 2>&1\r\n"
            + "if errorlevel 1 echo [WARNING] Margin refresh failed. Opening trades will remain fail-closed if InfiniTrader also omits the ratio.\r\n"
            + "echo.\r\n"
            + command
            + " start\r\n"
            + "set \"EXIT_CODE=%ERRORLEVEL%\"\r\n"
            + "echo.\r\n"
            + "if not \"%EXIT_CODE%\"==\"0\" echo The bridge did not exit normally. Run the status shortcut for automatic diagnostics.\r\n"
            + "pause\r\n"
            + "exit /b %EXIT_CODE%\r\n"
        ),
        "查看PythonGO桥接状态.cmd": (
            prefix
            + "title WorkBuddy PythonGO Bridge Status\r\n"
            + "echo Checking the current bridge status...\r\n"
            + "echo Diagnostics will run automatically only when the status is abnormal.\r\n"
            + "echo.\r\n"
            + command
            + " status\r\n"
            + "set \"EXIT_CODE=%ERRORLEVEL%\"\r\n"
            + "echo.\r\n"
            + "pause\r\n"
            + "exit /b %EXIT_CODE%\r\n"
        ),
    }
    encoding = "mbcs" if os.name == "nt" else locale.getpreferredencoding(False)
    results = []
    for name, content in scripts.items():
        try:
            encoded = content.encode(encoding)
        except UnicodeEncodeError as exc:
            raise BridgeError(
                "SHORTCUT_PATH_ENCODING_ERROR",
                "Python或配置路径无法写入Windows批处理文件，请改用可由系统代码页表示的路径",
                {"python": python_executable, "config": config_path},
            ) from exc
        results.append(_write_changed(os.path.join(target_dir, name), encoded))
    retired = _retire_obsolete_shortcut(os.path.join(target_dir, "更新PythonGO保证金数据.cmd"))
    return {"directory": target_dir, "scripts": results, "retired": retired}


def merge_mcp_config(path, config_path, python_executable=None):
    path = os.path.abspath(path)
    config = load_config(config_path)
    python_executable = os.path.abspath(python_executable or sys.executable)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                current = json.load(stream)
        except (OSError, ValueError) as exc:
            raise BridgeError(
                "INVALID_MCP_CONFIG",
                "WorkBuddy MCP配置不是有效JSON，未覆盖原文件：%s" % exc,
                {"path": path},
            ) from exc
        if not isinstance(current, dict):
            raise BridgeError("INVALID_MCP_CONFIG", "WorkBuddy MCP配置顶层必须是JSON对象", {"path": path})
    else:
        current = {}
    servers = current.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise BridgeError("INVALID_MCP_CONFIG", "mcpServers必须是JSON对象", {"path": path})
    updated = dict(current)
    updated_servers = dict(servers)
    updated_servers["workbuddy-pythongo"] = {
        "command": python_executable,
        "args": ["-m", "workbuddy_pythongo.mcp_server", "--config", os.path.abspath(config.path)],
    }
    updated["mcpServers"] = updated_servers
    encoded = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return _write_changed(path, encoded)


def _ask_yes_no(prompt, default, input_func=input):
    suffix = " [Y/n]：" if default else " [y/N]："
    while True:
        answer = input_func(prompt + suffix).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("请输入y或n；直接按Enter采用括号中的默认值。")


def _ask_path(prompt, default, input_func=input):
    value = input_func("%s [%s]：" % (prompt, default)).strip()
    return os.path.abspath(os.path.expandvars(value or default))


def run_setup(default_root, input_func=input, getpass_func=input, default_mcp_path=None):
    _header("WorkBuddy-PythonGO首次配置向导")
    print("所有新环境固定从OBSERVE_ONLY开始。")
    print("向导不会启动Worker、无限易或Adapter，不会签名Profile，也不会开放交易。")
    print("直接按Enter可采用显示在方括号中的安全默认值。")

    print("\n[向导 1/5] 选择安全运行目录")
    root = _ask_path("运行目录", os.path.abspath(default_root), input_func)
    init_result = initialize(root)
    config_path = init_result["config"]
    if init_result["created"]:
        print("已创建%d个初始文件；已有同名配置、密钥、token和Profile不会被覆盖。" % len(init_result["created"]))
    else:
        print("检测到已有runtime，已安全复用；配置、密钥、token和Profile均保持原值。")
    print("运行目录：%s" % root)

    print("\n[向导 2/5] 配置WorkBuddy MCP")
    print("合并操作只新增或更新mcpServers.workbuddy-pythongo，并保留其他MCP服务。")
    mcp_result = None
    if _ask_yes_no("是否现在合并WorkBuddy MCP配置", True, input_func):
        mcp_default = os.path.abspath(default_mcp_path or _default_mcp_path())
        mcp_path = _ask_path("WorkBuddy MCP配置文件", mcp_default, input_func)
        mcp_result = merge_mcp_config(mcp_path, config_path)
        print("MCP配置：%s" % mcp_result["path"])
        if mcp_result.get("backup"):
            print("原配置备份：%s" % mcp_result["backup"])
        elif not mcp_result.get("changed"):
            print("MCP配置已经是当前值，无需修改。")
    else:
        print("已跳过。稍后可从runtime中的workbuddy.mcp.example.json手工合并。")

    print("\n[向导 3/5] 绑定无限易投资者账号（可跳过）")
    config = load_config(config_path)
    account = config.account("main_futures")
    binding_missing = "REPLACE" in account.investor_fingerprint.upper()
    bind_result = None
    if binding_missing:
        print("账号仅在本机输入；输入内容会明文显示，请确认周围环境安全。")
        print("完整账号写入本机Adapter配置；Bridge主配置仅保存HMAC指纹。")
        if _ask_yes_no("是否现在绑定投资者账号", True, input_func):
            investor_id = getpass_func("请输入完整投资者账号（明文显示）：").strip()
            if not investor_id:
                raise BridgeError("INVALID_REQUEST", "投资者账号不能为空")
            bind_result = bind_investor(config_path, "main_futures", investor_id, "BIND-ACCOUNT")
            print("账号绑定完成；系统保持本地熔断和OBSERVE_ONLY，必须完成P0后才能签名Profile。")
        else:
            print("已跳过。稍后使用bind-investor本机命令完成绑定。")
    else:
        print("当前runtime已经存在投资者账号指纹，本次向导不会重复绑定。")
        print("这可避免每次运行安装向导都作废Profile并触发本地熔断。")
        print("只有目标无限易账号确实变化时，才使用bind-investor本机命令重新绑定。")

    print("\n[向导 4/5] 创建桌面入口")
    shortcut_result = None
    print("保证金更新已合并到启动入口，不再单独创建更新入口。")
    if _ask_yes_no("是否创建启动和状态两个入口", True, input_func):
        shortcut_dir = _ask_path("入口目录", _desktop_dir(), input_func)
        shortcut_result = create_shortcuts(config_path, shortcut_dir)
        print("桌面入口目录：%s" % shortcut_result["directory"])
    else:
        print("已跳过。稍后可运行create-shortcuts补建启动和状态两个入口。")

    print("\n[向导 5/5] 配置结果与后续人工操作")
    print("Bridge配置：%s" % config_path)
    print("PythonGO运行配置与部署文件：%s" % init_result["ready_dir"])
    print("下一步：")
    print("  1. 如已合并MCP配置，重启WorkBuddy。")
    print("  2. 仅把WorkBuddyPythonGOAdapter.py和pythongo_adapter.path部署到无限易PythonGO策略目录。")
    print("     删除该策略目录中的旧pythongo_adapter.json和pythongo_profile.json。")
    print("  3. 完整启动无限易，并在其中手工启动WorkBuddyPythonGO策略。")
    print("  4. 首次启动Worker必须选择OBSERVE_ONLY。")
    print("  5. 运行状态入口并完成P0清单；证据完整前不要签名Profile或切换模式。")
    print("  6. 以后日常启动使用“启动PythonGO桥接.cmd”，不要重复使用首次安装入口。")
    print("\n配置向导不会自动启动Worker、无限易或WorkBuddy。")
    return {
        "root": root,
        "config": config_path,
        "ready_dir": init_result["ready_dir"],
        "mcp": mcp_result,
        "binding": bind_result,
        "shortcuts": shortcut_result,
    }


def _read_worker_token(config):
    with open(config.worker_token_file, "r", encoding="ascii") as stream:
        token = stream.read().strip()
    if len(token) < 32:
        raise BridgeError("CONFIG_ERROR", "Worker token长度不足")
    return token


def probe_worker(config_path, timeout=1.0):
    config = load_config(config_path)
    try:
        token = _read_worker_token(config)
        client = WorkerClient("http://%s:%d" % (config.host, config.port), token, timeout=timeout)
        response = client.call("pythongo_health", {})
    except (OSError, BridgeError) as exc:
        return {"state": "STOPPED", "message": str(exc), "response": None}
    if response.get("ok"):
        return {"state": "RUNNING", "message": "", "response": response}
    try:
        with socket.create_connection((config.host, config.port), timeout=timeout):
            pass
        state = "CONFLICT"
        message = "本机端口已有进程监听，但没有返回当前Bridge的有效健康响应"
    except OSError:
        state = "STOPPED"
        message = "Worker未启动或本机端口不可访问"
    return {"state": state, "message": message, "response": response}


def _seconds_text(value):
    if not isinstance(value, (int, float)):
        return "暂无数据"
    if value < 1:
        return "不到1秒"
    return "%d秒" % int(value)


def _status_issues(probe):
    state = probe.get("state")
    if state != "RUNNING":
        return [{"code": state or "UNKNOWN", "message": probe.get("message") or "Worker状态异常"}]
    response = probe.get("response") or {}
    if not response.get("ok"):
        return [{"code": "HEALTH_CHECK_FAILED", "message": "Worker健康检查未通过"}]
    health = response.get("data") or {}
    issues = []
    if health.get("worker") != "READY":
        issues.append({"code": "WORKER_NOT_READY", "message": "Worker尚未就绪"})
    if health.get("halted"):
        issues.append({"code": "TRADING_HALTED", "message": "桥接已熔断：%s" % (health.get("halt_reason") or "未记录原因")})
    accounts = health.get("accounts") or []
    if not accounts:
        issues.append({"code": "NO_ENABLED_ACCOUNT", "message": "没有启用账户的状态数据"})
    for account in accounts:
        alias = account.get("account_alias", "<unknown>")
        if not account.get("ready"):
            issues.append({"code": "ADAPTER_NOT_READY", "message": "%s的PythonGO Adapter未就绪" % alias})
        depths = account.get("queue_depths") or {}
        dead_letters = int(depths.get("dead_letter", 0) or 0)
        if dead_letters:
            issues.append({"code": "DEAD_LETTER", "message": "%s存在%d条死信" % (alias, dead_letters)})
    unresolved = int(health.get("unresolved_submit_unknown", 0) or 0)
    if unresolved:
        issues.append({"code": "SUBMIT_UNKNOWN", "message": "存在%d笔需要人工对账的SUBMIT_UNKNOWN" % unresolved})
    return issues


def _header(title):
    print("=" * 60)
    print(title)
    print("=" * 60)


def print_status_human(config_path, probe, issues):
    _header("WorkBuddy-PythonGO桥接状态")
    print("配置文件：%s" % os.path.abspath(config_path))
    state = probe.get("state", "UNKNOWN")
    print("Worker：%s" % {"RUNNING": "正在运行", "STOPPED": "未启动", "CONFLICT": "端口冲突或响应异常"}.get(state, state))
    if state != "RUNNING":
        print("原因：%s" % (probe.get("message") or "未知"))
    else:
        health = (probe.get("response") or {}).get("data") or {}
        mode = health.get("mode", "UNKNOWN")
        print("运行模式：%s" % mode)
        if health.get("halted"):
            print("熔断状态：已熔断；原因：%s" % (health.get("halt_reason") or "未记录"))
        elif mode == "OBSERVE_ONLY":
            print("安全说明：当前只允许查询、预览和空跑闭环。")
        print("\n账户与PythonGO Adapter：")
        for account in health.get("accounts") or []:
            depths = account.get("queue_depths") or {}
            print("  - %s：%s" % (account.get("account_alias", "<unknown>"), "已就绪" if account.get("ready") else "未就绪"))
            print("      Adapter=%s；心跳=%s；模式=%s；Profile=%s；本地熔断=%s" % (
                account.get("adapter_status", "UNKNOWN"),
                _seconds_text(account.get("heartbeat_age_seconds")),
                account.get("adapter_mode") or "UNKNOWN",
                account.get("profile_status", "UNKNOWN"),
                "是" if account.get("local_halt") else "否",
            ))
            print("      队列：命令=%d，ACK=%d，事件=%d，控制=%d，死信=%d" % (
                int(depths.get("commands", 0) or 0),
                int(depths.get("command_acks", 0) or 0),
                int(depths.get("events", 0) or 0),
                int(depths.get("control", 0) or 0),
                int(depths.get("dead_letter", 0) or 0),
            ))
        unresolved = int(health.get("unresolved_submit_unknown", 0) or 0)
        if unresolved:
            print("\n警告：存在%d笔SUBMIT_UNKNOWN，必须人工对账。" % unresolved)
    print("\n当前结论：")
    if issues:
        print("桥接状态异常：")
        for issue in issues:
            print("  - %s" % issue["message"])
    else:
        print("Worker和所有启用账户的PythonGO Adapter均已就绪。")


def print_doctor_human(report):
    _header("自动配置诊断")
    print("诊断结果：%d项错误，%d项警告" % (int(report.get("errors", 0)), int(report.get("warnings", 0))))
    for check in report.get("checks") or []:
        if check.get("ok"):
            marker = "通过"
        elif check.get("severity") == "warning":
            marker = "警告"
        else:
            marker = "错误"
        print("  [%s] %s：%s" % (marker, check.get("name", "unknown"), check.get("detail", "")))


def get_start_mode_availability(config_path):
    blockers = get_non_observe_mode_blockers(config_path)
    return {
        mode: [] if mode == "OBSERVE_ONLY" else list(blockers)
        for mode in START_MODE_SELECTIONS.values()
    }


def _blocker_summary(blockers):
    labels = {
        "PROFILE_INVALID": "P0 Profile未验证或绑定不完整",
        "TRADING_HALTED": "本地熔断尚未解除",
    }
    return "；".join(labels.get(item.get("code"), item.get("message", "门禁未通过")) for item in blockers)


def select_start_mode(availability=None, input_func=input):
    availability = availability or {mode: [] for mode in START_MODE_SELECTIONS.values()}
    while True:
        _header("启动前选择Worker运行模式")
        for selected, mode in START_MODE_SELECTIONS.items():
            blockers = availability.get(mode) or []
            suffix = "  [暂不可用：%s]" % _blocker_summary(blockers) if blockers else ""
            print("  %s. %-13s %s%s" % (selected, mode, START_MODE_DESCRIPTIONS[mode], suffix))
        print("直接按Enter使用安全默认值OBSERVE_ONLY；输入Q可取消启动。")
        selected = input_func("请输入序号 [1]：").strip()
        if selected.lower() == "q":
            return None, None
        mode = START_MODE_SELECTIONS.get(selected or "1")
        if mode is None:
            print("\n输入无效：请输入1、2、3、4，或直接按Enter。\n")
            continue
        blockers = availability.get(mode) or []
        if blockers:
            print("\n%s当前暂不可用，安全门禁没有被绕过：" % mode)
            for blocker in blockers:
                print("  - %s" % _blocker_summary([blocker]))
            action = input_func("按Enter返回模式菜单；输入Q取消启动：").strip().lower()
            if action == "q":
                return None, None
            print("")
            continue
        confirm = None
        if mode != "OBSERVE_ONLY":
            print("\n警告：%s可能产生真实柜台报单；模式名称不能识别柜台是否为模拟环境。" % mode)
            confirm = input_func("确认账户、柜台、Profile和Adapter后，完整输入%s：" % mode).strip()
            if confirm != mode:
                print("\n确认文字不匹配，未切换模式。请重新选择。\n")
                continue
        return mode, confirm


def start_desktop(config_path):
    config = load_config(config_path)
    probe = probe_worker(config.path)
    if probe["state"] == "RUNNING":
        issues = _status_issues(probe)
        print_status_human(config.path, probe, issues)
        print("\n无需重复启动：现有Worker将继续运行。")
        return 0
    if probe["state"] == "CONFLICT":
        print_status_human(config.path, probe, _status_issues(probe))
        return 2
    while True:
        availability = get_start_mode_availability(config.path)
        mode, confirm = select_start_mode(availability)
        if mode is None:
            print("\n已取消启动；配置、模式和熔断状态均未改变。")
            return 0
        try:
            changed = set_mode(config.path, mode, confirm)
            break
        except BridgeError as exc:
            if mode == "OBSERVE_ONLY" or exc.code not in {"PROFILE_INVALID", "SIGNATURE_INVALID", "TRADING_HALTED"}:
                raise
            print("\n%s启动前的安全状态已经变化：%s" % (mode, exc.message))
            action = input("按Enter重新检查并返回模式菜单；输入Q取消启动：").strip().lower()
            if action == "q":
                print("\n已取消启动；安全门禁没有被绕过。")
                return 0
    print("\n本次启动模式：%s" % mode)
    print("ready目录中的Adapter配置已同步。")
    print("无限易通过pythongo_adapter.path直接读取ready配置，无需复制JSON或Profile。")
    print("模式或Profile变化后仍必须完整重启无限易，使Adapter重新加载。")
    if mode == "OBSERVE_ONLY":
        print("当前只允许查询、预览和空跑闭环；本地熔断状态不会被启动器解除。")
    elif mode == "MANUAL_LIVE":
        print("仍需为每笔预览授权，或创建1至60分钟人工会话。")
    elif mode == "LIMITED_AUTO":
        print("仍需readiness通过，并由WorkBuddy创建限时、限范围的P1策略许可。")
    if changed.get("adapter_configs"):
        for path in changed["adapter_configs"]:
            print("Adapter配置：%s" % path)
    print("\n正在启动Worker：http://%s:%d" % (config.host, config.port))
    print("请保持此窗口打开；按Ctrl+C可安全停止Worker。")
    args = ["--config", config.path]
    if mode != "OBSERVE_ONLY":
        args.extend(["--confirm-mode", mode])
    return worker_main(args)


def status_desktop(config_path):
    config = load_config(config_path)
    probe = probe_worker(config.path)
    issues = _status_issues(probe)
    print_status_human(config.path, probe, issues)
    if issues:
        print("\n" + "-" * 60)
        print("检测到状态异常，正在自动运行doctor……")
        print("-" * 60 + "\n")
        print_doctor_human(run_doctor(config.path))
        return 1
    print("\n状态正常，无需执行额外诊断。")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="WorkBuddy-PythonGO桌面启动与状态入口")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="运行首次交互配置向导")
    setup.add_argument("--root", required=True)
    sub.add_parser("start", help="交互选择模式并在当前窗口启动Worker")
    sub.add_parser("status", help="检查实时状态，异常时自动运行doctor")
    shortcuts = sub.add_parser("create-shortcuts", help="创建桌面启动和状态脚本")
    shortcuts.add_argument("--target-dir")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup":
            run_setup(args.root)
            return 0
        if not args.config:
            raise BridgeError("CONFIG_REQUIRED", "该命令需要--config")
        if args.command == "start":
            return start_desktop(args.config)
        if args.command == "status":
            return status_desktop(args.config)
        result = create_shortcuts(args.config, target_dir=args.target_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except BridgeError as exc:
        print("操作失败：%s" % exc.message, file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print("操作失败：%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
