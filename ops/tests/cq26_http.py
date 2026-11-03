"""CQ26：明示 Gin 目標，重用帳號／內容案例與已知邊界 DDL 恢復。"""

import argparse
from contextlib import redirect_stderr
import inspect
import io
import json
from pathlib import Path
import re
import secrets
import sys
from tempfile import TemporaryDirectory

import cq18_http as extra
import cq21_http as content
import cq22_http as account
import cq23_http as cross
import cq24_http as recovery
import ticket57_http as shared


BACKEND = "gin"
TICKET = "cq26"
RECOVERY = extra.RECOVERIES[TICKET]
PARTS = ("account", "content", "query", "settings", "session", "account-race", "scope", "lifecycle", "input",
         "race", "readback", "compatibility-flask", "compatibility-laravel")


# CLI 固定本票路徑與有限案例；錯票、互斥模式及未知參數都在 guard 前拒絕。
def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", choices=PARTS)
    modes.add_argument("--restore", type=Path)
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--interrupt-at-boundary", action="store_true",
                       help="建立已知邊界 DDL 後真退出 75；以新程序 --restore 收束")
    args = parser.parse_args(argv)
    if args.restore is not None and (
        args.restore.parent != RECOVERY
        or re.fullmatch(r"recovery-cq26[0-9a-f]{8}\.json", args.restore.name) is None
    ):
        parser.error("--restore 只接受 CQ26 固定目錄與檔名")
    return args


# 重用安全 renderer 的雙鏈遍歷；只補本入口與內容 driver 的已知 code object。
def report_failure(error):
    recovery.report_failure(error, ticket=TICKET, modules=(
        (sys.modules[__name__], "cq26_http.py"), (content, "cq21_http.py"), (cross, "cq23_http.py"),
    ))


# 每個 phase 只用自己的 nonce；帳號與內容既有的 s／l／i marker 相同，不能共用同一組帳號。
def run_cases(nonce, packet, path, part):
    shared.check(part in PARTS, "未核准的 CQ26 phase")
    if part in ("account", "query", "settings", "session", "account-race"):
        regression = "race" if part == "account-race" else None if part == "account" else part
        account.run_cases(nonce, packet, path, backend=BACKEND, ticket=TICKET, regression=regression)
    operations = {"scope": content.scope_cases, "lifecycle": content.lifecycle_cases,
                  "input": content.input_cases, "readback": cross.readback_cases}
    for name, operation in operations.items():
        if part in ("content", name):
            print(f"RUN CQ26 content {name}", flush=True)
            operation(nonce, backend=BACKEND, ticket=TICKET)
    if part in ("content", "race"):
        print("RUN CQ26 content version／parent lock chain", flush=True)
        cross.version_cases(nonce, backend=BACKEND, ticket=TICKET)
        content.race_cases(nonce, backend=BACKEND)
    if part == "compatibility-flask":
        # 真的省略 backend，驗既有 Flask 預設；fixture 仍歸本 phase 的 CQ26 nonce。
        account.run_cases(nonce, packet, path, ticket=TICKET, regression="query")
    elif part == "compatibility-laravel":
        extra.regression_cases(nonce)


# 離線檢查使用真 validator／renderer；暫存 recovery 不在 live 路徑，沒有 HTTP、Docker 或 DB。
def self_check():
    recovery.self_check(backend=BACKEND, ticket=TICKET)
    for operation in (account.run_cases, account.query_cases, cross.version_cases, cross.readback_cases):
        parameters = inspect.signature(operation).parameters
        assert parameters["backend"].default == "flask"
        assert parameters["ticket"].default == ("cq22" if operation.__module__ == account.__name__ else "cq23")
    assert inspect.signature(extra.regression_cases).parameters["backend"].default == "laravel"
    assert inspect.signature(recovery.interrupt_at_boundary).parameters["backend"].default == "fastapi"
    assert inspect.signature(recovery.restore_baseline).parameters["ticket"].default == "cq24"
    packet = {"nonce": "cq260123abcd", "settings": {
        "enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 300,
    }, "members": [], "account_times": [], "session_times": [], "restored": False}
    restore_path = RECOVERY / f"recovery-{packet['nonce']}.json"
    assert arguments(["--restore", str(restore_path)]).restore == restore_path
    assert arguments(["--interrupt-at-boundary"]).interrupt_at_boundary
    assert arguments(["--regression", "content"]).regression == "content"
    assert arguments([]).regression is None
    try:
        run_cases(None, None, None, "unknown")
    except AssertionError:
        pass
    else:
        raise AssertionError("未知 phase 沒有在服務操作前停止")
    # 直接驗新入口與 CQ24 原入口的 namespace 隔離，無效 CLI 不進服務 guard。
    for argv in (["--regression", "unknown"], ["--unknown"], ["--self-check", "--interrupt-at-boundary"],
                 ["--restore", str(extra.RECOVERIES["cq24"] / restore_path.name)],
                 ["--restore", str(RECOVERY / "recovery-cq240123abcd.json")]):
        with redirect_stderr(io.StringIO()):
            try:
                arguments(argv)
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError("錯誤 CQ26 CLI 未拒絕")
    with redirect_stderr(io.StringIO()):
        try:
            recovery.arguments(["--restore", str(restore_path)])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("CQ24 原 CLI 接受了 CQ26 恢復檔")
    # 唯一 pending 可恢復；兩次新 run、跨票 pending、錯票及 replay 均須在 DB 前拒絕。
    with TemporaryDirectory(prefix="cq26-pending-") as folder:
        folders = {ticket: Path(folder) / ticket for ticket in ("cq24", TICKET)}
        for directory in folders.values():
            directory.mkdir()
        path = folders[TICKET] / restore_path.name
        path.write_text(json.dumps(packet), encoding="utf-8")
        recovery.validate_restore(packet, path, backend=BACKEND, ticket=TICKET, recoveries=folders)
        for _ in range(2):
            try:
                content.pending_recovery(recoveries=folders)
            except AssertionError:
                pass
            else:
                raise AssertionError("pending 未阻止新 run")
        other = folders["cq24"] / "recovery-cq240123abcd.json"
        other.write_text(json.dumps(packet | {"nonce": "cq240123abcd"}), encoding="utf-8")
        for candidate, target in ((packet, path), (packet | {"restored": True}, path),
                                  (packet | {"nonce": "cq240123abcd"}, path)):
            try:
                recovery.validate_restore(candidate, target, backend=BACKEND, ticket=TICKET, recoveries=folders)
            except AssertionError:
                pass
            else:
                raise AssertionError("跨票 pending／replay／錯票恢復未拒絕")
        other.unlink()
        path.write_text(json.dumps(packet | {"restored": True}), encoding="utf-8")
        content.pending_recovery(recoveries=folders)
    # 真實 finally 雙失敗加 cause 循環，不洩訊息，且兩個原始失敗位置都可追溯。
    for kind in (AssertionError, KeyboardInterrupt):
        output = io.StringIO()
        try:
            try:
                raise kind("cq26-secret-sentinel")
            finally:
                raise RuntimeError("cq26-secret-sentinel")
        except RuntimeError as error:
            error.__cause__ = ValueError("cq26-secret-sentinel")
            error.__context__.__context__ = error
            with redirect_stderr(output):
                report_failure(error)
        text = output.getvalue()
        assert text.count("FAIL CQ26 ") == 3 and "cq26-secret-sentinel" not in text
        assert kind.__name__ in text and "RuntimeError" in text and "ValueError" in text
        assert "cq26_http.py:" in text
    # 標準函式庫位置不在可信 driver 清單，不能把外部 filename 或解析輸入寫入診斷。
    output = io.StringIO()
    try:
        json.loads("cq26-secret-sentinel")
    except json.JSONDecodeError as error:
        with redirect_stderr(output):
            report_failure(error)
    assert "<external>" in output.getvalue() and "decoder.py" not in output.getvalue()
    assert "cq26-secret-sentinel" not in output.getvalue()
    print("PASS CQ26 offline defaults／CLI／unique pending／replay／safe failure", flush=True)


# 完整模式依序建立兩份 baseline，帳號 phase 恢復成功才跑內容；避免既有 marker 衝突且不疊 pending。
def main(argv=None):
    args = arguments(argv)
    content.guard_source()
    if args.self_check:
        self_check()
        return
    if args.interrupt_at_boundary:
        recovery.interrupt_at_boundary(backend=BACKEND, ticket=TICKET)
        return
    packet = None
    if args.restore is not None:
        packet = json.loads(args.restore.read_text(encoding="utf-8"))
        recovery.validate_restore(packet, args.restore, backend=BACKEND, ticket=TICKET)
    if packet is not None:
        # 若程序在 DB stop 期間被硬中斷，Main 須先以固定 Compose 啟回原 db；此入口不繞過 guard。
        shared.guard()
        recovery.restore_baseline(packet, args.restore, backend=BACKEND, ticket=TICKET)
        return
    for part in ("account", "content") if args.regression is None else (args.regression,):
        content.pending_recovery()
        shared.guard()
        nonce = TICKET + secrets.token_hex(4)
        packet, path = extra.capture_baseline(nonce, ticket=TICKET)
        try:
            print(f"RUN CQ26 phase {part} nonce {nonce}", flush=True)
            run_cases(nonce, packet, path, part)
        finally:
            recovery.restore_baseline(packet, path, backend=BACKEND, ticket=TICKET)
    print("PASS CQ26 Gin 帳號及內容公開 HTTP／DB full suite" if args.regression is None
          else f"PASS CQ26 partial {args.regression}；其餘案例未執行", flush=True)
    print("限制：未驗瀏覽器、搜尋 U+0000；原 Flask／Laravel 預設相容性須另跑", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        report_failure(error)
        sys.exit(1)
