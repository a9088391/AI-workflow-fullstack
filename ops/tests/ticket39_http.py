"""Ticket 39：先核對隔離目標，再測 Fastify 管理與跨 Laravel 狀態。"""

import json
from contextlib import contextmanager
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import quote
from urllib.request import urlopen

from ticket37_http import call, check, cross_attempt, expect, sql
from ticket37_query_count import db_logs, statements

FASTIFY = "http://127.0.0.1:18393"
LARAVEL = "http://127.0.0.1:18392"
PROJECT = "ticket39_test"
COMPOSE = "compose.yaml:ops/tests/ticket39_compose.yaml"


# guard 核對實際容器、專用 DB 資料卷、埠及金鑰，通過前禁止任何 fixture 寫入。
def guard():
    # 先核對呼叫者指定的專案與 override，錯誤目標在任何 Docker/DB 指令前就拒絕。
    check(os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT, "錯誤 Compose 專案")
    check(os.environ.get("COMPOSE_FILE") == COMPOSE, "錯誤 Compose 檔")
    config = json.loads(subprocess.check_output(["docker", "compose", "--profile", "fastify", "config", "--format", "json"], text=True))
    check(config["name"] == PROJECT and config["volumes"]["postgres_data"]["name"] == PROJECT + "_postgres_data", "非隔離資料卷")
    check(config["services"]["db"]["environment"]["TICKET39_FIXTURE"] == "isolated-ticket39", "fixture 標記錯誤")
    # 後端必須是本次建置映像、健康容器、獨立唯讀金鑰與指定 loopback 埠。
    for service, port in (("laravel", "18392"), ("fastify", "18393")):
        ids = subprocess.check_output(["docker", "compose", "--profile", "fastify", "ps", "-q", service], text=True).splitlines()
        check(len(ids) == 1, "後端容器未唯一啟動：" + service)
        item = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
        check(item["Config"]["Labels"].get("com.docker.compose.project") == PROJECT, "容器不屬於本票")
        check(item["State"]["Running"] and item["State"].get("Health", {}).get("Status") == "healthy", "後端不健康")
        check(item["HostConfig"]["PortBindings"]["8000/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": port}], "實際埠錯誤")
        key = os.path.realpath(f".local/ticket39-backend/{service}.key")
        check(any(os.path.realpath(m.get("Source", "")) == key and m.get("Destination") == f"/run/secrets/{service}-jwt.key" and m.get("RW") is False for m in item["Mounts"]), "測試金鑰掛載錯誤")
        with open(key, "rb") as test_key, open(f".local/keys/{service}.key", "rb") as demo_key:
            check(test_key.read() != demo_key.read(), "測試與展示金鑰共用")
        image = subprocess.check_output(["docker", "compose", "--profile", "fastify", "images", "-q", service], text=True).strip()
        check("sha256:" + image == item["Image"], "執行中容器不是目前 Compose 映像")
    # 最後核對資料庫 volume 與真實連線身分，再允許建立測試帳號。
    ids = subprocess.check_output(["docker", "compose", "ps", "-q", "db"], text=True).splitlines()
    check(len(ids) == 1, "隔離 DB 未唯一啟動")
    database = json.loads(subprocess.check_output(["docker", "inspect", ids[0]], text=True))[0]
    check(database["Config"]["Labels"].get("com.docker.compose.project") == PROJECT and
          "TICKET39_FIXTURE=isolated-ticket39" in database["Config"]["Env"], "執行中 DB 身分錯誤")
    check(any(m.get("Name") == PROJECT + "_postgres_data" for m in database["Mounts"]), "DB 資料卷錯誤")
    check(sql("SELECT current_database()||':'||current_user") == "articles:articles_owner", "DB 身分錯誤")
    for base in (FASTIFY, LARAVEL):
        with urlopen(base + "/up", timeout=5) as response:
            check(response.status == 200 and response.read() == b"OK", "後端未就緒")


# create_user 只在隔離 DB 中建立唯一會員；管理員角色由受控 fixture 更新。
def create_user(tag, backend=FASTIFY, admin=False):
    email = f"t39-{tag}@example.test"
    expect(call(backend, "POST", "/api/auth/register", {"email": email, "password": "Abcd1234", "nickname": "票39測試"}), 201)
    # 管理員只在已核對的專用資料庫升級，公開註冊本身不得授予 admin。
    if admin:
        sql(f"UPDATE accounts SET role='admin' WHERE email='{email}'")
    data = expect(call(backend, "POST", "/api/auth/login", {"email": email, "password": "Abcd1234"}), 200)
    return {"id": data["user"]["id"], "email": email, "token": data["access_token"]}


# settings 讀取四欄並拒絕額外資料，便於跨後端逐值比較。
def settings(base, token):
    data = expect(call(base, "GET", "/api/settings/login-lockout", token=token), 200)
    check(set(data) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, "設定欄位不精確")
    return data


# parity_error 在相同資料狀態逐字比較兩後端錯誤，僅排除每請求獨立的 log_id。
def parity_error(method, path, body, fastify_token, laravel_token, status, code, fields=None):
    payloads = [expect(call(base, method, path, body, token=token), status, code, fields)
                for base, token in ((FASTIFY, fastify_token), (LARAVEL, laravel_token))]
    # 追查碼各自有效，但業務狀態、文案與驗證明細不得因框架不同而變。
    for payload in payloads:
        check(str(uuid.UUID(payload["log_id"])) == payload["log_id"], "log_id 不是 UUID")
    check({key: value for key, value in payloads[0].items() if key != "log_id"} ==
          {key: value for key, value in payloads[1].items() if key != "log_id"}, "跨後端錯誤不一致")


# locked_sql 只在 guard 通過後持有指定隔離資料列，供兩後端請求實際重疊。
def locked_sql(statement):
    command = ["docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
               "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c",
               "SET application_name='ticket39_holder'; BEGIN; " + statement + "; SELECT pg_sleep(6); COMMIT;"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # 等待資料庫確認持鎖者進入 pg_sleep，避免只憑固定時間猜測並行順序。
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _, error = process.communicate()
            raise AssertionError("隔離鎖交易未持鎖：" + error.strip())
        if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='ticket39_holder' AND wait_event='PgSleep'") == "1":
            return process
        time.sleep(0.05)
    process.terminate()
    process.communicate()
    raise AssertionError("隔離鎖交易未進入等待")


# wait_lock 確認真正的資料庫鎖等待，而非只以固定睡眠推測請求順序。
def wait_lock(minimum=1):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if int(sql("SELECT count(*) FROM pg_stat_activity WHERE usename='articles_app' AND wait_event_type='Lock'")) >= minimum:
            return
        time.sleep(0.05)
    raise AssertionError("未觀察到後端資料庫鎖等待")


# finish_lock 等隔離持鎖交易提交後才檢查結果，避免未知提交狀態被當成測試結論。
def finish_lock(process):
    _, error = process.communicate(timeout=10)
    check(process.returncode == 0, "隔離鎖交易失敗：" + error.strip())


# strict_sql 讓測試用 trigger 的建立與移除有明確失敗狀態，不把 psql 錯誤當成功。
def strict_sql(query):
    return subprocess.check_output(["docker", "compose", "exec", "-T", "db", "psql", "-X", "-q", "-t", "-A",
                                    "-v", "ON_ERROR_STOP=1", "-U", "articles_owner", "-d", "articles", "-c", query], text=True).strip()


# rollback_catalog_sql 唯讀收集同名物件與完整身分，包含其他 namespace／overload，未知者只能保留。
def rollback_catalog_sql():
    return (
        "WITH f AS (SELECT p.* FROM pg_catalog.pg_proc p WHERE p.proname='ticket39_fail_epoch'), "
        "t AS (SELECT x.* FROM pg_catalog.pg_trigger x WHERE x.tgname='ticket39_rollback_probe') "
        "SELECT jsonb_build_object('table_oid','public.accounts'::regclass::oid,"
        "'functions',COALESCE((SELECT jsonb_agg(jsonb_build_object('row',to_jsonb(f),"
        "'namespace',(SELECT nspname FROM pg_catalog.pg_namespace WHERE oid=f.pronamespace),"
        "'owner',pg_get_userbyid(f.proowner),'definition',CASE WHEN f.prokind IN ('f','p') "
        "THEN pg_get_functiondef(f.oid) ELSE NULL END) ORDER BY f.oid) FROM f),'[]'::jsonb),"
        "'triggers',COALESCE((SELECT jsonb_agg(jsonb_build_object('row',to_jsonb(t),"
        "'definition',pg_get_triggerdef(t.oid,true)) ORDER BY t.oid) FROM t),'[]'::jsonb),"
        "'comments',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.classoid,d.objoid,d.objsubid) "
        "FROM pg_catalog.pg_description d WHERE (d.classoid='pg_proc'::regclass AND d.objoid IN (SELECT oid FROM f)) "
        "OR (d.classoid='pg_trigger'::regclass AND d.objoid IN (SELECT oid FROM t))),'[]'::jsonb),"
        "'dependencies',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.classid,d.objid,d.objsubid,"
        "d.refclassid,d.refobjid,d.refobjsubid,d.deptype) FROM pg_catalog.pg_depend d WHERE "
        "(d.classid='pg_proc'::regclass AND d.objid IN (SELECT oid FROM f)) OR "
        "(d.refclassid='pg_proc'::regclass AND d.refobjid IN (SELECT oid FROM f)) OR "
        "(d.classid='pg_trigger'::regclass AND d.objid IN (SELECT oid FROM t)) OR "
        "(d.refclassid='pg_trigger'::regclass AND d.refobjid IN (SELECT oid FROM t))),'[]'::jsonb))"
    )


# rollback_catalog 只讀固定物件，executor 預設仍走原隔離 SQL，副本必顯式傳其受 guard 保護的 executor。
def rollback_catalog(*, execute=strict_sql):
    catalog = json.loads(execute(rollback_catalog_sql()))
    check(isinstance(catalog, dict) and set(catalog) == {'table_oid','functions','triggers','comments','dependencies'} and
          all(isinstance(catalog[key], list) for key in ('functions','triggers','comments','dependencies')), 'DDL catalog 回應不完整')
    return catalog


# rollback_receipt 核對完整 metadata；builder 可只有 function，原 context 另要求 pair，不認領歷史殘留。
def rollback_receipt(catalog):
    # PostgreSQL JSON 的 OID 是字串；只驗 canonical uint32，不改 raw catalog 的逐值身分。
    check(isinstance(catalog, dict) and set(catalog) == {'table_oid','functions','triggers','comments','dependencies'} and
          type(catalog['table_oid']) is str and re.fullmatch(r'[1-9][0-9]{0,9}', catalog['table_oid']) is not None and
          int(catalog['table_oid']) <= 4294967295 and
          all(isinstance(catalog[key], list) for key in ('functions','triggers','comments','dependencies')) and
          len(catalog['functions']) == 1 and len(catalog['triggers']) <= 1, '本輪 DDL receipt 不完整')
    function = catalog['functions'][0]
    check(isinstance(function, dict) and set(function) == {'row','namespace','owner','definition'} and
          isinstance(function['row'], dict) and isinstance(function['definition'], str) and
          isinstance(function['owner'], str) and
          {'oid','proname','pronargs','prokind','proowner','proacl','prosrc','prosecdef','proconfig'} <= set(function['row']), '本輪 DDL receipt metadata 不完整')
    check(function['namespace'] == 'public' and function['row']['proname'] == 'ticket39_fail_epoch' and
          function['row']['pronargs'] == 0 and function['row']['prokind'] == 'f', '本輪 DDL receipt function 目標不符')
    if catalog['triggers']:
        trigger = catalog['triggers'][0]['row']
        check(trigger['tgname'] == 'ticket39_rollback_probe' and trigger['tgrelid'] == catalog['table_oid'] and
              trigger['tgfoid'] == function['row']['oid'], '本輪 DDL receipt trigger 目標不符')
    return catalog


# clear_rollback_probe 保留任何同名 function/trigger（含 overload/其他 namespace），只有本輪 receipt 可清。
def clear_rollback_probe(owned=None, *, execute=strict_sql):
    current = rollback_catalog(execute=execute)
    if owned is None:
        check(not any(current[key] for key in ('functions','triggers','comments','dependencies')), 'Ticket 39 DDL 已存在；保留現場，須人工處理')
        return
    rollback_receipt(owned)
    check(current == owned, 'Ticket 39 本輪 DDL 身分已變；拒絕清理')
    # RESTRICT 與單交易保護未知相依物件，不能用 CASCADE 擴大刪除範圍。
    drop_trigger = 'DROP TRIGGER ticket39_rollback_probe ON public.accounts; ' if owned['triggers'] else ''
    execute('BEGIN; ' + drop_trigger + 'DROP FUNCTION public.ticket39_fail_epoch() RESTRICT; COMMIT;')
    clear_rollback_probe(execute=execute)


# rollback_create_sql 固定本票 function/trigger 與正整數 owned account，副本不另複製建立邏輯。
def rollback_create_sql(target_id):
    check(type(target_id) is int and 1 <= target_id <= 9007199254740991, 'rollback DDL 帳號不符')
    return ("CREATE FUNCTION public.ticket39_fail_epoch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ticket39 rollback probe'; END $$",
            f'CREATE TRIGGER ticket39_rollback_probe BEFORE UPDATE OF failure_epoch ON public.accounts FOR EACH ROW WHEN (NEW.id={target_id}) EXECUTE FUNCTION public.ticket39_fail_epoch()')


# rollback_trigger 只有建立、receipt、COMMIT 都確認才進案例；finally 只清本輪物件，未知結果保留。
@contextmanager
def rollback_trigger(target_id, *, execute=strict_sql):
    clear_rollback_probe(execute=execute)
    statements = rollback_create_sql(target_id)
    owned = None
    try:
        raw = execute('BEGIN; ' + '; '.join(statements) + '; ' + rollback_catalog_sql() + '; COMMIT;')
        parsed = rollback_receipt(json.loads(raw))
        check(len(parsed['triggers']) == 1, '本輪 DDL pair 未完整建立')
        owned = parsed
        yield owned
    finally:
        # 工具或 JSON/receipt 失敗不授 ownership；清理再失敗仍保留原案例的例外鏈。
        if owned is not None:
            clear_rollback_probe(owned, execute=execute)


# rollback_probe 只在本票資料庫注入一次更新失敗，確認設定與所有會員清鎖一起 rollback。
def rollback_probe(admin_token, target_id, disabled):
    clear_rollback_probe()
    before = strict_sql(f"SELECT row_to_json(x) FROM (SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1) x")
    account = strict_sql(f"SELECT row_to_json(x) FROM (SELECT failure_epoch,locked_until FROM accounts WHERE id={target_id}) x")
    failure_count = strict_sql(f"SELECT count(*) FROM login_failures WHERE account_id={target_id}")
    with rollback_trigger(target_id):
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", disabled, admin_token), 500, "E9999")
        check(strict_sql("SELECT row_to_json(x) FROM (SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1) x") == before,
              "失敗後設定仍有部分提交")
        check(strict_sql(f"SELECT row_to_json(x) FROM (SELECT failure_epoch,locked_until FROM accounts WHERE id={target_id}) x") == account and
              strict_sql(f"SELECT count(*) FROM login_failures WHERE account_id={target_id}") == failure_count,
              "失敗後會員鎖定或計數遭修改")
    clear_rollback_probe()


# search_plan 核對 Fastify 真正送出的計數／分頁 SQL，再記錄隔離資料上的查詢計畫。
def search_plan(admin_token, keyword):
    before = db_logs()
    expect(call(FASTIFY, "GET", "/api/members?page=1&per_page=20&filters%5Bkeyword%5D=" + quote(keyword), token=admin_token), 200)
    emitted = statements(before, db_logs())
    check(len(emitted) == 3 and "SELECT count(*)::bigint" in emitted[1] and "FROM accounts" in emitted[1] and
          "SELECT id,email,nickname,role,status,created_at" in emitted[2], f"搜尋 SQL 次數或形狀不符：{emitted}")
    print("PASS ticket39 member search SQL count=3")
    strict_sql("ANALYZE accounts")
    # 查詢與實際 API 同用資料庫篩選、精確計數及 created_at/id 排序，保留估算與耗時證據。
    base = "FROM accounts WHERE role='member' AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'" + keyword + "')>0 OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'" + keyword + "')>0)"
    for label, query in (("count", "SELECT count(*)::bigint AS total " + base),
                         ("page", "SELECT id,email,nickname,role,status,created_at " + base + " ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET 0")):
        plan = strict_sql("EXPLAIN (ANALYZE, BUFFERS) " + query)
        check("Execution Time" in plan and "Buffers" in plan and "actual" in plan, label + " 查詢計畫不完整")
        print("PLAN members-" + label, plan.replace("\n", " | "))


# management_sql 核對管理列表的實際 SQL 次數，避免每筆文章或留言再查作者。
def management_sql(admin_token):
    for name, path, table in (("articles", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=management", "articles"),
                              ("comments", "/api/comments?page=1&per_page=20", "comments")):
        before = db_logs()
        expect(call(FASTIFY, "GET", path, token=admin_token), 200)
        emitted = statements(before, db_logs())
        check(len(emitted) == 3 and "SELECT count(*)::bigint" in emitted[1] and
              f"FROM {table} JOIN accounts" in emitted[1] and f"SELECT {table}.id" in emitted[2],
              f"{name} 管理列表 SQL 次數或形狀不符：{emitted}")
        print(f"PASS ticket39 {name} management SQL count=3")


# boundary_sql 沿用已驗證的真實 PostgreSQL 微秒案例，只替換 Fastify 讀取一次時鐘的結果。
def boundary_sql():
    source = Path(__file__).with_name("ticket37_boundary.js").read_text()
    result = subprocess.run(["docker", "compose", "--profile", "fastify", "exec", "-T", "fastify", "node", "-"],
                            input=source, text=True, capture_output=True, timeout=60)
    check(result.returncode == 0 and "PASS ticket37 real login SQL boundary" in result.stdout,
          "真實 SQL 微秒邊界失敗：" + result.stderr.strip())
    print("PASS ticket39 real Fastify login SQL boundary: left excluded, +1us included")


# main 依契約順序測角色、搜尋、停用、內容及設定的 HTTP 與持久效果。
def main():
    guard()
    clear_rollback_probe()
    if sys.argv[1:] == ["--guard-only"]:
        print("PASS ticket39_test guard")
        return
    nonce = secrets.token_hex(5)
    admin = create_user(nonce + "-admin", admin=True)
    member = create_user(nonce + "-member")
    other = create_user(nonce + "-other", backend=LARAVEL)
    admin_laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": admin["email"], "password": "Abcd1234"}), 200)["access_token"]
    member_laravel = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    original = settings(FASTIFY, admin["token"])
    check(settings(LARAVEL, admin_laravel) == original, "初始跨後端設定不同")
    # 身分先於無效參數；管理員 ID 對管理讀取及狀態修改都隱藏。
    for method, path, body in (("GET", "/api/members?bad=1", None),
                               ("GET", "/api/members/bad?bad=1", None),
                               ("PUT", "/api/members/bad/status", {"status": "bad"}),
                               ("GET", "/api/comments?bad=1", None),
                               ("GET", "/api/settings/login-lockout?bad=1", None),
                               ("PUT", "/api/settings/login-lockout", {"bad": 1})):
        parity_error(method, path, body, None, None, 401, "E0002")
        parity_error(method, path, body, member["token"], member_laravel, 403, "E0003")
    for path in (f"/api/members/{admin['id']}", "/api/members/9007199254740991"):
        parity_error("GET", path, None, admin["token"], admin_laravel, 404, "E0004")
    parity_error("PUT", f"/api/members/{admin['id']}/status", {"status": "disabled"},
                 admin["token"], admin_laravel, 404, "E0004")
    # 精確 errors 與搜尋：短字、ASCII 折疊、Unicode、萬用符號、NUL、超頁。
    parity_error("GET", "/api/members?page=1&per_page=20&filters[keyword][]=x", None,
                 admin["token"], admin_laravel, 422, "E0001", {"filters.keyword": ["欄位型態不正確"]})
    parity_error("PUT", "/api/settings/login-lockout",
                 {"enabled": "true", "window_minutes": 0, "failure_threshold": 5, "lock_seconds": 300},
                 admin["token"], admin_laravel, 422, "E0001",
                 {"enabled": ["欄位型態不正確"], "window_minutes": ["欄位格式或範圍不正確"]})
    parity_error("PUT", f"/api/members/{member['id']}/status", {"status": "deleted"},
                 admin["token"], admin_laravel, 422, "E0001", {"status": ["欄位格式或範圍不正確"]})
    parity_error("GET", "/api/comments?page=1&per_page=20&other=1", None,
                 admin["token"], admin_laravel, 422, "E0001", {"other": ["不支援此欄位"]})
    # 原始 JSON 指數與超過安全整數上界不能先被語言的數值轉換放行。
    for raw, fields in (('{"enabled":true,"window_minutes":1e0,"failure_threshold":5,"lock_seconds":300}',
                         {"window_minutes": ["欄位型態不正確"]}),
                        ('{"enabled":true,"window_minutes":9007199254740992,"failure_threshold":5,"lock_seconds":300}',
                         {"window_minutes": ["欄位格式或範圍不正確"]})):
        check('"window_minutes":1e0' in raw or '"window_minutes":9007199254740992' in raw,
              "原始 JSON 數字文字遭改寫")
        parity_error("PUT", "/api/settings/login-lockout", raw,
                     admin["token"], admin_laravel, 422, "E0001", fields)
    search_ids = []
    for nickname in ("Aa%_\\Ä陳" + nonce, "aa%_\\ä陳" + nonce):
        email = f"t39-{nonce}-search{len(nickname.encode())}{secrets.token_hex(2)}@example.test"
        expect(call(FASTIFY, "POST", "/api/auth/register", {"email": email, "password": "Abcd1234", "nickname": nickname}), 201)
        search_ids.append(int(sql(f"SELECT id FROM accounts WHERE email='{email}'")))
    # 一、二碼搜尋比對新增前後精確集合，重跑時也不假設隔離資料卷為空。
    short = ("𒀀", "𒀀𒀁")
    before_short = [expect(call(FASTIFY, "GET", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(word), token=admin["token"]), 200) for word in short]
    short_email = f"t39-{nonce}-short@example.test"
    expect(call(FASTIFY, "POST", "/api/auth/register", {"email": short_email, "password": "Abcd1234", "nickname": short[1] + nonce}), 201)
    short_id = int(sql(f"SELECT id FROM accounts WHERE email='{short_email}'"))
    for word, before in zip(short, before_short):
        after = expect(call(FASTIFY, "GET", "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(word), token=admin["token"]), 200)
        check(after["pagination"]["total"] == before["pagination"]["total"] + 1 and
              set(row["id"] for row in after["list"]) == set(row["id"] for row in before["list"]) | {short_id}, "一／二碼搜尋集合錯誤")
    check(quote("\0") == "%00" and quote("\\") == "%5C", "特殊搜尋字元的 URL 編碼不符")
    for keyword, expected_ids in (("aA%_\\Ä陳" + nonce, {search_ids[0]}),
                                  ("aa%_\\ä陳" + nonce, {search_ids[1]}),
                                  ("ä陳" + nonce, {search_ids[1]}), ("\0", set())):
        path = "/api/members?page=1&per_page=100&filters%5Bkeyword%5D=" + quote(keyword)
        data = expect(call(FASTIFY, "GET", path, token=admin["token"]), 200)
        check(data["pagination"]["total"] == len(expected_ids) and
              {row["id"] for row in data["list"]} == expected_ids, "搜尋結果或 total 錯誤：" + repr(keyword))
        peer = expect(call(LARAVEL, "GET", path, token=admin_laravel), 200)
        check(peer == data, "跨後端搜尋集合或欄位不一致：" + repr(keyword))
    page = expect(call(FASTIFY, "GET", "/api/members?page=1&per_page=100", token=admin["token"]), 200)
    check(all(row["role"] == "member" and set(row) == {"id", "email", "nickname", "role", "status", "created_at"} for row in page["list"]), "會員列表範圍／欄位錯誤")
    deep = expect(call(FASTIFY, "GET", "/api/members?page=999999&per_page=1", token=admin["token"]), 200)
    check(deep["list"] == [] and deep["pagination"]["total"] == page["pagination"]["total"] and deep["pagination"]["page"] == 999999, "超頁總數錯誤")
    check(expect(call(LARAVEL, "GET", "/api/members?page=999999&per_page=1", token=admin_laravel), 200) == deep,
          "跨後端深頁總數不同")
    search_plan(admin["token"], nonce)
    detail = expect(call(FASTIFY, "GET", f"/api/members/{member['id']}", token=admin["token"]), 200)
    check(detail["email"] == member["email"] and
          expect(call(LARAVEL, "GET", f"/api/members/{member['id']}", token=admin_laravel), 200) == detail,
          "跨後端會員詳情錯誤")
    # 建立與下架內容，驗證管理文章範圍及全部留言都可讀。
    title = "ticket39-" + nonce
    expect(call(FASTIFY, "POST", "/api/articles", {"title": title, "body": "本文"}, member["token"]), 201)
    article = int(sql(f"SELECT id FROM articles WHERE title='{title}' AND author_id={member['id']}"))
    expect(call(FASTIFY, "POST", f"/api/articles/{article}/comments", {"body": "下架父文留言"}, member["token"]), 201)
    comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body='下架父文留言'"))
    # 舊 01–16 操作逐一路過至少一個契約拒絕，避免新管理路由改動共同驗證順序。
    expect(call(FASTIFY, "POST", "/api/auth/register", {"email": "bad@example.test", "nickname": "測試"}), 422, "E0001", {"password": ["此欄位為必填"]})
    expect(call(FASTIFY, "POST", "/api/auth/login", {"email": member["email"]}), 422, "E0001", {"password": ["此欄位為必填"]})
    for method, path, body in (("POST", "/api/auth/logout", None), ("GET", "/api/me", None),
                               ("POST", "/api/articles", {"title": "拒絕", "body": "拒絕"}),
                               ("PUT", f"/api/articles/{article}", {"title": title, "body": "本文", "version": 1}),
                               ("GET", "/api/me/articles?page=1&per_page=20", None),
                               ("GET", "/api/me/comments?page=1&per_page=20", None),
                               ("PUT", f"/api/articles/{article}/visibility", {"visibility": "hidden", "version": 1}),
                               ("POST", f"/api/articles/{article}/comments", {"body": "拒絕"}),
                               ("GET", f"/api/comments/{comment}", None),
                               ("PUT", f"/api/comments/{comment}", {"body": "拒絕", "version": 1}),
                               ("PUT", f"/api/comments/{comment}/visibility", {"visibility": "hidden", "version": 1})):
        expect(call(FASTIFY, method, path, body), 401, "E0002")
    expect(call(FASTIFY, "GET", "/api/articles/bad"), 422, "E0001", {"article_id": ["欄位格式或範圍不正確"]})
    expect(call(FASTIFY, "GET", f"/api/articles/{article}/comments?page=bad&per_page=20"), 422, "E0001", {"page": ["欄位格式或範圍不正確"]})
    # 同一隔離來源依公開契約逐一走過既有操作 03–16 的正向路徑。
    check(article in [row["id"] for row in expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=100"), 200)["list"]], "公開文章列表缺新文章")
    check(expect(call(FASTIFY, "GET", f"/api/articles/{article}"), 200)["title"] == title, "文章詳情錯誤")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article}", {"title": title, "body": "本文", "version": 1}, member["token"]), 200)
    check(article in [row["id"] for row in expect(call(FASTIFY, "GET", "/api/me/articles?page=1&per_page=100", token=member["token"]), 200)["list"]], "我的文章缺新文章")
    check(comment in [row["id"] for row in expect(call(FASTIFY, "GET", "/api/me/comments?page=1&per_page=100", token=member["token"]), 200)["list"]], "我的留言缺新留言")
    check(comment in [row["id"] for row in expect(call(FASTIFY, "GET", f"/api/articles/{article}/comments?page=1&per_page=100"), 200)["list"]], "文章留言缺新留言")
    check(expect(call(FASTIFY, "GET", f"/api/comments/{comment}", token=member["token"]), 200)["body"] == "下架父文留言", "留言詳情錯誤")
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment}", {"body": "下架父文留言", "version": 1}, member["token"]), 200)
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment}/visibility", {"visibility": "hidden", "version": 1}, member["token"]), 200)
    expect(call(FASTIFY, "PUT", f"/api/comments/{comment}/visibility", {"visibility": "visible", "version": 2}, member["token"]), 200)
    spare = expect(call(FASTIFY, "POST", "/api/auth/login", {"email": other["email"], "password": "Abcd1234"}), 200)["access_token"]
    expect(call(FASTIFY, "POST", "/api/auth/logout", token=spare), 200)
    expect(call(FASTIFY, "GET", "/api/me", token=spare), 401, "E0002")
    expect(call(FASTIFY, "PUT", f"/api/articles/{article}/visibility", {"visibility": "hidden", "version": 1}, member["token"]), 200)
    expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management"), 401, "E0002")
    expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management", token=member["token"]), 403, "E0003")
    managed = expect(call(FASTIFY, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management", token=admin["token"]), 200)
    check(article in [row["id"] for row in managed["list"]] and
          expect(call(LARAVEL, "GET", "/api/articles?page=1&per_page=100&filters[scope]=management", token=admin_laravel), 200) == managed,
          "管理文章漏下架資料或跨後端不一致")
    comments = expect(call(FASTIFY, "GET", "/api/comments?page=1&per_page=100", token=admin["token"]), 200)
    check(comment in [row["id"] for row in comments["list"]] and
          expect(call(LARAVEL, "GET", "/api/comments?page=1&per_page=100", token=admin_laravel), 200) == comments,
          "管理留言漏下架父文資料或跨後端不一致")
    management_sql(admin["token"])
    # 同值狀態不撤銷；實際停用撤銷兩個後端 session，恢復後舊憑證不復活。
    expect(call(FASTIFY, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin["token"]), 200)
    expect(call(FASTIFY, "GET", "/api/me", token=member["token"]), 200)
    expect(call(FASTIFY, "PUT", f"/api/members/{member['id']}/status", {"status": "disabled"}, admin["token"]), 200)
    expect(call(FASTIFY, "GET", "/api/me", token=member["token"]), 403, "E0102")
    expect(call(LARAVEL, "GET", "/api/me", token=member_laravel), 403, "E0102")
    expect(call(LARAVEL, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin_laravel), 200)
    expect(call(FASTIFY, "GET", "/api/me", token=member["token"]), 401, "E0002")
    check(sql(f"SELECT status FROM accounts WHERE id={member['id']}") == "active" and sql(f"SELECT title FROM articles WHERE id={article}") == title, "恢復或內容持久狀態錯誤")
    # Laravel 的失敗事件由 Fastify 讀取；只有實際關閉才清鎖與計數，數值變更保留期限。
    try:
        enabled = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 300}
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", enabled, admin["token"]), 200)
        check(settings(LARAVEL, admin_laravel) == enabled, "Fastify 設定未跨後端生效")
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", {**enabled, "lock_seconds": 30}, admin["token"]), 200)
        boundary_sql()
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", enabled, admin["token"]), 200)
        # 先後排隊兩個完整設定寫入；共同設定列只允許最後提交的四欄一起保留。
        first = {**enabled, "failure_threshold": 3}
        last = {**enabled, "failure_threshold": 4, "lock_seconds": 600}
        holder = locked_sql("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fastify_put = pool.submit(call, FASTIFY, "PUT", "/api/settings/login-lockout", first, admin["token"])
                wait_lock()
                laravel_put = pool.submit(call, LARAVEL, "PUT", "/api/settings/login-lockout", last, admin_laravel)
                wait_lock(2)
                finish_lock(holder)
                expect(fastify_put.result(), 200)
                expect(laravel_put.result(), 200)
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.communicate()
        check(settings(FASTIFY, admin["token"]) == last and settings(LARAVEL, admin_laravel) == last, "設定競爭混合欄位或順序錯誤")
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", enabled, admin["token"]), 200)
        # 兩個後端同時輸錯同一會員，設定列序列化後只允許一次 401 與一次觸發鎖定。
        concurrent = create_user(nonce + "-concurrent")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_failure = pool.submit(cross_attempt, barrier, FASTIFY, concurrent["email"])
            second_failure = pool.submit(cross_attempt, barrier, LARAVEL, concurrent["email"])
            failures = [first_failure.result(timeout=20), second_failure.result(timeout=20)]
        check(sorted(result[0] for result in failures) == [401, 429], "跨後端並行登入漏計或重複鎖定")
        check(strict_sql(f"SELECT count(*) FROM login_failures WHERE account_id={concurrent['id']}") == "2" and
              strict_sql(f"SELECT locked_until IS NOT NULL FROM accounts WHERE id={concurrent['id']}") == "t",
              "並行失敗事件或鎖定未持久化")
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": other["email"], "password": "Wrong1234"}), 401, "E0101")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": other["email"], "password": "Wrong1234"}), 429, "E0105")
        until = sql(f"SELECT locked_until::text FROM accounts WHERE id={other['id']}")
        changed = {**enabled, "lock_seconds": 600}
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", changed, admin["token"]), 200)
        check(sql(f"SELECT locked_until::text FROM accounts WHERE id={other['id']}") == until, "數值變更改動既有期限")
        # 以資料庫實際時間設邊界：Fastify 在鎖內回 429，Laravel 到期後能登入並清鎖。
        boundary = create_user(nonce + "-boundary")
        sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '3 seconds' WHERE id={boundary['id']}")
        expect(call(FASTIFY, "POST", "/api/auth/login", {"email": boundary["email"], "password": "Abcd1234"}), 429, "E0105")
        deadline = time.monotonic() + 5
        while sql(f"SELECT clock_timestamp() >= locked_until FROM accounts WHERE id={boundary['id']}") != "t":
            check(time.monotonic() < deadline, "資料庫鎖定期限未到")
            time.sleep(0.05)
        expect(call(LARAVEL, "POST", "/api/auth/login", {"email": boundary["email"], "password": "Abcd1234"}), 200)
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={boundary['id']}") == "t", "跨後端到期未清鎖")
        epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}"))
        disabled = {**changed, "enabled": False}
        rollback_probe(admin["token"], other["id"], disabled)
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", disabled, admin["token"]), 200)
        check(settings(LARAVEL, admin_laravel) == disabled and sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={other['id']}") == "t", "關閉未清鎖")
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}")) == epoch + 1, "關閉未清計數世代")
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", disabled, admin["token"]), 200)
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={other['id']}")) == epoch + 1, "同值關閉重做副作用")
    finally:
        expect(call(FASTIFY, "PUT", "/api/settings/login-lockout", original, admin["token"]), 200)
    # 先排隊停用，再送 Laravel 寫入；帳號列鎖釋放後舊登入不能寫入。
    fresh = expect(call(LARAVEL, "POST", "/api/auth/login", {"email": member["email"], "password": "Abcd1234"}), 200)["access_token"]
    race_title = "ticket39-race-" + nonce
    holder = locked_sql(f"SELECT id FROM accounts WHERE id={member['id']} FOR UPDATE")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            disable = pool.submit(call, FASTIFY, "PUT", f"/api/members/{member['id']}/status", {"status": "disabled"}, admin["token"])
            wait_lock()
            write = pool.submit(call, LARAVEL, "POST", "/api/articles", {"title": race_title, "body": "不可提交"}, fresh)
            wait_lock(2)
            finish_lock(holder)
            expect(disable.result(), 200)
            expect(write.result(), 403, "E0102")
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate()
    check(sql(f"SELECT count(*) FROM articles WHERE title='{race_title}'") == "0", "停用後的 Laravel 寫入仍提交")
    expect(call(FASTIFY, "PUT", f"/api/members/{member['id']}/status", {"status": "active"}, admin["token"]), 200)
    print("PASS Ticket 39 Fastify 管理 HTTP/SQL 驗收")


if __name__ == "__main__":
    main()
