"""CQ23：沿用固定 guard 與內容案例，驗證 Flask 並只收束本次 fixture。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import inspect
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import traceback

from cq10_http import changed, create_article, create_comment, login, row
import cq18_http as extra
import cq21_http as content
import ticket57_http as shared


BACKEND = "flask"
PEER = "symfony"
TICKET = "cq23"
PARTS = ("scope", "lifecycle", "input", "race", "readback")


# 固定模式與恢復檔名在 guard 前檢查，錯誤參數不觸碰任何服務。
def arguments(argv=None, *, ticket=TICKET):
    shared.check(ticket in ("cq23", "cq25"), "未核准的內容編排票號")
    recovery = extra.RECOVERIES[ticket]
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", choices=(*PARTS, "compatibility", "compatibility-input"))
    modes.add_argument("--restore", type=Path)
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--interrupt-after-fixtures", action="store_true",
                       help="建立本票 fixture 後立即退出，供 Main 驗證中斷後 --restore")
    args = parser.parse_args(argv)
    if args.restore is not None and (
        args.restore.parent != recovery
        or re.fullmatch(rf"recovery-{ticket}[0-9a-f]{{8}}\.json", args.restore.name) is None
    ):
        parser.error(f"--restore 只接受 {ticket.upper()} 固定目錄及檔名")
    return args


# 確認所有票的 pending 記錄已處理；共用檢查保留各票 validator 與原路徑限制。
def pending_recovery():
    content.pending_recovery()


# 只比對原始非秘密 baseline，不寫設定或既有會員，也不把異動覆蓋成看似恢復。
def unchanged_baseline(packet):
    settings = json.loads(shared.sql(
        "SELECT json_build_object('enabled',enabled,'window_minutes',window_minutes,"
        "'failure_threshold',failure_threshold,'lock_seconds',lock_seconds)::text "
        "FROM login_lockout_settings WHERE id=1"))
    shared.check(settings == packet["settings"], "四設定已改變，保留 pending 交 Main 查核")
    member_ids = ",".join(str(item["id"]) for item in packet["members"]) or "NULL"
    members = json.loads(shared.sql(
        "SELECT coalesce(json_agg(m ORDER BY id),'[]'::json)::text FROM "
        f"(SELECT id,failure_epoch,locked_until FROM accounts WHERE id IN ({member_ids}) AND role='member') m"))
    shared.check(members == packet["members"], "既有會員角色／epoch／locked_until 已改變")
    shared.check(not packet["account_times"] and not packet["session_times"], "內容編排不接受時間 fixture")


# 只終止本 nonce holder、降回臨時角色並撤銷其 sessions；成功核對後才標記恢復。
def recover(packet, path, *, ticket=TICKET):
    extra.validate_packet(packet, ticket=ticket)
    shared.check(not packet["account_times"] and not packet["session_times"], "內容編排不接受時間 fixture")
    nonce = packet["nonce"]
    content.cleanup_holders(nonce, ticket=ticket)
    extra.clean_boundary()
    # 與 API 相容地先鎖帳號、再撤銷 sessions；新增內容及 sequence 保留供證據讀回。
    extra.restore_sql(
        "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='15s'; "
        f"SELECT id FROM accounts WHERE email LIKE 't57-{nonce}%' ORDER BY id FOR UPDATE; "
        f"UPDATE accounts SET role='member' WHERE email LIKE 't57-{nonce}%' AND role='admin'; "
        "UPDATE login_sessions SET revoked_at=coalesce(revoked_at,clock_timestamp()) "
        f"WHERE account_id IN (SELECT id FROM accounts WHERE email LIKE 't57-{nonce}%'); COMMIT;"
    )
    shared.check(shared.sql(
        f"SELECT count(*) FROM accounts WHERE email LIKE 't57-{nonce}%' AND role='admin'"
    ) == "0" and shared.sql(
        "SELECT count(*) FROM login_sessions s JOIN accounts a ON a.id=s.account_id "
        f"WHERE a.email LIKE 't57-{nonce}%' AND s.revoked_at IS NULL"
    ) == "0", "本票 admin／sessions 未收束")
    unchanged_baseline(packet)
    extra.clean_boundary()
    packet["restored"] = True
    try:
        extra.save_packet(packet, path)
    except BaseException:
        packet["restored"] = False
        raise
    print(f"PASS {ticket.upper()} recovery {path}；存量設定／鎖定欄位未寫入", flush=True)


# 兩個建立來源各做文章／留言同版本競爭；完整同版本輸入同時送出，勝者由提交決定。
def version_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    observed = []
    for creator, peer in ((backend, PEER), (PEER, backend)):
        owner = shared.user(creator, nonce + "v" + creator, "member")
        peer_token = login(peer, owner)
        article_id = create_article(creator, owner, f"{ticket}-{nonce}-{creator}-version")
        comment_id = create_comment(creator, owner, article_id, f"{ticket}-{nonce}-{creator}-version-comment")
        for table, identifier in (("articles", article_id), ("comments", comment_id)):
            before = row(table, identifier)
            path = f"/api/{table}/{identifier}"
            bodies = [{"body": text, "version": 1} for text in ("建立端競爭😀", "另一端競爭\r\n")]
            if table == "articles":
                for body in bodies:
                    body["title"] = before["title"]
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(shared.synchronized_request, backend, "PUT", path, body, token, barrier)
                           for backend, body, token in zip((creator, peer), bodies, (owner["token"], peer_token))]
                results = [future.result(timeout=20) for future in futures]
            shared.check(sorted(result[0] for result in results) == [200, 409], "版本競爭未一成功一衝突")
            for result in results:
                shared.expect(result, result[0], "E0201" if result[0] == 409 else None)
            winner = next(index for index, result in enumerate(results) if result[0] == 200)
            committed = row(table, identifier)
            changed(before, committed, "版本競爭未精確遞增一次")
            shared.check(committed["body"] == bodies[winner]["body"]
                         and committed["created_backend"] == creator, "競爭覆寫勝者或建立來源")
            primary = shared.expect(shared.request(creator, "GET", path, token=owner["token"]), 200)
            content.resource(primary, table, identifier, owner["id"], creator)
            shared.check(primary == shared.expect(shared.request(peer, "GET", path, token=peer_token), 200),
                         "競爭提交後跨後端讀回不同")
            # 競爭後再送同一舊版本，單獨比較完整快照，確認拒絕不留下額外副作用。
            backend_token = owner["token"] if creator == backend else peer_token
            content.rejected(nonce, observed, "PUT", path, bodies[winner], backend_token, 409, "E0201",
                             backend=backend, ticket=ticket)
    shared.trace_errors(backend, observed)
    print(f"PASS {ticket.upper()} {backend}↔{PEER} 文章／留言同版本競爭", flush=True)


# 全十二後端讀取兩種 API 建立來源；每個讀回都依完整共同資源契約與 DB 核對。
def readback_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    for creator in (backend, PEER):
        actor = shared.user(creator, nonce + "b" + creator, "member")
        article_id = create_article(creator, actor, f"{ticket}-{nonce}-{creator}-all12")
        comment_id = create_comment(creator, actor, article_id, f"{ticket}-{nonce}-{creator}-all12-comment")
        for reader in shared.BACKENDS:
            token = actor["token"] if reader == creator else login(reader, actor)
            for table, identifier in (("articles", article_id), ("comments", comment_id)):
                data = shared.expect(shared.request(reader, "GET", f"/api/{table}/{identifier}", token=token), 200)
                content.resource(data, table, identifier, actor["id"], creator)
                shared.check(data["visibility"] == "visible" and data["can_restore"] is False,
                             "十二後端可見性或恢復旗標不符")
    print(f"PASS {ticket.upper()} 全十二後端讀回 {backend}／{PEER} API 建立文章及留言", flush=True)


# 只輸出例外鏈的類型與位置，保留案例及復原失敗且不洩漏訊息、來源行或區域變數。
def report_failure(error, *, ticket=TICKET):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        locations = " -> ".join(
            f"{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}"
            for frame, line in traceback.walk_tb(error.__traceback__)
        )
        print(f"FAIL {ticket.upper()} {type(error).__name__} at {locations}；核對 recovery", file=sys.stderr)
        # 明示原因優先，否則沿 finally 的 context；循環引用只輸出一次，避免診斷再卡住。
        error = error.__cause__ if error.__cause__ is not None else error.__context__


# 模擬案例與 finally 復原同時失敗，直接驗 CLI 共用輸出且不觸碰服務或真實 recovery。
def reporting_self_check(*, ticket=TICKET):
    sentinel = f"{ticket}-reporting-sentinel-secret"
    packet = {"restored": False}

    # 原案例可能是一般失敗或使用者中斷；兩者都必須保留位置而不洩漏訊息。
    def fail_case(error_type):
        raise error_type(sentinel)

    # 模擬 finally 的復原失敗；保持 pending，不能假造復原成功。
    def fail_recovery():
        raise RuntimeError(sentinel)

    for error_type in (AssertionError, KeyboardInterrupt):
        output = io.StringIO()
        try:
            try:
                fail_case(error_type)
            finally:
                fail_recovery()
        except RuntimeError as error:
            with redirect_stderr(output):
                report_failure(error, ticket=ticket)
            # 明示 cause 與循環鏈也沿同一安全輸出；只動合成例外，不碰 guard 或服務。
            error.__cause__ = error.__context__
            error.__cause__.__context__ = error
            cyclic_output = io.StringIO()
            with redirect_stderr(cyclic_output):
                report_failure(error, ticket=ticket)
            assert cyclic_output.getvalue().count(f"FAIL {ticket.upper()} ") == 2
            assert sentinel not in cyclic_output.getvalue()
        text = output.getvalue()
        assert error_type.__name__ in text and "RuntimeError" in text
        for function in ("fail_case", "fail_recovery"):
            assert re.search(r"cq23_http\.py:[1-9][0-9]*:" + function, text)
        assert sentinel not in text and packet["restored"] is False
        # 單一失敗與 KeyboardInterrupt 仍須顯示原位置，不額外產生復原成功標記。
        output = io.StringIO()
        try:
            fail_case(error_type)
        except (AssertionError, KeyboardInterrupt) as error:
            with redirect_stderr(output):
                report_failure(error, ticket=ticket)
        text = output.getvalue()
        assert error_type.__name__ in text and "fail_case" in text and "fail_recovery" not in text
        assert sentinel not in text and packet["restored"] is False


# 使用合成值驗證序列化、namespace、固定 guard 與共享函式預設，不連服務。
def self_check(*, ticket=TICKET):
    content.self_check()
    packet = {"nonce": ticket + "0123abcd", "settings": {
        "enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 300,
    }, "members": [], "account_times": [], "session_times": [], "restored": False}
    extra.validate_packet(json.loads(json.dumps(packet)), ticket=ticket)
    other_ticket = "cq25" if ticket == "cq23" else "cq23"
    for invalid in ({**packet, "nonce": "cq210123abcd"},
                    {**packet, "nonce": other_ticket + "0123abcd"}, {**packet, "extra": True}):
        try:
            extra.validate_packet(invalid, ticket=ticket)
        except AssertionError:
            continue
        raise AssertionError("內容 recovery 非法輸入未被拒絕")
    for operation in (content.scope_cases, content.lifecycle_cases, content.input_cases):
        parameters = inspect.signature(operation).parameters
        shared.check(parameters["backend"].default == "django" and parameters["ticket"].default == "cq21",
                     "共享內容案例的原預設已改變")
    for operation in (version_cases, readback_cases, main):
        parameters = inspect.signature(operation).parameters
        shared.check(parameters["backend"].default == "flask" and parameters["ticket"].default == "cq23",
                     "CQ23 原預設已改變")
    # 只解析合成 argv；錯票目錄、非法模式及互斥選項都必須在 guard 前停止。
    recovery = extra.RECOVERIES[ticket] / f"recovery-{packet['nonce']}.json"
    assert arguments(["--restore", str(recovery)], ticket=ticket).restore == recovery
    for argv in (["--regression", "unknown"], ["--unknown"], ["--self-check", "--interrupt-after-fixtures"],
                 ["--restore", str(extra.RECOVERIES["cq21"] / recovery.name)],
                 ["--restore", str(recovery.parent / "recovery-cq210123abcd.json")]):
        with redirect_stderr(io.StringIO()):
            try:
                arguments(argv, ticket=ticket)
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError("錯誤 CLI 參數未被拒絕")
    # 合成檔只存在暫存目錄；直接驗同一 pending 掃描，不改全域 guard 或 live 路徑。
    with TemporaryDirectory(prefix=f"{ticket}-pending-") as folder:
        path = Path(folder) / recovery.name
        path.write_text(json.dumps(packet), encoding="utf-8")
        try:
            content.pending_recovery(recoveries={ticket: Path(folder)})
        except AssertionError:
            pass
        else:
            raise AssertionError("pending 記錄未阻止新測資")
        path.write_text(json.dumps(packet | {"restored": True}), encoding="utf-8")
        content.pending_recovery(recoveries={ticket: Path(folder)})
        path.rename(Path(folder) / f"recovery-{ticket}ffffffff.json")
        try:
            content.pending_recovery(recoveries={ticket: Path(folder)})
        except AssertionError:
            pass
        else:
            raise AssertionError("恢復檔名與 nonce 不符仍被接受")
    reporting_self_check(ticket=ticket)
    print(f"PASS {ticket.upper()} offline namespace／CQ21 defaults／guard identity／safe failure reporting", flush=True)


# 每 run 先 durable capture，再執行薄編排；中斷模式刻意跳過 finally 供獨立 CLI 恢復。
def main(*, backend=BACKEND, ticket=TICKET):
    shared.check((backend, ticket) in (("flask", "cq23"), ("fastapi", "cq25")), "未核准的內容編排組合")
    args = arguments(ticket=ticket)
    content.guard_source()
    if args.self_check:
        self_check(ticket=ticket)
        return
    packet = None
    if args.restore is not None:
        packet = json.loads(args.restore.read_text(encoding="utf-8"))
        extra.validate_packet(packet, ticket=ticket)
        shared.check(args.restore.name == f"recovery-{packet['nonce']}.json" and not packet["restored"],
                     "恢復檔名不符或已完成")
    else:
        pending_recovery()
    shared.guard()
    if packet is not None:
        recover(packet, args.restore, ticket=ticket)
        return
    nonce = ticket + secrets.token_hex(4)
    packet, path = extra.capture_baseline(nonce, ticket=ticket)
    try:
        if args.interrupt_after_fixtures:
            actor = shared.user(backend, nonce + "interrupt", "admin")
            article_id = create_article(backend, actor, f"{ticket}-{nonce}-interrupt")
            create_comment(backend, actor, article_id, f"{ticket}-{nonce}-interrupt-comment")
            print(f"INTERRUPT {ticket.upper()} {path}；請先驗 pending 拒絕，再執行 --restore", flush=True)
            os._exit(75)
        for part in PARTS if args.regression is None else (args.regression,):
            print(f"RUN {ticket.upper()} {part}", flush=True)
            if part in ("scope", "lifecycle", "input"):
                operation = {"scope": content.scope_cases, "lifecycle": content.lifecycle_cases,
                             "input": content.input_cases}[part]
                operation(nonce, backend=backend, ticket=ticket)
            elif part == "race":
                version_cases(nonce, backend=backend, ticket=ticket)
                content.race_cases(nonce, backend=backend)
            elif part in ("compatibility", "compatibility-input"):
                # 省略 backend 真正執行原 Django 預設；新資料仍屬目前票的 recovery 範圍。
                operation = content.scope_cases if part == "compatibility" else content.input_cases
                operation(nonce, ticket=ticket)
            else:
                readback_cases(nonce, backend=backend, ticket=ticket)
    finally:
        recover(packet, path, ticket=ticket)
    print(f"PASS {ticket.upper()} {backend} 內容公開 HTTP／DB suite" if args.regression is None
          else f"PASS {ticket.upper()} partial {args.regression}；其他案例未執行", flush=True)
    print("限制：未驗瀏覽器、搜尋U+0000、其他十一後端完整寫入矩陣；compatibility須另跑", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        report_failure(error)
        sys.exit(1)
