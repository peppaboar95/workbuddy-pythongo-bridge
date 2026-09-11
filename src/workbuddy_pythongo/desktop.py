import argparse
import ctypes
import datetime as dt
import glob
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
from .margin_reference import migrate_margin_policy_if_safe
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

APP_DATA_DIRECTORY = "WorkBuddyPythonGO"
RUNTIME_POINTER_NAME = "runtime.path"
DEPLOYMENT_FILENAMES = ("WorkBuddyPythonGOAdapter.py", "pythongo_adapter.path")
LEGACY_DEPLOYMENT_FILENAMES = ("pythongo_adapter.json", "pythongo_profile.json")

DOCTOR_LABELS = {
    "bridge_config": "Bridge配置",
    "message_keyring": "本机消息密钥",
    "worker_token": "Worker访问令牌",
    "sqlite": "本机运行数据库",
    "runtime": "运行环境",
    "adapter_hash": "Adapter版本",
    "adapter_config_locator": "Adapter定位文件",
    "adapter_config_schema": "Adapter配置格式",
    "identity_binding": "账户与实例绑定",
    "investor_binding": "投资者账号绑定",
    "mode_sync": "Worker与Adapter模式",
    "risk_limit_sync": "双端风险限制",
    "profile_schema": "交易能力档案格式",
    "profile_signature": "交易能力档案签名",
    "profile_p0_binding": "现场验证绑定",
    "profile_build_binding": "客户端版本绑定",
    "ready_bundle": "无限易部署包",
    "heartbeat": "无限易Adapter连接",
    "margin_policy_heartbeat": "保证金策略同步",
}

DOCTOR_ACTIONS = {
    "bridge_config": "重新运行“首次安装与配置.cmd”，并确认运行目录。",
    "message_keyring": "恢复原runtime备份；不要从其他电脑复制密钥。",
    "worker_token": "恢复原runtime备份，或在确认不再使用旧环境后新建runtime。",
    "runtime": "先运行首次配置向导；若提示策略迁移，按屏幕给出的命令复核。",
    "adapter_hash": "重新运行安装向导，再把新版Adapter部署到无限易策略目录。",
    "adapter_config_locator": "重新运行安装向导中的“部署到无限易”步骤。",
    "adapter_config_schema": "恢复配置备份，或重新运行安装向导生成兼容配置。",
    "identity_binding": "确认选择了正确runtime；账号确实变化时再重新绑定。",
    "investor_binding": "使用本机绑定命令重新输入当前无限易投资者账号。",
    "mode_sync": "完整退出并重启无限易，让Adapter重新加载当前模式。",
    "risk_limit_sync": "重新运行安装向导生成一致的双端风险配置。",
    "profile_schema": "交易暂不可用；查询不受影响。需要交易时再运行交易启用流程。",
    "profile_signature": "交易暂不可用；查询不受影响。完成现场验证后再签名。",
    "profile_p0_binding": "重新核对目标账号、客户端和已验证交易动作。",
    "profile_build_binding": "客户端版本发生变化；交易前重新完成相应现场验证。",
    "ready_bundle": "重新运行安装向导并重新部署Adapter。",
    "heartbeat": "启动无限易、登录账号，并在PythonGO中启动WorkBuddyPythonGO策略。",
    "margin_policy_heartbeat": "完整退出并重启无限易，使Adapter加载当前保证金策略。",
}


def _default_mcp_path():
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")


def _local_app_data_dir(environ=None):
    environ = os.environ if environ is None else environ
    value = environ.get("LOCALAPPDATA")
    if value:
        return os.path.abspath(os.path.expandvars(value))
    return os.path.abspath(os.path.join(os.path.expanduser("~"), "AppData", "Local"))


def default_runtime_root(environ=None):
    return os.path.join(_local_app_data_dir(environ), APP_DATA_DIRECTORY, "runtime")


def runtime_pointer_path(environ=None):
    return os.path.join(_local_app_data_dir(environ), APP_DATA_DIRECTORY, RUNTIME_POINTER_NAME)


def _is_runtime_root(path):
    return bool(path) and os.path.isfile(os.path.join(os.path.abspath(path), "config", "bridge.json"))


def discover_runtime_root(default_root, legacy_root=None, pointer_path=None):
    default_root = os.path.abspath(default_root)
    pointer_path = os.path.abspath(pointer_path or runtime_pointer_path())
    stale_pointer = None
    try:
        with open(pointer_path, "r", encoding="utf-8-sig") as stream:
            saved = stream.readline().strip()
        if saved:
            saved = os.path.abspath(os.path.expandvars(saved))
            if _is_runtime_root(saved):
                return {"path": saved, "source": "saved", "pointer": pointer_path, "stale_pointer": None}
            stale_pointer = saved
    except FileNotFoundError:
        pass
    if legacy_root and _is_runtime_root(legacy_root):
        return {
            "path": os.path.abspath(legacy_root),
            "source": "legacy",
            "pointer": pointer_path,
            "stale_pointer": stale_pointer,
        }
    return {
        "path": default_root,
        "source": "existing" if _is_runtime_root(default_root) else "new",
        "pointer": pointer_path,
        "stale_pointer": stale_pointer,
    }


def remember_runtime_root(root, pointer_path=None):
    pointer_path = os.path.abspath(pointer_path or runtime_pointer_path())
    return _write_changed(pointer_path, (os.path.abspath(root) + "\n").encode("utf-8"))


def discover_mcp_config_paths(environ=None, home=None):
    environ = os.environ if environ is None else environ
    home = os.path.abspath(home or os.path.expanduser("~"))
    candidates = []
    override = environ.get("WORKBUDDY_MCP_CONFIG")
    if override:
        candidates.append(os.path.expandvars(override))
    appdata = environ.get("APPDATA")
    localappdata = environ.get("LOCALAPPDATA")
    candidates.append(os.path.join(home, ".workbuddy", "mcp.json"))
    if appdata:
        candidates.extend([
            os.path.join(appdata, "WorkBuddy", "mcp.json"),
            os.path.join(appdata, "Tencent", "WorkBuddy", "mcp.json"),
        ])
    if localappdata:
        candidates.append(os.path.join(localappdata, "WorkBuddy", "mcp.json"))
    result = []
    seen = set()
    for path in candidates:
        normalized = os.path.abspath(os.path.expandvars(path))
        key = os.path.normcase(normalized)
        if key not in seen and os.path.isfile(normalized):
            seen.add(key)
            result.append(normalized)
    return result


def discover_pythongo_strategy_dirs(environ=None, home=None):
    environ = os.environ if environ is None else environ
    if os.name != "nt" and not any(environ.get(name) for name in ("INFINITRADER_HOME", "INFINITRADER_PATH")):
        return []
    home = os.path.abspath(home or os.path.expanduser("~"))
    roots = [
        environ.get("INFINITRADER_HOME"),
        environ.get("INFINITRADER_PATH"),
        os.path.join(home, "Documents"),
        environ.get("ProgramFiles"),
        environ.get("ProgramFiles(x86)"),
        environ.get("LOCALAPPDATA"),
    ]
    patterns = []
    for root in roots:
        if not root:
            continue
        root = os.path.abspath(os.path.expandvars(root))
        patterns.extend([
            os.path.join(root, "pyStrategy", "self_strategy"),
            os.path.join(root, "*", "pyStrategy", "self_strategy"),
            os.path.join(root, "*", "*", "pyStrategy", "self_strategy"),
        ])
    result = []
    seen = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            normalized = os.path.abspath(path)
            key = os.path.normcase(normalized)
            if key not in seen and os.path.isdir(normalized):
                seen.add(key)
                result.append(normalized)
    return sorted(result, key=lambda value: os.path.normcase(value))


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


def deploy_adapter_files(ready_dir, strategy_dir):
    ready_dir = os.path.abspath(ready_dir)
    strategy_dir = os.path.abspath(os.path.expandvars(strategy_dir))
    if (
        os.path.basename(strategy_dir).lower() != "self_strategy"
        or os.path.basename(os.path.dirname(strategy_dir)).lower() != "pystrategy"
    ):
        raise BridgeError(
            "INVALID_STRATEGY_DIRECTORY",
            "目标必须是无限易的pyStrategy\\self_strategy目录",
            {"path": strategy_dir},
        )
    py_strategy_dir = os.path.dirname(strategy_dir)
    if not os.path.isdir(py_strategy_dir):
        raise BridgeError(
            "STRATEGY_PARENT_NOT_FOUND",
            "未找到目标目录的pyStrategy父目录，请先确认无限易安装位置",
            {"path": py_strategy_dir},
        )
    sources = [os.path.join(ready_dir, name) for name in DEPLOYMENT_FILENAMES]
    missing = [path for path in sources if not os.path.isfile(path)]
    if missing:
        raise BridgeError("READY_BUNDLE_INCOMPLETE", "runtime中的无限易部署文件不完整", {"missing": missing})
    os.makedirs(strategy_dir, exist_ok=True)
    deployed = []
    for source, name in zip(sources, DEPLOYMENT_FILENAMES):
        with open(source, "rb") as stream:
            deployed.append(_write_changed(os.path.join(strategy_dir, name), stream.read()))
    retired = []
    for name in LEGACY_DEPLOYMENT_FILENAMES:
        result = _retire_obsolete_shortcut(os.path.join(strategy_dir, name))
        if result:
            retired.append(result)
    return {"directory": strategy_dir, "files": deployed, "retired": retired}


def _select_mcp_path(default_mcp_path, input_func):
    if default_mcp_path:
        return _ask_path("WorkBuddy MCP配置文件", os.path.abspath(default_mcp_path), input_func)
    discovered = discover_mcp_config_paths()
    if not discovered:
        print("未发现现有MCP配置，将使用WorkBuddy常用位置。")
        return _ask_path("WorkBuddy MCP配置文件", _default_mcp_path(), input_func)
    if len(discovered) == 1:
        print("已发现WorkBuddy MCP配置：%s" % discovered[0])
        return _ask_path("WorkBuddy MCP配置文件", discovered[0], input_func)
    print("发现多个MCP配置，请选择要更新的文件：")
    for index, path in enumerate(discovered, 1):
        print("  %d. %s" % (index, path))
    while True:
        selected = input_func("请输入序号 [1]，或输入C填写其他路径：").strip().lower()
        if not selected:
            return discovered[0]
        if selected == "c":
            return _ask_path("WorkBuddy MCP配置文件", discovered[0], input_func)
        if selected.isdigit() and 1 <= int(selected) <= len(discovered):
            return discovered[int(selected) - 1]
        print("请输入列表中的序号，或输入C。")


def _select_strategy_dir(candidates, input_func):
    candidates = list(candidates)
    if candidates:
        print("已发现以下无限易PythonGO策略目录：")
        for index, path in enumerate(candidates, 1):
            print("  %d. %s" % (index, path))
        while True:
            default = "1" if len(candidates) == 1 else "S"
            selected = input_func("请输入序号 [%s]，输入C填写路径，输入S暂时跳过：" % default).strip().lower()
            selected = selected or default.lower()
            if selected == "s":
                return None
            if selected == "c":
                value = input_func("请输入pyStrategy\\self_strategy完整路径：").strip()
                return os.path.abspath(os.path.expandvars(value)) if value else None
            if selected.isdigit() and 1 <= int(selected) <= len(candidates):
                return candidates[int(selected) - 1]
            print("请输入列表中的序号、C或S。")
    value = input_func("未自动发现无限易；可输入pyStrategy\\self_strategy完整路径，直接按Enter暂时跳过：").strip()
    return os.path.abspath(os.path.expandvars(value)) if value else None


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
            + "title WorkBuddy PythonGO 桥接启动器\r\n"
            + "echo 请先选择Worker运行模式，随后桥接服务会在当前窗口启动。\r\n"
            + "echo 在模式提示处直接按Enter，将使用安全的OBSERVE_ONLY默认值。\r\n"
            + "echo Worker运行期间请保持此窗口打开。\r\n"
            + "echo.\r\n"
            + manager_command
            + " refresh-margin-reference --if-due >nul 2>&1\r\n"
            + "set \"MARGIN_REFRESH_EXIT=%ERRORLEVEL%\"\r\n"
            + "if \"%MARGIN_REFRESH_EXIT%\"==\"3\" goto margin_policy_migration_required\r\n"
            + "if not \"%MARGIN_REFRESH_EXIT%\"==\"0\" echo [警告] 保证金参考刷新失败；若无限易也未提供比例，开仓会继续安全拒绝。\r\n"
            + "echo.\r\n"
            + command
            + " start\r\n"
            + "set \"EXIT_CODE=%ERRORLEVEL%\"\r\n"
            + "echo.\r\n"
            + "if not \"%EXIT_CODE%\"==\"0\" echo 桥接服务未正常退出，请双击“查看PythonGO桥接状态.cmd”自动诊断。\r\n"
            + "pause\r\n"
            + "exit /b %EXIT_CODE%\r\n"
            + ":margin_policy_migration_required\r\n"
            + "echo.\r\n"
            + "echo [需要复核] 保证金策略发生实质变化，交易暂不启用；查询仍可使用。\r\n"
            + "echo 请运行下面的命令并核对结果，然后重新启动桥接：\r\n"
            + "echo.\r\n"
            + "echo "
            + manager_command
            + " migrate-margin-policy --confirm MIGRATE-MARGIN-POLICY\r\n"
            + "echo.\r\n"
            + "echo 实质策略变化会保留Profile签名，但交易会保持保护状态，直到完成复核。\r\n"
            + "pause\r\n"
            + "exit /b 3\r\n"
        ),
        "查看PythonGO桥接状态.cmd": (
            prefix
            + "title WorkBuddy PythonGO 桥接状态\r\n"
            + "echo 正在检查桥接状态……\r\n"
            + "echo 仅在状态异常时自动运行详细诊断。\r\n"
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


def run_setup(
    default_root,
    input_func=input,
    default_mcp_path=None,
    discover_existing=False,
    legacy_root=None,
    pointer_path=None,
    strategy_candidates=None,
):
    _header("WorkBuddy-PythonGO首次配置向导")
    print("所有新环境固定从OBSERVE_ONLY开始。")
    print("查询功能开箱即用；交易功能以后需要时再单独启用。")
    print("向导不会启动Worker或无限易，不会签名Profile，也不会开放交易。")
    print("直接按Enter可采用显示在方括号中的安全默认值。")

    print("\n[向导 1/6] 选择稳定运行目录")
    runtime_discovery = {
        "path": os.path.abspath(default_root), "source": "requested",
        "pointer": os.path.abspath(pointer_path) if pointer_path else runtime_pointer_path(),
        "stale_pointer": None,
    }
    if discover_existing:
        runtime_discovery = discover_runtime_root(default_root, legacy_root, pointer_path)
        source_labels = {
            "saved": "上次保存的运行目录", "legacy": "旧安装包中的已有运行目录",
            "existing": "默认位置中的已有运行目录", "new": "新的稳定运行目录",
        }
        print("已选择%s：%s" % (source_labels.get(runtime_discovery["source"], "运行目录"), runtime_discovery["path"]))
        if runtime_discovery.get("stale_pointer"):
            print("提示：上次保存的位置已不存在，已安全回退：%s" % runtime_discovery["stale_pointer"])
    root = _ask_path("运行目录", runtime_discovery["path"], input_func)
    init_result = initialize(root)
    config_path = init_result["config"]
    pointer_result = remember_runtime_root(root, pointer_path)
    if init_result["created"]:
        print("已创建%d个初始文件；已有同名配置、密钥、token和Profile不会被覆盖。" % len(init_result["created"]))
    else:
        print("检测到已有runtime，已安全复用；配置、密钥、token和Profile均保持原值。")
    print("运行目录：%s" % root)
    migration_result = migrate_margin_policy_if_safe(config_path)
    if migration_result.get("review_required"):
        print("检测到保证金风险策略实质变化；查询仍可使用，交易前请运行：")
        print('  "%s" -m workbuddy_pythongo.manager --config "%s" migrate-margin-policy --confirm MIGRATE-MARGIN-POLICY' % (sys.executable, config_path))
    elif migration_result.get("migrated"):
        print("已自动补齐保证金策略跟踪字段；Profile保持原值，未开启交易保护。")

    print("\n[向导 2/6] 配置WorkBuddy MCP")
    print("合并操作只新增或更新mcpServers.workbuddy-pythongo，并保留其他MCP服务。")
    mcp_result = None
    if _ask_yes_no("是否现在合并WorkBuddy MCP配置", True, input_func):
        mcp_path = _select_mcp_path(default_mcp_path, input_func)
        mcp_result = merge_mcp_config(mcp_path, config_path)
        print("MCP配置：%s" % mcp_result["path"])
        if mcp_result.get("backup"):
            print("原配置备份：%s" % mcp_result["backup"])
        elif not mcp_result.get("changed"):
            print("MCP配置已经是当前值，无需修改。")
    else:
        print("已跳过。稍后可从runtime中的workbuddy.mcp.example.json手工合并。")

    print("\n[向导 3/6] 绑定无限易投资者账号（可跳过）")
    config = load_config(config_path)
    account = config.account("main_futures")
    binding_missing = "REPLACE" in account.investor_fingerprint.upper()
    bind_result = None
    if binding_missing:
        print("请输入投资者账号（不是密码）；内容会明文显示，便于核对。")
        print("完整账号写入本机Adapter配置；Bridge主配置仅保存HMAC指纹。")
        if _ask_yes_no("是否现在绑定投资者账号", True, input_func):
            investor_id = input_func("请输入完整投资者账号（明文显示）：").strip()
            if not investor_id:
                raise BridgeError("INVALID_REQUEST", "投资者账号不能为空")
            bind_result = bind_investor(config_path, "main_futures", investor_id, "BIND-ACCOUNT")
            print("账号绑定完成；查询功能可正常配置。交易尚未启用，不属于故障或熔断。")
        else:
            print("已跳过。稍后使用bind-investor本机命令完成绑定。")
    else:
        print("当前runtime已经存在投资者账号指纹，本次向导不会重复绑定。")
        print("这可避免每次运行安装向导都作废Profile并触发本地熔断。")
        print("只有目标无限易账号确实变化时，才使用bind-investor本机命令重新绑定。")

    print("\n[向导 4/6] 部署无限易PythonGO策略文件")
    deployment_result = None
    candidates = discover_pythongo_strategy_dirs() if strategy_candidates is None else strategy_candidates
    strategy_dir = _select_strategy_dir(candidates, input_func)
    if strategy_dir:
        try:
            deployment_result = deploy_adapter_files(init_result["ready_dir"], strategy_dir)
            print("已部署到：%s" % deployment_result["directory"])
            for item in deployment_result["files"]:
                if item.get("backup"):
                    print("已备份旧文件：%s" % item["backup"])
            for item in deployment_result["retired"]:
                print("已停用旧配置并保留备份：%s" % item["backup"])
        except (BridgeError, OSError) as exc:
            message = exc.message if isinstance(exc, BridgeError) else str(exc)
            deployment_result = {"ok": False, "directory": strategy_dir, "error": message}
            print("自动部署未完成：%s" % message)
            print("稍后可重新运行首次配置向导；现有无限易文件未被直接删除。")
    else:
        print("已跳过自动部署；配置结果中会保留手工部署提示。")

    print("\n[向导 5/6] 创建桌面入口")
    shortcut_result = None
    print("保证金更新已合并到启动入口，不再单独创建更新入口。")
    if _ask_yes_no("是否创建启动和状态两个入口", True, input_func):
        shortcut_dir = _ask_path("入口目录", _desktop_dir(), input_func)
        shortcut_result = create_shortcuts(config_path, shortcut_dir)
        print("桌面入口目录：%s" % shortcut_result["directory"])
    else:
        print("已跳过。稍后可运行create-shortcuts补建启动和状态两个入口。")

    print("\n[向导 6/6] 配置完成")
    print("Bridge配置：%s" % config_path)
    print("PythonGO运行配置与部署文件：%s" % init_result["ready_dir"])
    print("查询功能的下一步：")
    print("  1. 如已合并MCP配置，重启WorkBuddy。")
    if deployment_result and deployment_result.get("ok", True):
        print("  2. 启动无限易，并在PythonGO中启动WorkBuddyPythonGOAdapter策略。")
    else:
        print("  2. 把ready目录中的两个部署文件放入无限易pyStrategy\\self_strategy目录。")
        print("     只需要WorkBuddyPythonGOAdapter.py和pythongo_adapter.path；旧JSON请改名备份。")
        print("  3. 启动无限易，并在PythonGO中启动WorkBuddyPythonGOAdapter策略。")
    print("  4. 双击“启动PythonGO桥接.cmd”，直接按Enter使用OBSERVE_ONLY。")
    print("  5. 此时即可使用查询接口；P0、Profile签名和解除交易保护都不是查询前置步骤。")
    print("以后需要交易时：再按文档执行一次性P0验证、签名Profile并选择交易模式。")
    print("日常启动只使用“启动PythonGO桥接.cmd”，无需重复安装。")
    print("\n配置向导不会自动启动Worker、无限易或WorkBuddy。")
    return {
        "root": root,
        "config": config_path,
        "ready_dir": init_result["ready_dir"],
        "margin_policy_migration": migration_result,
        "runtime_discovery": runtime_discovery,
        "runtime_pointer": pointer_result,
        "mcp": mcp_result,
        "binding": bind_result,
        "deployment": deployment_result,
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
    if health.get("halted") and health.get("mode") != "OBSERVE_ONLY":
        issues.append({"code": "TRADING_HALTED", "message": "交易保护已开启：%s" % (health.get("halt_reason") or "未记录原因")})
    accounts = health.get("accounts") or []
    if not accounts:
        issues.append({"code": "NO_ENABLED_ACCOUNT", "message": "没有启用账户的状态数据"})
    for account in accounts:
        alias = account.get("account_alias", "<unknown>")
        observation_ready = account.get("observation_ready")
        if observation_ready is None:
            observation_ready = account.get("ready")
        if not observation_ready:
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
        if state == "STOPPED":
            print("下一步：双击“启动PythonGO桥接.cmd”，在模式选择处直接按Enter。")
        elif state == "CONFLICT":
            print("下一步：关闭占用Bridge端口的其他程序，再重新启动桥接。")
    else:
        health = (probe.get("response") or {}).get("data") or {}
        mode = health.get("mode", "UNKNOWN")
        print("运行模式：%s" % mode)
        protection = health.get("trade_protection") or {}
        protection_kind = protection.get("kind") or ("INCIDENT_HALT" if health.get("halted") else "NONE")
        if health.get("halted") or protection.get("active"):
            if protection_kind == "SETUP_LOCK" and not health.get("halted"):
                print("交易状态：尚未启用（首次配置状态，不是故障或熔断）。")
                print("需要交易时：完成一次性P0验证并签名Profile；只查询无需处理。")
            elif protection_kind == "POLICY_REVIEW":
                print("交易状态：保证金策略需要复核；查询不受影响。")
                print("原因：%s" % (health.get("halt_reason") or protection.get("reason") or "未记录"))
            else:
                print("交易保护：已开启；原因：%s" % (health.get("halt_reason") or protection.get("reason") or "未记录"))
            if health.get("observation_ready") or protection.get("queries_available"):
                print("查询状态：可用；仅阻止新的交易提交。")
        elif mode == "OBSERVE_ONLY":
            print("交易保护：观察模式；查询、预览和空跑闭环可用。")
        print("\n账户与PythonGO Adapter：")
        for account in health.get("accounts") or []:
            depths = account.get("queue_depths") or {}
            observation_ready = account.get("observation_ready")
            if observation_ready is None:
                observation_ready = account.get("ready")
            print("  - %s：查询链路%s" % (account.get("account_alias", "<unknown>"), "可用" if observation_ready else "不可用"))
            print("      Adapter=%s；心跳=%s；模式=%s；Profile=%s；交易保护=%s" % (
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
        health = (probe.get("response") or {}).get("data") or {}
        protection = health.get("trade_protection") or {}
        if health.get("mode") == "OBSERVE_ONLY" and (health.get("halted") or protection.get("active")):
            print("Worker和查询链路正常；新的交易提交受保护。")
        else:
            print("Worker和所有启用账户的PythonGO Adapter均已就绪。")


def print_doctor_human(report):
    _header("自动配置诊断")
    print("诊断结果：%d项错误，%d项警告" % (int(report.get("errors", 0)), int(report.get("warnings", 0))))
    actions = []
    for check in report.get("checks") or []:
        if check.get("ok"):
            marker = "通过"
        elif check.get("severity") == "warning":
            marker = "警告"
        else:
            marker = "错误"
        raw_name = check.get("name", "unknown")
        key = raw_name.rsplit(":", 1)[-1]
        label = DOCTOR_LABELS.get(key, raw_name)
        print("  [%s] %s：%s" % (marker, label, check.get("detail", "")))
        action = DOCTOR_ACTIONS.get(key)
        if not check.get("ok") and action and action not in actions:
            actions.append(action)
    if actions:
        print("\n建议操作：")
        for index, action in enumerate(actions, 1):
            print("  %d. %s" % (index, action))


def get_start_mode_availability(config_path):
    blockers = get_non_observe_mode_blockers(config_path)
    return {
        mode: [] if mode == "OBSERVE_ONLY" else list(blockers)
        for mode in START_MODE_SELECTIONS.values()
    }


def _blocker_summary(blockers):
    labels = {
        "PROFILE_INVALID": "P0 Profile未验证或绑定不完整",
        "TRADING_HALTED": "交易保护尚未解除",
        "TRADE_PROTECTION_ACTIVE": "交易功能尚未启用或仍需复核",
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
            if mode == "OBSERVE_ONLY" or exc.code not in {
                "PROFILE_INVALID", "SIGNATURE_INVALID", "TRADING_HALTED", "TRADE_PROTECTION_ACTIVE",
            }:
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
        print("当前可使用查询、预览和空跑闭环；交易保护状态不会被启动器解除。")
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
    setup.add_argument("--discover-existing", action="store_true", help="优先复用上次保存或旧安装包中的runtime")
    setup.add_argument("--legacy-root", help="旧安装包中可能存在的runtime目录")
    sub.add_parser("start", help="交互选择模式并在当前窗口启动Worker")
    sub.add_parser("status", help="检查实时状态，异常时自动运行doctor")
    shortcuts = sub.add_parser("create-shortcuts", help="创建桌面启动和状态脚本")
    shortcuts.add_argument("--target-dir")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup":
            run_setup(args.root, discover_existing=args.discover_existing, legacy_root=args.legacy_root)
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
