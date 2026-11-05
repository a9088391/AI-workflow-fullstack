"""CQ34 固定隔離副本的共同驗收入口；建置與服務啟停由 Main 負責。

從原工作區執行此檔，cwd 必須是 .local/cq34-fixture，並設定
COMPOSE_PROJECT_NAME=cq34_test、COMPOSE_FILE=compose.yaml:.local/cq34-compose.yaml。
只支援 --guard、--self-test、--backend 固定名稱、--all、--restore 本 namespace。
pending 或身分不符時不重送；此入口不清庫、不刪測資、不啟停其他環境。
"""

import argparse
from contextlib import contextmanager
import fcntl
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

import cq18_http as recovery
from cq23_http import report_failure, reporting_self_check
import ticket57_http as shared

RECOVERY = Path(".local/cq34-recovery")
LIMITS = {"accounts": 2500, "articles": 1000, "comments": 1000, "login_sessions": 10000, "login_failures": 5000}
# 每組有限既有案例的保守預留；不在額度邊緣開跑後才發現這組無法完成。
RESERVE = {"accounts": 120, "articles": 80, "comments": 80, "login_sessions": 400, "login_failures": 120}


# 有限命令先解析，未知後端、參數及恢復路徑在 guard／服務讀取之前即拒絕。
def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--guard", action="store_true")
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--backend", choices=shared.BACKENDS)
    modes.add_argument("--all", action="store_true")
    modes.add_argument("--restore", type=Path)
    args = parser.parse_args(argv)
    if args.restore is not None and (args.restore.parent != RECOVERY or
            re.fullmatch(r"recovery-cq34[0-9a-f]{8}\.json", args.restore.name) is None):
        parser.error("--restore 只接受 .local/cq34-recovery/recovery-cq34<8hex>.json")
    return args


# private regular file 與固定目錄限定本輪恢復資料，不接受 symlink 或放寬權限的輸入。
def private_path(path, *, directory=False):
    info = path.lstat()
    shared.check(not path.is_symlink() and path.resolve() == Path.cwd() / path and info.st_uid == os.getuid() and
                 stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600) and
                 (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)), "CQ34 recovery 路徑／權限不符")


# 固定 flock 防止兩個 process 同時通過 pending 掃描；SIGKILL 釋鎖但既有 pending 仍阻止重送。
@contextmanager
def run_lock():
    path = Path(".local/cq34-run.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private_path(path)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


# 驗證完整外層及非秘密欄位，來源、cluster、volume、keys 或摘要不同都不能取得恢復資格。
def load_packet(path, identity):
    private_path(RECOVERY, directory=True)
    private_path(path)
    packet, receipt = recovery.unwrapped_packet(json.loads(path.read_text(encoding="utf-8")), identity)
    recovery.validate_packet(packet, ticket="cq34")
    shared.check(path.name == f"recovery-{packet['nonce']}.json", "CQ34 recovery 檔名不符")
    shared.check(set(receipt) == {"namespace", "identity", "times", "boundary", "uncertain", "state", "counts_before", "groups"} and
                 type(receipt["uncertain"]) is bool and receipt["state"] in ("pending", "restoring", "restored", "incomplete") and
                 (receipt["state"] in ("restored", "incomplete")) == packet["restored"] and
                 (receipt["state"] != "incomplete" or receipt["uncertain"]), "CQ34 recovery 狀態不符")
    validate_times(receipt["times"])
    if receipt["boundary"] is not None:
        shared.boundary_receipt(receipt["boundary"])
    validate_counts(receipt["counts_before"])
    shared.check(isinstance(receipt["groups"], list), "CQ34 group 記錄不符")
    for group in receipt["groups"]:
        shared.check(set(group) == {"backend", "name", "before", "after"} and group["backend"] in shared.BACKENDS and
                     group["name"] in GROUP_NAMES, "CQ34 group 欄位不符")
        validate_counts(group["before"])
        validate_counts(group["after"])
    return packet, receipt


# 只接受兩個核准表的必要原時間；型別、重複識別及時區不符皆保留而停止。
def validate_times(items):
    shared.check(isinstance(items, list), "CQ34 時間記錄型態不符")
    seen = set()
    for item in items:
        shared.check(set(item) == {"table", "row"} and item["table"] in ("accounts", "login_failures"), "CQ34 時間表不符")
        row = item["row"]
        fields = {"id", "locked_until"} if item["table"] == "accounts" else {"id", "account_id", "occurred_at"}
        shared.check(set(row) == fields and type(row["id"]) is int and row["id"] > 0, "CQ34 時間欄位不符")
        if "account_id" in row:
            shared.check(type(row["account_id"]) is int and row["account_id"] > 0, "CQ34 事件帳號不符")
        field = "locked_until" if item["table"] == "accounts" else "occurred_at"
        value = row[field]
        shared.check(value is None and field == "locked_until" or isinstance(value, str) and
                     datetime.fromisoformat(value).tzinfo is not None, "CQ34 原時間缺少時區")
        key = item["table"], row["id"]
        shared.check(key not in seen, "CQ34 原時間識別重複")
        seen.add(key)


# 每組前後只讀總數，不讀帳密／JWT；整票上限與預留不足會在下一組首寫之前阻止開始。
def counts():
    result = json.loads(shared.sql("SELECT json_build_object(" + ",".join(
        f"'{table}',(SELECT count(*) FROM {table})" for table in LIMITS) + ")"))
    validate_counts(result)
    return result


# 這些 count 是上限證據與恢復檔的一部分，拒絕 bool、負數、缺欄或已超額的資料。
def validate_counts(value):
    shared.check(set(value) == set(LIMITS) and all(type(value[name]) is int and
                 0 <= value[name] <= maximum for name, maximum in LIMITS.items()), "CQ34 合成資料已超額或 count 不符")


# 已完成歷史只供只讀 audit；必要的測試來源更版不能授予舊 baseline 任何恢復資格。
def audit_completed_history(path, identity):
    private_path(RECOVERY, directory=True)
    private_path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    recorded = value["receipt"]["identity"]
    complete = (value["baseline"].get("restored") is True and value["receipt"].get("state") == "restored" and
                value["receipt"].get("uncertain") is False and value["receipt"].get("boundary") is None)
    expected = identity
    if complete:
        # 只容許合法 SHA256 source 不同；實際 target、cluster、images、keys 與 volume 仍逐欄相同。
        shared.check(isinstance(recorded, dict) and set(recorded) == set(identity) and
                     isinstance(recorded.get("source"), str) and re.fullmatch(r"[0-9a-f]{64}", recorded["source"]) is not None and
                     all(recorded[key] == identity[key] for key in identity if key != "source"), "CQ34 歷史 audit 身分不符")
        expected = recorded
    # pending／restoring／incomplete 仍要求完整 current identity；原 schema、摘要、namespace 全部照常核對。
    packet, receipt = load_packet(path, expected)
    shared.check(packet["restored"] and receipt["state"] == "restored" and not receipt["uncertain"] and
                 receipt["boundary"] is None, "CQ34 上輪 pending／未知結果；先由 Main 核對")


# 新執行只能在所有既有記錄都完成恢復時開始；未知檔案與中斷暫存不會自動移除。
def preflight_packets(identity, restore=None):
    if not RECOVERY.exists():
        shared.check(restore is None, "CQ34 恢復目錄不存在")
        RECOVERY.mkdir(mode=0o700)
    private_path(RECOVERY, directory=True)
    found = None
    for path in sorted(RECOVERY.iterdir()):
        shared.check(re.fullmatch(r"recovery-cq34[0-9a-f]{8}\.json", path.name) is not None, "CQ34 recovery 有未知／pending 檔案")
        if path == restore:
            packet, receipt = load_packet(path, identity)
            shared.check(not packet["restored"], "CQ34 recovery 已完成，拒絕重播")
            found = packet, receipt
        else:
            audit_completed_history(path, identity)
    shared.check(restore is None or found is not None, "CQ34 指定 recovery 不存在")
    return found


# 本輪人工時間只以 owned account、精確 row ID 與固定欄位恢復，事件／合成資料仍保留。
def restore_times(packet, receipt):
    validate_times(receipt["times"])
    nonce = packet["nonce"]
    for item in receipt["times"]:
        table, row = item["table"], item["row"]
        field = "locked_until" if table == "accounts" else "occurred_at"
        value = row[field]
        literal = "NULL" if value is None else "'" + value.replace("'", "''") + "'::timestamptz"
        owner = (f"t.email LIKE 't57-{nonce}%'" if table == "accounts" else
                 f"t.account_id={row['account_id']} AND EXISTS (SELECT 1 FROM accounts a WHERE a.id=t.account_id AND a.email LIKE 't57-{nonce}%')")
        shared.check(shared.sql(f"UPDATE {table} t SET {field}={literal} WHERE t.id={row['id']} AND {owner} RETURNING t.id") ==
                     str(row["id"]), "CQ34 原時間所有權不符")
        shared.check(shared.sql(f"SELECT ({field} IS NOT DISTINCT FROM {literal})::text FROM {table} WHERE id={row['id']}") ==
                     "true", "CQ34 原時間未恢復")


# finally 與人工 restore 走同一條精確收尾；所有讀回完成才寫 restored，重播不會再次覆蓋。
def restore(packet, path, receipt):
    shared.check(not packet["restored"] and receipt["state"] in ("pending", "restoring"), "CQ34 拒絕已完成的恢復重播")
    receipt["state"] = "restoring"
    recovery.save_packet(packet, path, receipt=receipt)
    # 原 DDL context 只清有 receipt 的物件；未知殘留先拒絕，不能按名字刪除。
    shared.clear_boundary_trigger(receipt["boundary"])
    receipt["boundary"] = None
    recovery.save_packet(packet, path, receipt=receipt)
    recovery.restore_baseline(packet, path, ticket="cq34", mark_restored=False, receipt=receipt)
    restore_times(packet, receipt)
    counts()
    packet["restored"] = True
    receipt["state"] = "incomplete" if receipt["uncertain"] else "restored"
    recovery.save_packet(packet, path, receipt=receipt)
    print("INCOMPLETE CQ34 cleanup 已讀回，但遠端寫入結果未知；禁止新 run" if receipt["uncertain"] else
          "PASS CQ34 精確恢復及讀回；合成資料保留", flush=True)


# 每個 backend 都用同一組既有入口；沒有複製 suite 或依框架接受不同錯誤。
GROUPS = (shared.backend_cases, shared.contract_cases, shared.boundary_cases, shared.lockout_cases,
          shared.lockout_edge_cases, shared.database_cases, shared.extended_invalid_cases, shared.ordered_cases,
          recovery.session_cases, recovery.expiry_cases)
GROUP_NAMES = {case.__name__ for case in GROUPS} | {"readback"}


# 每組先查額度並落 pending，再執行有限案例與 count 讀回；失敗不能被後續 PASS 掩蓋。
def run_group(backend, case, nonce, packet, path, receipt, reference):
    before = counts()
    shared.check(all(before[name] + RESERVE[name] <= maximum for name, maximum in LIMITS.items()), "CQ34 本組預留額度不足，停止新寫入")
    recovery.save_packet(packet, path, receipt=receipt)
    name = case.__name__
    print(f"RUN CQ34 {backend} {name}", flush=True)
    if case is shared.extended_invalid_cases:
        result = case(backend, nonce, reference)
        shared.check(len(result) == 284, "CQ34 共同 284 invalid 覆蓋缺漏")
    elif case is recovery.session_cases:
        result = case(nonce, backend=backend)
    elif case is recovery.expiry_cases:
        result = case(nonce, packet, path, backend=backend, receipt=receipt)
    else:
        result = case(backend, nonce)
    after = counts()
    receipt["groups"].append({"backend": backend, "name": name, "before": before, "after": after})
    recovery.save_packet(packet, path, receipt=receipt)
    print("COUNT CQ34 " + backend + " " + name + " " + json.dumps({name: after[name] - before[name] for name in LIMITS}, sort_keys=True), flush=True)
    return result


# 新 baseline 在任何 API 寫入前 exclusive/fsync 落盤，單批失敗仍以 finally 恢復設定與 fixture。
def suite(backends, identity):
    preflight_packets(identity)
    before = counts()
    nonce = "cq34" + secrets.token_hex(4)
    receipt = {"namespace": "cq34", "identity": identity, "times": [], "boundary": None, "uncertain": False,
               "state": "pending", "counts_before": before, "groups": []}
    packet, path = recovery.capture_baseline(nonce, ticket="cq34", receipt=receipt, preflight=preflight_packets)
    shared.CQ34_RECOVERY = packet, path, receipt
    reference, records = {}, {}
    try:
        for backend in backends:
            for case in GROUPS:
                result = run_group(backend, case, nonce, packet, path, receipt, reference)
                if case is shared.backend_cases:
                    records[backend] = result
            # 同一組文章／留言由全部十二版讀回，來源識別與內容按原契約核對。
            recovery.readback(records[backend], backend=backend)
            actual = counts()
            receipt["groups"].append({"backend": backend, "name": "readback", "before": actual, "after": actual})
            recovery.save_packet(packet, path, receipt=receipt)
    finally:
        try:
            restore(packet, path, receipt)
        finally:
            shared.CQ34_RECOVERY = None
    after = counts()
    print("COUNT CQ34 run " + json.dumps({name: after[name] - before[name] for name in LIMITS}, sort_keys=True), flush=True)
    print(f"PASS CQ34 {len(backends)} 後端共同案例與恢復；不代表前端／啟停／全部 CQ34 驗收完成", flush=True)


# self-test 僅以暫存檔與 mock 驗證 fail-closed；不讀 keys、不接 Docker／HTTP／DB。
def self_test():
    import cq34_test
    reporting_self_check(ticket="cq34")
    cq34_test.run()


# 僅在有限 CLI 與固定 guard 都通過後執行；guard 模式完全不建立 baseline 或合成資料。
def main(argv=None):
    args = arguments(argv)
    if args.self_test:
        self_test()
        return
    shared.select_preset("cq34")
    identity = shared.guard()
    if args.guard:
        print("PASS CQ34 固定隔離目標 guard", flush=True)
        return
    with run_lock():
        if args.restore is not None:
            packet, receipt = preflight_packets(identity, args.restore)
            restore(packet, args.restore, receipt)
            return
        suite(shared.BACKENDS if args.all else (args.backend,), identity)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # 沿用既有安全 reporter 保留 cause／context 類型與位置，不輸出例外訊息或秘密內容。
        report_failure(error, ticket="cq34")
        sys.exit(1)
