import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import load_config
from .errors import BridgeError
from .security import KeyRing
from .util import atomic_write_bytes, atomic_write_json, iso_now, parse_time, utc_now


REQUIRED_COLUMNS = {
    "交易所名称", "合约代码", "保证金-买", "保证金-卖", "保证金-每手",
    "手续费标准-开仓-万分之", "手续费标准-开仓-元",
    "手续费标准-平昨-万分之", "手续费标准-平昨-元",
    "手续费标准-平今-万分之", "手续费标准-平今-元", "手续费更新时间",
}
EXCHANGE_NAMES = {
    "上海期货交易所": "SHFE",
    "大连商品交易所": "DCE",
    "郑州商品交易所": "CZCE",
    "上海国际能源交易中心": "INE",
    "广州期货交易所": "GFEX",
    "中国金融期货交易所": "CFFEX",
}
FUTURES_COMM_INFO_URL = "https://www.9qihuo.com/qihuoshouxufei"
FUTURES_COMM_DOWNLOAD_TIMEOUT_SECONDS = 60
FUTURES_COMM_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
FUTURES_COMM_MIN_ROWS = 500
MARGIN_REFERENCE_SCHEMA_VERSION = 2


class FuturesFeeTableParser(HTMLParser):
    """Extract only the desktop fee table used by the 9qihuo export button."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_target_table = False
        self.table_depth = 0
        self.current_row = None
        self.current_cell = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "table":
            if self.in_target_table:
                self.table_depth += 1
            elif attrs_dict.get("id") == "heyuetbl":
                self.in_target_table = True
                self.table_depth = 1
            return
        if not self.in_target_table:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = {"parts": [], "attrs": attrs_dict}
        elif tag == "br" and self.current_cell is not None:
            self.current_cell["parts"].append(" ")

    def handle_data(self, data):
        if self.in_target_table and self.current_cell is not None:
            self.current_cell["parts"].append(data)

    def handle_endtag(self, tag):
        if not self.in_target_table:
            return
        if tag in {"td", "th"} and self.current_cell is not None:
            self.current_cell["text"] = " ".join("".join(self.current_cell["parts"]).split())
            self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self.in_target_table = False


def _parse_number(value):
    matched = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(matched.group()) if matched else float("nan")


def _parse_fee_cell(value):
    text = str(value).replace(",", "")
    rate_match = re.search(r"(\d+(?:\.\d+)?)\s*/?\s*万分之", text)
    amount_match = re.search(r"\((\d+(?:\.\d+)?)\s*元\)", text)
    if amount_match is None and "万分之" not in text:
        amount_match = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
    rate = float(rate_match.group(1)) / 10_000 if rate_match else float("nan")
    amount = float(amount_match.group(1)) if amount_match else float("nan")
    return rate, amount


def _extract_update_time(title):
    matched = re.search(r"更新时间[:：]\s*(.+?)\s*$", str(title))
    return matched.group(1) if matched else ""


def _parse_9qihuo_html(html_text):
    parser = FuturesFeeTableParser()
    parser.feed(html_text)
    parser.close()
    records = []
    current_exchange = ""
    contract_pattern = re.compile(r"^(.*?)\s*\(([A-Za-z]+\d{3,4}[A-Za-z]?)\)\s*$")
    for row in parser.rows:
        if len(row) == 1:
            title = row[0].get("text", "").strip()
            if title in EXCHANGE_NAMES:
                current_exchange = title
            continue
        if len(row) < 13:
            continue
        cells = [cell.get("text", "").strip() for cell in row]
        contract_match = contract_pattern.match(cells[0])
        if not contract_match or not current_exchange:
            continue
        limit_parts = cells[2].split("/", 1)
        open_rate, open_amount = _parse_fee_cell(cells[6])
        close_rate, close_amount = _parse_fee_cell(cells[7])
        close_today_rate, close_today_amount = _parse_fee_cell(cells[8])
        records.append({
            "交易所名称": current_exchange,
            "交易所代码": EXCHANGE_NAMES[current_exchange],
            "合约名称": contract_match.group(1).strip(),
            "合约代码": contract_match.group(2).strip(),
            "现价": _parse_number(cells[1]),
            "涨停板": _parse_number(limit_parts[0]),
            "跌停板": _parse_number(limit_parts[1]) if len(limit_parts) == 2 else float("nan"),
            "保证金-买": _parse_number(cells[3]),
            "保证金-卖": _parse_number(cells[4]),
            "保证金-每手": _parse_number(cells[5]),
            "手续费标准-开仓-万分之": open_rate,
            "手续费标准-开仓-元": open_amount,
            "手续费标准-平昨-万分之": close_rate,
            "手续费标准-平昨-元": close_amount,
            "手续费标准-平今-万分之": close_today_rate,
            "手续费标准-平今-元": close_today_amount,
            "每跳毛利": _parse_number(cells[9]),
            "手续费": _parse_number(cells[10]),
            "每跳净利": _parse_number(cells[11]),
            "备注": cells[12],
            "手续费更新时间": _extract_update_time(row[0].get("attrs", {}).get("title", "")),
            "价格更新时间": _extract_update_time(row[1].get("attrs", {}).get("title", "")),
            "数据来源": FUTURES_COMM_INFO_URL,
        })
    unique = {}
    duplicates = set()
    for record in records:
        key = (record["交易所代码"], record["合约代码"].lower())
        if key in unique:
            duplicates.add(key)
        else:
            unique[key] = record
    for key in duplicates:
        unique.pop(key, None)
    records = [
        record for record in unique.values()
        if all(math.isfinite(float(record[name])) and float(record[name]) > 0 for name in (
            "保证金-买", "保证金-卖", "保证金-每手",
        ))
    ]
    missing_exchanges = set(EXCHANGE_NAMES) - {record["交易所名称"] for record in records}
    if missing_exchanges:
        raise ValueError("九期网手续费表格缺少交易所：" + "、".join(sorted(missing_exchanges)))
    if len(records) < FUTURES_COMM_MIN_ROWS:
        raise ValueError(
            "九期网手续费表格记录数不足：%d < %d" % (len(records), FUTURES_COMM_MIN_ROWS)
        )
    return records


def _encode_records(records):
    fieldnames = list(records[0])
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)
    return stream.getvalue().encode("utf-8")


def _parse_local_time(value):
    text = str(value or "").strip()
    china = dt.timezone(dt.timedelta(hours=8))
    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(text, pattern).replace(tzinfo=china).astimezone(dt.timezone.utc)
        except ValueError:
            pass
    now = utc_now().astimezone(china)
    for pattern in ("%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S", "%m-%d %H:%M"):
        for year in (now.year, now.year - 1):
            try:
                parsed = dt.datetime.strptime("%d-%s" % (year, text), "%Y-" + pattern)
            except ValueError:
                continue
            candidate = parsed.replace(tzinfo=china)
            if candidate <= now + dt.timedelta(days=1):
                return candidate.astimezone(dt.timezone.utc)
    raise ValueError("invalid source update time")


def validate_margin_csv(encoded):
    try:
        text = encoded.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
    except (AttributeError, UnicodeError, csv.Error) as exc:
        raise BridgeError("MARGIN_REFERENCE_INVALID", "保证金文件不是有效UTF-8 CSV：%s" % exc) from exc
    if not isinstance(reader.fieldnames, list) or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        raise BridgeError(
            "MARGIN_REFERENCE_INVALID",
            "保证金文件缺少必要列",
            {"required_columns": sorted(REQUIRED_COLUMNS)},
        )
    records = {}
    duplicates = set()
    rejected = 0
    for row in reader:
        exchange = EXCHANGE_NAMES.get(str(row.get("交易所名称") or "").strip())
        instrument = str(row.get("合约代码") or "").strip().lower()
        if not exchange or not instrument:
            rejected += 1
            continue
        try:
            long_percent = float(row.get("保证金-买"))
            short_percent = float(row.get("保证金-卖"))
            margin_per_lot = float(row.get("保证金-每手"))
        except (TypeError, ValueError):
            rejected += 1
            continue
        if not (
            math.isfinite(long_percent) and math.isfinite(short_percent) and
            0 < long_percent <= 100 and 0 < short_percent <= 100 and
            math.isfinite(margin_per_lot) and margin_per_lot > 0
        ):
            rejected += 1
            continue
        try:
            updated_at = _parse_local_time(row.get("手续费更新时间"))
        except (TypeError, ValueError):
            updated_at = None
        key = (exchange, instrument)
        if key in records:
            duplicates.add(key)
            continue
        records[key] = {
            "exchange": exchange,
            "instrument_id": instrument,
            "long_margin_ratio": long_percent / 100.0,
            "short_margin_ratio": short_percent / 100.0,
            "margin_per_lot": margin_per_lot,
            "source_updated_at": updated_at,
        }
    for key in duplicates:
        records.pop(key, None)
    if not records:
        raise BridgeError("MARGIN_REFERENCE_INVALID", "保证金文件没有可用的唯一合约记录")
    source_times = [
        item["source_updated_at"] for item in records.values()
        if isinstance(item["source_updated_at"], dt.datetime)
    ]
    source_time_invalid_records = len(records) - len(source_times)
    latest = max(source_times) if source_times else None
    earliest = min(source_times) if source_times else None
    return {
        "valid_records": len(records),
        "rejected_records": rejected,
        "duplicate_contracts": len(duplicates),
        "source_time_invalid_records": source_time_invalid_records,
        "latest_source_updated_at": latest.isoformat(timespec="milliseconds") if latest else None,
        "earliest_source_updated_at": earliest.isoformat(timespec="milliseconds") if earliest else None,
    }


def _adapter_margin_paths(config):
    root = os.path.abspath(os.path.join(os.path.dirname(config.path), os.pardir))
    max_ages = {}
    updated_adapter_configs = []
    default_path = os.path.join(config.data_dir, "reference", "保证金手续费.csv")
    for account in config.accounts.values():
        if not account.enabled:
            continue
        adapter_path = os.path.join(root, "pythongo_ready", account.adapter_instance, "pythongo_adapter.json")
        try:
            with open(adapter_path, "r", encoding="utf-8") as stream:
                adapter = json.load(stream)
        except (OSError, ValueError) as exc:
            raise BridgeError("MARGIN_REFERENCE_CONFIG_ERROR", "无法读取Adapter配置：%s" % exc) from exc
        changed = False
        legacy_max_age = adapter.pop("margin_reference_max_age_hours", None)
        if legacy_max_age is not None:
            changed = True
        defaults = {
            "margin_reference_file": default_path,
            "margin_reference_schema_version": MARGIN_REFERENCE_SCHEMA_VERSION,
            "margin_reference_refresh_max_age_hours": legacy_max_age if legacy_max_age is not None else 36,
            "margin_reference_source_warn_age_hours": max(168, legacy_max_age) if isinstance(legacy_max_age, int) and not isinstance(legacy_max_age, bool) else 168,
            "margin_reference_safety_multiplier": 1.25,
            "margin_reference_require_signature": True,
        }
        for name, value in defaults.items():
            if name not in adapter or (name == "margin_reference_file" and not str(adapter.get(name) or "").strip()):
                adapter[name] = value
                changed = True
        if adapter.get("margin_reference_schema_version") != MARGIN_REFERENCE_SCHEMA_VERSION:
            adapter["margin_reference_schema_version"] = MARGIN_REFERENCE_SCHEMA_VERSION
            changed = True
        if changed:
            atomic_write_json(adapter_path, adapter)
            updated_adapter_configs.append(adapter_path)
        path = str(adapter["margin_reference_file"]).strip()
        absolute = os.path.abspath(os.path.expandvars(path))
        configured_age = adapter["margin_reference_refresh_max_age_hours"]
        source_warn_age = adapter["margin_reference_source_warn_age_hours"]
        if isinstance(configured_age, bool) or not isinstance(configured_age, int) or not 1 <= configured_age <= 168:
            raise BridgeError(
                "MARGIN_REFERENCE_CONFIG_ERROR",
                "margin_reference_refresh_max_age_hours必须是1至168的整数",
            )
        if isinstance(source_warn_age, bool) or not isinstance(source_warn_age, int) or not 1 <= source_warn_age <= 8760:
            raise BridgeError(
                "MARGIN_REFERENCE_CONFIG_ERROR",
                "margin_reference_source_warn_age_hours必须是1至8760的整数",
            )
        max_ages[absolute] = min(configured_age, max_ages.get(absolute, configured_age))
    if not max_ages:
        raise BridgeError("MARGIN_REFERENCE_CONFIG_ERROR", "没有启用账户的保证金文件路径")
    return max_ages, updated_adapter_configs


def _fetch_9qihuo_csv():
    request = Request(
        FUTURES_COMM_INFO_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    try:
        with urlopen(request, timeout=FUTURES_COMM_DOWNLOAD_TIMEOUT_SECONDS) as response:
            parsed_url = urlparse(response.geturl())
            final_host = (parsed_url.hostname or "").lower()
            if parsed_url.scheme != "https" or final_host not in {"9qihuo.com", "www.9qihuo.com"}:
                raise ValueError("九期网下载发生异常跳转：%s" % final_host)
            raw_data = response.read(FUTURES_COMM_MAX_DOWNLOAD_BYTES + 1)
            if len(raw_data) > FUTURES_COMM_MAX_DOWNLOAD_BYTES:
                raise ValueError("九期网手续费页面超过大小限制")
            charset = response.headers.get_content_charset() or "utf-8"
        records = _parse_9qihuo_html(raw_data.decode(charset, errors="strict"))
        encoded = _encode_records(records)
    except Exception as exc:
        raise BridgeError(
            "NINEQIHUO_REFRESH_FAILED",
            "九期网保证金手续费刷新失败：%s" % type(exc).__name__,
        ) from exc
    return encoded, "9QIHUO_FUTURES_COMM_INFO", "desktop-heyuetbl-v1"


def _reference_is_fresh(path, keyring, max_age_hours=36):
    try:
        with open(path, "rb") as stream:
            encoded = stream.read()
        with open(path + ".meta.json", "r", encoding="utf-8") as stream:
            metadata = json.load(stream)
        keyring.verify(metadata)
        if metadata.get("schema_version") != MARGIN_REFERENCE_SCHEMA_VERSION or metadata.get("csv_sha256") != hashlib.sha256(encoded).hexdigest():
            return False
        refreshed_at = parse_time(metadata.get("refreshed_at"))
        validate_margin_csv(encoded)
        now = utc_now()
        china = dt.timezone(dt.timedelta(hours=8))
        return bool(
            refreshed_at.astimezone(china).date() == now.astimezone(china).date() and
            dt.timedelta(0) <= now - refreshed_at <= dt.timedelta(hours=max_age_hours)
        )
    except (OSError, ValueError, TypeError, BridgeError):
        return False


def refresh_margin_reference(config_path, source_csv=None, if_due=False):
    config = load_config(config_path)
    max_ages, updated_adapter_configs = _adapter_margin_paths(config)
    paths = list(max_ages)
    keyring = KeyRing.load(config.key_file)
    reset_profiles = []
    halt_result = None
    if updated_adapter_configs:
        # Activating or changing local risk-data policy changes accepted execution inputs.
        # Halt both ends first, then invalidate the old P0 attestation.
        from .worker import build_runtime

        _, _, core = build_runtime(config.path)
        halt_result = core.halt_trading("local margin reference policy changed; P0 revalidation required")
        if halt_result.get("adapter_file_failures"):
            raise BridgeError(
                "HALT_FILE_FAILED",
                "启用本地保证金回退时无法写入全部Adapter熔断文件",
                {"failures": halt_result["adapter_file_failures"]},
            )
        for adapter_path in updated_adapter_configs:
            try:
                with open(adapter_path, "r", encoding="utf-8") as stream:
                    adapter = json.load(stream)
                profile_path = os.path.abspath(os.path.expandvars(str(adapter["mapping_profile"])))
                with open(profile_path, "r", encoding="utf-8") as stream:
                    profile = json.load(stream)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise BridgeError(
                    "MARGIN_REFERENCE_CONFIG_ERROR",
                    "无法在风险逻辑升级后重置Profile：%s" % exc,
                ) from exc
            profile["verified"] = False
            profile["signature"] = ""
            atomic_write_json(profile_path, profile)
            reset_profiles.append(profile_path)
    if if_due and not source_csv and all(
        _reference_is_fresh(path, keyring, max_ages[path]) for path in paths
    ):
        return {
            "ok": True,
            "skipped": True,
            "reason": "signed local reference is valid and already refreshed today",
            "paths": paths,
            "updated_adapter_configs": updated_adapter_configs,
            "reset_profiles": reset_profiles,
            "halted_for_upgrade": bool(halt_result),
        }
    if source_csv:
        source_csv = os.path.abspath(os.path.expandvars(source_csv))
        try:
            with open(source_csv, "rb") as stream:
                encoded = stream.read()
        except OSError as exc:
            raise BridgeError("MARGIN_REFERENCE_READ_FAILED", "无法读取本地保证金文件：%s" % exc) from exc
        source = "LOCAL_CSV_IMPORT"
        source_version = None
    else:
        encoded, source, source_version = _fetch_9qihuo_csv()
    summary = validate_margin_csv(encoded)
    refreshed_at = iso_now()
    digest = hashlib.sha256(encoded).hexdigest()
    for path in paths:
        atomic_write_bytes(path, encoded)
        metadata = {
            "schema_version": MARGIN_REFERENCE_SCHEMA_VERSION,
            "csv_sha256": digest,
            "source": source,
            "source_version": source_version,
            "refreshed_at": refreshed_at,
            "key_id": keyring.active_key_id,
            "signature": "",
        }
        metadata["signature"] = keyring.sign(metadata)
        atomic_write_json(path + ".meta.json", metadata)
    return {
        "ok": True,
        "skipped": False,
        "source": source,
        "source_version": source_version,
        "refreshed_at": refreshed_at,
        "csv_sha256": digest,
        "paths": paths,
        "updated_adapter_configs": updated_adapter_configs,
        "reset_profiles": reset_profiles,
        "halted_for_upgrade": bool(halt_result),
        **summary,
    }
