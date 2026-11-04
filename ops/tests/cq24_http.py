"""CQ24：重用帳號 HTTP 案例，補上 FastAPI 精確邊界中斷與具名恢復。"""

import argparse
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import traceback
from types import CodeType, FunctionType

import cq10_http
import cq18_http as extra
import cq21_http as content
import cq22_http as suite
import ticket40_http
import ticket57_http as shared


BACKEND = "fastapi"
TICKET = "cq24"
RECOVERY = extra.RECOVERIES[TICKET]
# 與既有 shared exact_boundary_cases 相同的固定函式本文；只允許四個非秘密整數不同。
BOUNDARY_BODY = (
    " BEGIN IF NEW.account_id={member_id} AND NEW.failure_epoch={epoch} THEN "
    "UPDATE public.login_failures SET occurred_at=NEW.occurred_at-interval '1 minute' "
    "+interval '{microseconds} microseconds' WHERE id={first_id} "
    "AND account_id=NEW.account_id AND failure_epoch=NEW.failure_epoch; "
    "IF NOT FOUND THEN RAISE EXCEPTION 'ticket57 boundary fixture missing'; END IF; "
    "END IF; RETURN NEW; END; "
)
BOUNDARY_PATTERN = re.escape(BOUNDARY_BODY)
for field in ("member_id", "epoch", "microseconds", "first_id"):
    BOUNDARY_PATTERN = BOUNDARY_PATTERN.replace(re.escape("{" + field + "}"), rf"(?P<{field}>[0-9]+)")


# 模式互斥且先核固定恢復路徑，任何參數錯誤都不得呼叫 guard 或服務。
def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", choices=("query", "settings", "session", "race"))
    modes.add_argument("--restore", type=Path)
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--interrupt-at-boundary", action="store_true",
                       help="獨立故障 fixture 建立 shared 同型 DDL 後退出 75，留下 pending 供新程序恢復")
    args = parser.parse_args(argv)
    if args.restore is not None and (
        args.restore.parent != RECOVERY
        or re.fullmatch(r"recovery-cq24[0-9a-f]{8}\.json", args.restore.name) is None
    ):
        parser.error("--restore 只接受 CQ24 固定目錄與檔名")
    return args


# 只接受已核准的固定 DDL 與唯一 trigger；未知來源、依賴或 namespace 均保留待 Main 查明。
def boundary_identity(catalog, nonce, *, ticket=TICKET):
    shared.check(re.fullmatch(extra.NONCES[ticket], nonce) is not None, "邊界 nonce 不符")
    function, triggers = catalog["function"], catalog["triggers"]
    if function is None and not triggers:
        shared.check(not catalog["dependents"], "沒有 function 卻有未知相依物件")
        return None
    shared.check(function is not None and len(triggers) == 1, "邊界 DDL 不完整或 trigger 不唯一")
    # 驗證權限、語言與執行屬性後才解讀本文，不能讓同名的其他函式取得刪除資格。
    expected = {
        "namespace": "public", "name": "ticket57_http_boundary_shift", "owner": "articles_owner",
        "language": "plpgsql", "kind": "f", "returns": "trigger", "arguments": "",
        "security_definer": True, "strict": False, "returns_set": False, "leakproof": False,
        "volatility": "v", "parallel": "u", "cost": 100, "rows": 0,
        "config": ["search_path=pg_catalog, public"], "acl": None,
    }
    shared.check(all(function.get(key) == value for key, value in expected.items()), "邊界 function 定義或 owner 不符")
    match = re.fullmatch(BOUNDARY_PATTERN, function["source"])
    shared.check(match is not None, "邊界 function 本文不是已知固定模板")
    values = {key: int(value) for key, value in match.groupdict().items()}
    shared.check(values["microseconds"] in (0, 1) and values["epoch"] >= 0
                 and 1 <= values["member_id"] <= 9007199254740991 and values["first_id"] > 0
                 and BOUNDARY_BODY.format(**values) == function["source"], "邊界模板整數不符")
    # INSERT BEFORE ROW、沒有額外條件或參數，且只有此 trigger 依賴該 function。
    trigger = triggers[0]
    shared.check(trigger == {
        "oid": trigger["oid"], "function_oid": function["oid"], "namespace": "public",
        "table": "login_failures", "name": "ticket57_http_boundary_shift", "type": 7,
        "enabled": "O", "internal": False, "arguments": "", "condition": None,
        "definition": "CREATE TRIGGER ticket57_http_boundary_shift BEFORE INSERT ON public.login_failures "
                      "FOR EACH ROW EXECUTE FUNCTION ticket57_http_boundary_shift()",
    }, "邊界 trigger 定義不符")
    shared.check(catalog["dependents"] == [{"class": "pg_trigger", "oid": trigger["oid"], "subid": 0, "type": "n"}],
                 "邊界 function 有未知相依物件")
    return values


# 唯讀取得實際 catalog；不能只靠物件名稱決定能否 DROP SECURITY DEFINER。
def boundary_catalog():
    return json.loads(shared.sql(
        "SELECT json_build_object('function',(SELECT json_build_object("
        "'oid',p.oid,'namespace',n.nspname,'name',p.proname,'owner',pg_get_userbyid(p.proowner),"
        "'language',l.lanname,'kind',p.prokind,'returns',p.prorettype::regtype::text,"
        "'arguments',pg_get_function_arguments(p.oid),'security_definer',p.prosecdef,'strict',p.proisstrict,"
        "'returns_set',p.proretset,'leakproof',p.proleakproof,'volatility',p.provolatile,'parallel',p.proparallel,"
        "'cost',p.procost,'rows',p.prorows,'config',p.proconfig,'acl',p.proacl,'source',p.prosrc,"
        "'definition',pg_get_functiondef(p.oid)) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "JOIN pg_language l ON l.oid=p.prolang WHERE p.oid=to_regprocedure('public.ticket57_http_boundary_shift()')),"
        "'triggers',(SELECT coalesce(json_agg(json_build_object('oid',t.oid,'function_oid',t.tgfoid,"
        "'namespace',n.nspname,'table',c.relname,'name',t.tgname,'type',t.tgtype,'enabled',t.tgenabled,"
        "'internal',t.tgisinternal,'arguments',encode(t.tgargs,'hex'),'condition',t.tgqual::text,"
        "'definition',pg_get_triggerdef(t.oid)) ORDER BY t.oid),'[]'::json) "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE (t.tgrelid='public.login_failures'::regclass AND t.tgname='ticket57_http_boundary_shift') "
        "OR t.tgfoid=to_regprocedure('public.ticket57_http_boundary_shift()')),"
        "'dependents',(SELECT coalesce(json_agg(json_build_object('class',d.classid::regclass::text,"
        "'oid',d.objid,'subid',d.objsubid,'type',d.deptype) ORDER BY d.classid,d.objid),'[]'::json) "
        "FROM pg_depend d WHERE d.refclassid='pg_proc'::regclass "
        "AND d.refobjid=to_regprocedure('public.ticket57_http_boundary_shift()')))::text"
    ))


# 先預檢帳號／事件所有權，再於取得目標表鎖的同一交易重驗並刪已知 DDL；呼叫端先核唯一 pending。
def cleanup_boundary(packet, *, backend=BACKEND, ticket=TICKET):
    catalog = boundary_catalog()
    values = boundary_identity(catalog, packet["nonce"], ticket=ticket)
    if values is None:
        return
    email = f"t57-{packet['nonce']}x{values['microseconds']}-{backend}-member@example.test"
    ownership = (
        "SELECT count(*) FROM accounts a JOIN login_failures f ON f.account_id=a.id "
        f"WHERE a.id={values['member_id']} AND a.email='{email}' AND a.role='member' "
        f"AND a.failure_epoch={values['epoch']} AND f.id={values['first_id']} AND f.failure_epoch={values['epoch']}"
    )
    shared.check(shared.sql(ownership) == "1", "邊界帳號／世代／第一筆事件不屬於本 nonce")
    function, trigger = catalog["function"], catalog["triggers"][0]
    definition = function["definition"].replace("'", "''")
    trigger_definition = trigger["definition"].replace("'", "''")
    extra.restore_sql(
        "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='15s'; "
        "LOCK TABLE public.login_failures IN ACCESS EXCLUSIVE MODE; DO $cq24$ BEGIN "
        f"IF pg_get_functiondef({function['oid']}) IS DISTINCT FROM '{definition}' "
        f"OR pg_get_triggerdef({trigger['oid']}) IS DISTINCT FROM '{trigger_definition}' "
        f"OR (SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid={function['oid']}) "
        "IS DISTINCT FROM 'articles_owner' THEN RAISE EXCEPTION 'CQ24 boundary changed'; END IF; "
        # 等待表鎖期間所有權可能改變；同交易重驗失敗即回滾，保留 DDL 與 pending 供查明。
        f"IF ({ownership}) <> 1 THEN RAISE EXCEPTION 'CQ24 boundary ownership changed'; END IF; "
        "DROP TRIGGER ticket57_http_boundary_shift ON public.login_failures; "
        "DROP FUNCTION public.ticket57_http_boundary_shift() RESTRICT; END $cq24$; COMMIT;"
    )
    extra.clean_boundary()


# 先離線核對唯一 pending 與固定目錄；測試可傳暫存目錄，不必模擬 DB 或偷換全域值。
def validate_restore(packet, path, *, backend=BACKEND, ticket=TICKET, recoveries=extra.RECOVERIES):
    shared.check((backend, ticket) in (("fastapi", "cq24"), ("gin", "cq26"), ("echo", "cq27"), ("fiber", "cq28"), ("express", "cq29"), ("fastify", "cq30"), ("nestjs", "cq31"))
                 and path.parent == recoveries[ticket]
                 and path.name == f"recovery-{packet['nonce']}.json" and not packet["restored"], "邊界恢復路徑或狀態不符")
    extra.validate_packet(packet, ticket=ticket)
    # 任何其他 pending 都必須先處理，不能讓不同 run 的恢復互相覆蓋。
    for other_ticket, folder in recoveries.items():
        for other in folder.glob(f"recovery-{other_ticket}*.json"):
            candidate = json.loads(other.read_text(encoding="utf-8"))
            extra.validate_packet(candidate, ticket=other_ticket)
            shared.check(other.name == f"recovery-{candidate['nonce']}.json"
                         and (other == path or candidate["restored"]), "不是唯一 pending recovery，拒絕恢復")


# 先收束本票已知 holder 與邊界 DDL，再用既有交易恢復四設定、存量鎖欄及本票時間。
def restore_baseline(packet, path, *, backend=BACKEND, ticket=TICKET):
    validate_restore(packet, path, backend=backend, ticket=ticket)
    # 只終止本 nonce 與固定 mode 命名的 owner holder，不碰應用連線或其他票。
    names = ",".join(f"'{packet['nonce']}-{mode}'" for mode in ("settings", "disable-first", "login-first"))
    shared.sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() "
               f"AND usename='articles_owner' AND application_name IN ({names}) AND pid<>pg_backend_pid()")
    # 只有含內容 phase 的有限編排票需要額外收束既有 cq10 holder。
    if ticket in ("cq26", "cq27", "cq28", "cq29", "cq30", "cq31"):
        content.cleanup_holders(packet["nonce"], ticket=ticket)
    cleanup_boundary(packet, backend=backend, ticket=ticket)
    extra.restore_baseline(packet, path, ticket=ticket)


# 建立與 shared 精確左界中斷相同的持久狀態；這是獨立故障 fixture，沒有攔截 shared 函式。
def interrupt_at_boundary(*, backend=BACKEND, ticket=TICKET):
    shared.check((backend, ticket) in (("fastapi", "cq24"), ("gin", "cq26"), ("echo", "cq27"), ("fiber", "cq28"), ("express", "cq29"), ("fastify", "cq30"), ("nestjs", "cq31")), "未核准的邊界 fixture 組合")
    suite.pending_recovery()
    shared.guard()
    nonce = ticket + secrets.token_hex(4)
    packet, path = extra.capture_baseline(nonce, ticket=ticket)
    try:
        admin = shared.user(backend, nonce + "e", "admin")
        member = shared.user(backend, nonce + "x0", "member")
        settings = {"enabled": True, "window_minutes": 1, "failure_threshold": 2, "lock_seconds": 30}
        shared.expect(shared.request(backend, "PUT", extra.SETTINGS, settings, admin["token"]), 200)
        epoch = int(shared.sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}"))
        shared.expect(shared.request(backend, "POST", extra.LOGIN,
                                    {"email": member["email"], "password": "Wrong1234"}), 401, "E0101")
        first_id = shared.sql(f"SELECT id FROM login_failures WHERE account_id={member['id']} AND failure_epoch={epoch}")
        shared.check(first_id.isdecimal(), "故障 fixture 第一筆事件不唯一")
        extra.clean_boundary()
        source = BOUNDARY_BODY.format(member_id=member["id"], epoch=epoch, first_id=int(first_id), microseconds=0)
        shared.sql("CREATE FUNCTION public.ticket57_http_boundary_shift() RETURNS trigger "
                   "LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$" + source + "$$; "
                   "CREATE TRIGGER ticket57_http_boundary_shift BEFORE INSERT ON public.login_failures "
                   "FOR EACH ROW EXECUTE FUNCTION public.ticket57_http_boundary_shift()")
        boundary_identity(boundary_catalog(), nonce, ticket=ticket)
        print(f"INTERRUPT {ticket.upper()} independent boundary fixture；pending {path}", flush=True)
        # 刻意略過 finally，留下已落盤的 pending 與持久 DDL，供新程序以 --restore 驗證恢復。
        os._exit(75)
    finally:
        # 一般例外（含 KeyboardInterrupt）仍立即嘗試恢復；失敗則保留 pending，交 Main／CLI 處理。
        restore_baseline(packet, path, backend=backend, ticket=ticket)


# 只從已知 driver 模組收集 code object 與靜態名稱，例外中不可信的 filename／函式名不直接輸出。
def trusted_locations(*, modules=()):
    locations = {}
    for module, filename in ((sys.modules[__name__], "cq24_http.py"), (suite, "cq22_http.py"),
                             (extra, "cq18_http.py"), (shared, "ticket57_http.py"),
                             (cq10_http, "cq10_http.py"), (ticket40_http, "ticket40_http.py"), *modules):
        codes = [value.__code__ for value in vars(module).values()
                 if isinstance(value, FunctionType) and value.__module__ == module.__name__]
        while codes:
            code = codes.pop()
            locations[code] = (filename, code.co_name)
            codes.extend(value for value in code.co_consts if isinstance(value, CodeType))
    return locations


# 案例與復原可能同時失敗；遍歷 cause/context 且防循環，只輸出可信位置及固定例外類型。
def report_failure(error, *, ticket=TICKET, modules=()):
    locations, seen, pending = trusted_locations(modules=modules), set(), [error]
    types = {kind: kind.__name__ for kind in (AssertionError, RuntimeError, KeyboardInterrupt, ValueError,
             TypeError, KeyError, OSError, TimeoutError, json.JSONDecodeError,
             subprocess.CalledProcessError, subprocess.TimeoutExpired)}
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        frames = []
        for frame, line in traceback.walk_tb(current.__traceback__):
            location = locations.get(frame.f_code)
            frames.append(f"{location[0]}:{line}:{location[1]}" if location else "<external>")
        location = " -> ".join(frames) or "<no traceback>"
        print(f"FAIL {ticket.upper()} {types.get(type(current), 'Exception')} at {location}；核對 RUN／recovery", file=sys.stderr)
        for linked in (current.__context__, current.__cause__):
            if linked is not None:
                pending.append(linked)


# 離線用真 validator 核 DDL 拒絕、參數與雙重失敗診斷；沒有 guard、HTTP、Docker 或 DB 呼叫。
def self_check(*, backend=BACKEND, ticket=TICKET):
    suite.self_check(backend=backend, ticket=ticket)
    raw = json.dumps({"nickname": "😀\r\n\0"}).encode()
    assert [ord(char) for char in json.loads(raw)["nickname"]] == [0x1F600, 13, 10, 0]
    source = BOUNDARY_BODY.format(member_id=7, epoch=2, first_id=11, microseconds=0)
    function = {"oid": 90, "namespace": "public", "name": "ticket57_http_boundary_shift", "owner": "articles_owner",
                "language": "plpgsql", "kind": "f", "returns": "trigger", "arguments": "", "security_definer": True,
                "strict": False, "returns_set": False, "leakproof": False, "volatility": "v", "parallel": "u",
                "cost": 100, "rows": 0, "config": ["search_path=pg_catalog, public"], "acl": None, "source": source}
    trigger = {"oid": 91, "function_oid": 90, "namespace": "public", "table": "login_failures",
               "name": "ticket57_http_boundary_shift", "type": 7, "enabled": "O", "internal": False,
               "arguments": "", "condition": None,
               "definition": "CREATE TRIGGER ticket57_http_boundary_shift BEFORE INSERT ON public.login_failures "
                             "FOR EACH ROW EXECUTE FUNCTION ticket57_http_boundary_shift()"}
    catalog = {"function": function, "triggers": [trigger],
               "dependents": [{"class": "pg_trigger", "oid": 91, "subid": 0, "type": "n"}]}
    assert boundary_identity(catalog, ticket + "0123abcd", ticket=ticket) == {
        "member_id": 7, "epoch": 2, "first_id": 11, "microseconds": 0,
    }
    assert boundary_identity({"function": None, "triggers": [], "dependents": []}, ticket + "0123abcd", ticket=ticket) is None
    for changed in ({**catalog, "function": {**function, "owner": "unknown"}},
                    {**catalog, "function": {**function, "source": source + " "}},
                    {**catalog, "function": {**function, "security_definer": False}},
                    {**catalog, "triggers": [{**trigger, "enabled": "D"}]},
                    {**catalog, "dependents": []}):
        try:
            boundary_identity(changed, ticket + "0123abcd", ticket=ticket)
        except AssertionError:
            continue
        raise AssertionError("未知 DDL 未被拒絕")
    # 真實兩條例外鏈使用同一 CLI renderer，訊息含秘密哨兵亦不得出現在輸出。
    for failure in (AssertionError, KeyboardInterrupt):
        output = io.StringIO()
        try:
            try:
                raise failure("cq24-secret-sentinel")
            finally:
                raise RuntimeError("cq24-secret-sentinel")
        except RuntimeError as error:
            error.__cause__ = ValueError("cq24-secret-sentinel")
            error.__context__.__context__ = error
            with redirect_stderr(output):
                report_failure(error, ticket=ticket)
        text = output.getvalue()
        assert text.count(f"FAIL {ticket.upper()} ") == 3
        assert "RuntimeError" in text and "ValueError" in text and failure.__name__ in text
        assert "cq24_http.py:" in text and "cq24-secret-sentinel" not in text
    assert arguments(["--regression", "query"]).regression == "query"
    assert arguments(["--interrupt-at-boundary"]).interrupt_at_boundary
    print(f"PASS {ticket.upper()} offline known-DDL rejection／safe chained failure／CLI", flush=True)


# 只有本票入口選 FastAPI/CQ24；生命週期固定走本票恢復，再重用原票的公開案例編排。
def main(argv=None):
    args = arguments(argv)
    shared.check(hashlib.sha256(Path(shared.__file__).read_bytes()).hexdigest() == suite.GUARD_SHA, "Ticket57 guard 來源不同")
    if args.self_check:
        self_check()
        return
    elif args.interrupt_at_boundary:
        interrupt_at_boundary()
        return
    packet = None
    if args.restore is not None:
        packet = json.loads(args.restore.read_text(encoding="utf-8"))
        extra.validate_packet(packet, ticket=TICKET)
        shared.check(args.restore.name == f"recovery-{packet['nonce']}.json" and not packet["restored"],
                     "恢復檔名不符或已恢復，拒絕覆蓋 baseline")
    else:
        suite.pending_recovery()
    shared.guard()
    if packet is not None:
        restore_baseline(packet, args.restore)
        return
    nonce = TICKET + secrets.token_hex(4)
    packet, path = extra.capture_baseline(nonce, ticket=TICKET)
    try:
        suite.run_cases(nonce, packet, path, backend=BACKEND, ticket=TICKET, regression=args.regression)
    finally:
        restore_baseline(packet, path)
    print("PASS CQ24 " + (args.regression or "FastAPI 公開 HTTP／DB full suite"), flush=True)
    print("限制：保留新增 fixture／logs／sequences；搜尋 NUL、UI 及其他十一版完整矩陣未驗", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        report_failure(error)
        sys.exit(1)
