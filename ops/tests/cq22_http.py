"""CQ22：沿用固定隔離 guard，驗證 Flask 帳號／會員／鎖定 HTTP 與復原。"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from cq10_http import wait_line
import cq18_http as extra
import ticket57_http as shared


BACKEND = "flask"
PEER = "symfony"
TICKET = "cq22"
RECOVERY = extra.RECOVERIES[TICKET]
GUARD_SHA = "51391fd2dc6ec86e51f6dd7d6ae6ecaa1e3fc0dea69b18c463b563929233fb89"


# 固定模式及本票檔名在 guard 前驗證，避免錯誤參數觸碰服務或別票 baseline。
def arguments(argv=None, *, ticket=TICKET):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--regression", choices=("query", "settings", "session", "race"))
    modes.add_argument("--restore", type=Path)
    modes.add_argument("--self-check", action="store_true", help="只核對序列化及恢復格式，不連服務")
    args = parser.parse_args(argv)
    if args.restore is not None and (
        args.restore.parent != extra.RECOVERIES[ticket]
        or re.fullmatch(rf"recovery-{ticket}[0-9a-f]{{8}}\.json", args.restore.name) is None
    ):
        parser.error(f"--restore 只接受 {ticket.upper()} 固定目錄與檔名")
    return args


# 任何前票未復原都停止新 run，避免用新 baseline 蓋掉尚未處理的副作用。
def pending_recovery():
    for ticket, folder in extra.RECOVERIES.items():
        for path in folder.glob(f"recovery-{ticket}*.json"):
            packet = json.loads(path.read_text(encoding="utf-8"))
            extra.validate_packet(packet, ticket=ticket)
            shared.check(path.name == f"recovery-{packet['nonce']}.json" and packet["restored"],
                         "存在未復原或檔名不符的 recovery，拒絕建立新 fixture")


# 以真正序列化與 validator 驗合法／非法封包，離線檢查不會呼叫 guard 或寫 DB。
def self_check(*, backend=BACKEND, ticket=TICKET):
    packet = {"nonce": ticket + "0123abcd", "settings": {
        "enabled": True, "window_minutes": 5, "failure_threshold": 5, "lock_seconds": 300,
    }, "members": [], "account_times": [], "session_times": [], "restored": False}
    extra.validate_packet(json.loads(json.dumps(packet)), ticket=ticket)
    for changed in ({**packet, "nonce": "cq210123abcd"}, {**packet, "extra": True},
                    {**packet, "settings": {**packet["settings"], "window_minutes": True}}):
        try:
            extra.validate_packet(changed, ticket=ticket)
        except AssertionError:
            continue
        raise AssertionError("無效 recovery 未被拒絕")
    raw = json.dumps({"nickname": "😀\r\n\0"}).encode()
    shared.check(json.loads(raw)["nickname"] == "😀\r\n\0", "原始特殊字元序列化不符")
    shared.check(len(f"t57-{packet['nonce']}r-{backend}-member") <= 64, "fixture Email local part 超界")
    print(f"PASS {ticket.upper()} offline serialization／recovery rejection", flush=True)


# 四設定與本票帳號完整快照僅留記憶體，拒絕與同值操作不能隱藏部分更新。
def snapshot(nonce, *, ticket=TICKET):
    shared.check(re.fullmatch(extra.NONCES[ticket], nonce) is not None, "帳號案例 marker 不符")
    ids = f"SELECT id FROM accounts WHERE email LIKE 't57-{nonce}%'"
    return shared.sql(
        "SELECT jsonb_build_object('settings',(SELECT to_jsonb(s) FROM login_lockout_settings s WHERE id=1),"
        f"'accounts',(SELECT jsonb_agg(a ORDER BY a.id) FROM accounts a WHERE a.id IN ({ids})),"
        f"'sessions',(SELECT jsonb_agg(s ORDER BY s.jti) FROM login_sessions s WHERE s.account_id IN ({ids})),"
        f"'failures',(SELECT jsonb_agg(f ORDER BY f.id) FROM login_failures f WHERE f.account_id IN ({ids})),"
        f"'articles',(SELECT jsonb_agg(a ORDER BY a.id) FROM articles a WHERE a.author_id IN ({ids})),"
        f"'comments',(SELECT jsonb_agg(c ORDER BY c.id) FROM comments c WHERE c.author_id IN ({ids})))::text"
    )


# 同一 Query 先走公開入口，非法 bytes 與合法替代字元不可合併成同一參數。
def query_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    extra.regression_cases(nonce, backend=backend)
    before = snapshot(nonce, ticket=ticket)
    for query, fields in (
        ("%EF%BF%BD=x", {"�": ["不支援此欄位"]}),
        ("%EF%BF%BD=x&%EF%BF%BD=y", {"�": ["此參數不可重複"]}),
        ("%FF=x&%EF%BF%BD=y", {"%FF": ["不支援此欄位"], "�": ["不支援此欄位"]}),
    ):
        result = extra.decoded(extra.raw_request("POST", "/api/auth/register?" + query, b"{", backend=backend))
        shared.expect(result, 422, "E0001", fields)
        shared.check(snapshot(nonce, ticket=ticket) == before, "Query 拒絕留下部分資料")
    print(f"PASS {ticket.upper()} 合法 U+FFFD／非法 UTF-8 Query 身分", flush=True)


# 保留 DB 整秒與八小時約束，整組時間偏移仍須 401；還原後同 token 必須可用。
def exact_session_cases(nonce, packet, path, *, backend=BACKEND, ticket=TICKET):
    actor = shared.user(backend, nonce + "m", "member")
    claims = json.loads(base64.urlsafe_b64decode(actor["token"].split(".")[1] + "=="))
    jti = claims["jti"]
    # 正確本後端簽章仍須核對 DB 身分；只使用本票真登入，不改任何 session 身分欄位。
    other = shared.user(backend, nonce + "m", "other")
    other_claims = json.loads(base64.urlsafe_b64decode(other["token"].split(".")[1] + "=="))
    peer = shared.expect(shared.request(PEER, "POST", extra.LOGIN,
        {"email": actor["email"], "password": "Abcd1234"}), 200)
    peer_claims = json.loads(base64.urlsafe_b64decode(peer["access_token"].split(".")[1] + "=="))
    absent_jti = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
    shared.check(shared.sql(f"SELECT count(*) FROM login_sessions WHERE jti='{absent_jti}'") == "0",
                 "不存在 jti fixture 發生碰撞")
    before = snapshot(nonce, ticket=ticket)
    for changed in (
        {**claims, "jti": absent_jti},
        {**claims, "jti": other_claims["jti"]},
        {**claims, "sub": str(other["id"])},
        {**peer_claims, "iss": claims["iss"], "aud": claims["aud"]},
    ):
        token = extra.signed_token(changed, backend=backend)
        shared.expect(shared.request(backend, "GET", "/api/me", token=token), 401, "E0002")
        shared.check(snapshot(nonce, ticket=ticket) == before, "session 身分不符拒絕留下部分資料")
    original = json.loads(shared.sql(
        "SELECT json_build_object('id',account_id,'jti',jti,'issued_at',issued_at,'expires_at',expires_at)::text "
        f"FROM login_sessions WHERE jti='{jti}' AND account_id={actor['id']}"))
    packet["session_times"].append(original)
    extra.save_packet(packet, path)
    stable = extra.state(actor["id"])
    try:
        # issued_at 必須是整秒且 expires_at 固定相差八小時，兩欄須一起移動才能形成合法測資。
        for seconds in (1, -1):
            shared.sql(f"UPDATE login_sessions SET issued_at=issued_at+interval '{seconds} seconds',"
                       f"expires_at=expires_at+interval '{seconds} seconds' WHERE jti='{jti}'")
            before = extra.state(actor["id"])
            shared.expect(shared.request(backend, "GET", "/api/me", token=actor["token"]), 401, "E0002")
            shared.check(extra.state(actor["id"]) == before, "session 偏移拒絕更動持久資料")
            shared.sql(f"UPDATE login_sessions SET issued_at='{original['issued_at']}'::timestamptz,"
                       f"expires_at='{original['expires_at']}'::timestamptz WHERE jti='{jti}'")
            shared.expect(shared.request(backend, "GET", "/api/me", token=actor["token"]), 200)
            shared.check(extra.state(actor["id"]) == stable, "session 時間未精確恢復或同 token 讀取留下變更")
    finally:
        shared.sql(f"UPDATE login_sessions SET issued_at='{original['issued_at']}'::timestamptz,"
                   f"expires_at='{original['expires_at']}'::timestamptz WHERE jti='{jti}'")
    print(f"PASS {ticket.upper()} session jti／帳號／backend 身分、整組正負一秒拒絕及同 token 恢復", flush=True)


# 關閉設定須清全部一般會員；逐列核對世代／鎖，其餘欄位、事件與登入都不改。
def settings_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    admin = shared.user(backend, nonce + "g", "admin")
    actor = shared.user(backend, nonce + "g", "member")
    settings = {"enabled": True, "window_minutes": 5, "failure_threshold": 1, "lock_seconds": 30}
    shared.expect(shared.request(backend, "PUT", extra.SETTINGS, settings, admin["token"]), 200)
    shared.expect(shared.request(backend, "POST", extra.LOGIN,
                                {"email": actor["email"], "password": "Wrong1234"}), 429, "E0105")
    before = json.loads(shared.sql("SELECT json_agg(a ORDER BY id)::text FROM accounts a"))
    sessions = shared.sql("SELECT coalesce(jsonb_agg(s ORDER BY jti),'[]'::jsonb)::text FROM login_sessions s")
    failures = shared.sql("SELECT coalesce(jsonb_agg(f ORDER BY id),'[]'::jsonb)::text FROM login_failures f")
    # 改數值不清鎖；完整同值不能換世代或撤銷任何登入。
    settings = {**settings, "window_minutes": 6, "failure_threshold": 2, "lock_seconds": 40}
    shared.expect(shared.request(backend, "PUT", extra.SETTINGS, settings, admin["token"]), 200)
    shared.check(json.loads(shared.sql("SELECT json_agg(a ORDER BY id)::text FROM accounts a")) == before,
                 "數值修改清鎖或改帳號")
    same = snapshot(nonce, ticket=ticket)
    shared.expect(shared.request(backend, "PUT", extra.SETTINGS, settings, admin["token"]), 200)
    shared.check(snapshot(nonce, ticket=ticket) == same, "啟用同值重做副作用")
    disabled = {**settings, "enabled": False}
    shared.expect(shared.request(backend, "PUT", extra.SETTINGS, disabled, admin["token"]), 200)
    after = json.loads(shared.sql("SELECT json_agg(a ORDER BY id)::text FROM accounts a"))
    expected = [{**account, "failure_epoch": account["failure_epoch"] + 1, "locked_until": None}
                if account["role"] == "member" else account for account in before]
    shared.check(after == expected, "關閉未完整清全體會員或更動其他帳號欄位")
    shared.check(shared.sql("SELECT coalesce(jsonb_agg(s ORDER BY jti),'[]'::jsonb)::text FROM login_sessions s") == sessions
                 and shared.sql("SELECT coalesce(jsonb_agg(f ORDER BY id),'[]'::jsonb)::text FROM login_failures f") == failures,
                 "關閉改動 session 或刪除失敗事件")
    same = snapshot(nonce, ticket=ticket)
    shared.expect(shared.request(backend, "PUT", extra.SETTINGS, disabled, admin["token"]), 200)
    shared.check(snapshot(nonce, ticket=ticket) == same, "關閉同值再次清鎖／換世代")
    shared.expect(shared.request(backend, "GET", "/api/me", token=actor["token"]), 200)
    shared.expect(shared.request(backend, "PUT", extra.SETTINGS, settings, admin["token"]), 200)
    shared.check(shared.expect(shared.request(backend, "GET", extra.SETTINGS, token=admin["token"]), 200)
                 == settings, "重新啟用遺失完整四欄")
    print(f"PASS {ticket.upper()} 數值／同值、全體會員清鎖、保留事件與既有登入", flush=True)


# 只接受真正等待設定列的 app 連線，避免宣告 barrier 到達但 HTTP 尚未取得共用鎖。
def waiting_settings(holder_pid, previous=()):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        rows = json.loads(shared.sql(
            "SELECT coalesce(json_agg(json_build_object('pid',pid,'blockers',pg_blocking_pids(pid))),'[]') "
            "FROM pg_stat_activity WHERE datname='articles' AND usename='articles_app' "
            "AND state='active' AND wait_event_type='Lock' "
            "AND query ~* 'SELECT.*login_lockout_settings.*FOR UPDATE'"))
        for row in rows:
            if row["pid"] not in previous and any(pid in (holder_pid, *previous) for pid in row["blockers"]):
                return row["pid"]
        time.sleep(0.025)
    raise AssertionError("未觀察到設定列實際等待")


# 兩後端排隊後才放行，分別核對最後完整設定、關閉前後登入計數與既有 session。
def race_cases(nonce, *, backend=BACKEND, ticket=TICKET):
    admin = shared.user(backend, nonce + "q", "admin")
    peer_token = shared.expect(shared.request(PEER, "POST", extra.LOGIN,
        {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]
    initial = {"enabled": True, "window_minutes": 5, "failure_threshold": 1, "lock_seconds": 30}
    for mode in ("settings", "disable-first", "login-first"):
        actor = shared.user(backend, nonce + mode, "race")
        shared.expect(shared.request(backend, "PUT", extra.SETTINGS, initial, admin["token"]), 200)
        epoch = int(shared.sql(f"SELECT failure_epoch FROM accounts WHERE id={actor['id']}"))
        first_values = {"enabled": True, "window_minutes": 7, "failure_threshold": 3, "lock_seconds": 45}
        last_values = {"enabled": True, "window_minutes": 9, "failure_threshold": 4, "lock_seconds": 60}
        actions = ((backend, "PUT", extra.SETTINGS, first_values, admin["token"]),
                   (PEER, "PUT", extra.SETTINGS, last_values, peer_token))
        if mode != "settings":
            actions = ((PEER, "PUT", extra.SETTINGS, {**initial, "enabled": False}, peer_token),
                       (backend, "POST", extra.LOGIN, {"email": actor["email"], "password": "Wrong1234"}, None))
            if mode == "login-first":
                actions = actions[::-1]
        application = nonce + "-" + mode
        holder = subprocess.Popen(
            ["docker", "compose", "--profile", "*", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
             "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            holder.stdin.write((f"SET application_name='{application}'; SET idle_in_transaction_session_timeout='12s'; "
                                "BEGIN; SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE;\n"
                                f"\\echo {ticket}-locked\n").encode())
            holder.stdin.flush()
            wait_line(holder, ticket + "-locked")
            pid = int(shared.sql(f"SELECT pid FROM pg_stat_activity WHERE application_name='{application}'"))
            with ThreadPoolExecutor(max_workers=2) as pool:
                try:
                    first = pool.submit(shared.request, *actions[0])
                    first_pid = waiting_settings(pid)
                    second = pool.submit(shared.request, *actions[1])
                    waiting_settings(pid, (first_pid,))
                    shared.check(not first.done() and not second.done(), "HTTP 未同時進入設定鎖鏈")
                finally:
                    # 先釋放 holder 才等 HTTP，避免測試自己造成 timeout。
                    holder.stdin.write(b"ROLLBACK;\n\\q\n")
                    holder.stdin.flush()
                results = (first.result(timeout=15), second.result(timeout=15))
            for action, result in zip(actions, results):
                status = 200 if action[1] == "PUT" else 429 if mode == "login-first" else 401
                shared.expect(result, status, "E0105" if status == 429 else "E0101" if status == 401 else None)
            expected = last_values if mode == "settings" else {**initial, "enabled": False}
            shared.check(shared.expect(shared.request(backend, "GET", extra.SETTINGS, token=admin["token"]), 200)
                         == expected, "競爭後四設定不是最後完整提交")
            if mode != "settings":
                shared.check(shared.sql(
                    f"SELECT failure_epoch={epoch + 1} AND locked_until IS NULL AND "
                    f"(SELECT count(*) FROM login_failures WHERE account_id=a.id)={int(mode == 'login-first')} "
                    f"FROM accounts a WHERE id={actor['id']}") == "t", "關閉／登入競爭的計數或清鎖不符")
            shared.expect(shared.request(backend, "GET", "/api/me", token=actor["token"]), 200)
        finally:
            if holder.poll() is None:
                try:
                    holder.stdin.write(b"ROLLBACK;\n\\q\n")
                    holder.stdin.flush()
                    holder.wait(timeout=3)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    holder.kill()
                    holder.wait(timeout=3)
            holder.stdin.close()
            holder.stdout.close()
    print(f"PASS {ticket.upper()} {backend}／{PEER} 設定列重疊、完整更新與關閉／登入雙序", flush=True)


# 共用公開案例只接收明示後端／票號，生命週期及恢復仍由各票入口負責。
def run_cases(nonce, packet, path, *, backend=BACKEND, ticket=TICKET, regression=None):
    cases = {"query": query_cases, "settings": settings_cases, "race": race_cases}
    for name, case in cases.items():
        if regression is None or regression == name:
            print(f"RUN {ticket.upper()} {name}", flush=True)
            case(nonce, backend=backend, ticket=ticket)
    if regression in (None, "session"):
        for case in (extra.entry_cases, extra.session_cases):
            print(f"RUN {ticket.upper()} {case.__name__}", flush=True)
            case(nonce, backend=backend)
        exact_session_cases(nonce, packet, path, backend=backend, ticket=ticket)
        extra.expiry_cases(nonce, packet, path, backend=backend)
    if regression is None:
        extra.member_list_cases(nonce, packet, path, backend=backend)
        records = shared.backend_cases(backend, nonce)
        for case in (shared.contract_cases, shared.boundary_cases, shared.lockout_cases,
                     shared.lockout_edge_cases, shared.database_cases):
            print(f"RUN {ticket.upper()} {case.__name__}", flush=True)
            case(backend, nonce)
        counts = shared.extended_invalid_cases(backend, nonce, {})
        shared.check(len(counts) == 284, "284 invalid cases 缺漏")
        shared.ordered_cases(backend, nonce)
        extra.readback(records, backend=backend)
        extra.unavailable_cases(nonce, backend=backend)


# 原票入口固定 Flask/CQ22；新票只能重用案例，不能由此繞過自己的恢復程序。
def main(argv=None):
    args = arguments(argv)
    shared.check(hashlib.sha256(Path(shared.__file__).read_bytes()).hexdigest() == GUARD_SHA,
                 "Ticket57 guard 來源不同")
    if args.self_check:
        self_check()
        return
    packet = None
    if args.restore is not None:
        packet = json.loads(args.restore.read_text(encoding="utf-8"))
        extra.validate_packet(packet, ticket=TICKET)
        shared.check(args.restore.name == f"recovery-{packet['nonce']}.json" and not packet["restored"],
                     "恢復檔名不符或已恢復，拒絕覆蓋 baseline")
    else:
        pending_recovery()
    shared.guard()
    if packet is not None:
        extra.restore_baseline(packet, args.restore, ticket=TICKET)
        return
    nonce = TICKET + secrets.token_hex(4)
    packet, path = extra.capture_baseline(nonce, ticket=TICKET)
    try:
        run_cases(nonce, packet, path, regression=args.regression)
    finally:
        extra.restore_baseline(packet, path, ticket=TICKET)
    print("PASS CQ22 " + (args.regression or "Flask 公開 HTTP／DB full suite"), flush=True)
    print("限制：保留新增 fixture／logs／sequences；搜尋 NUL、UI 及其他十一版完整矩陣未驗", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # assertion 可能帶 JWT payload，只保留安全 prefix，不把憑證寫入 evidence。
        detail = str(error).split("actual=", 1)[0].split("expected=", 1)[0][:300] if isinstance(error, AssertionError) else ""
        print(f"FAIL CQ22 {type(error).__name__}: {detail}；核對 RUN／recovery", file=sys.stderr)
        sys.exit(1)
