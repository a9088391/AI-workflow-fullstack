"""Ticket 27：僅在 ticket25_test 隔離 Compose 驗證 Gin 管理與鎖定契約。"""

import base64
import os
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from ticket25_http import GIN, LARAVEL, call, check, expect, sign_token, sql
from ticket26_http import finish_lock, guard, literal, locked_sql, wait_lock


# actor 經公開註冊取得各後端 session，角色只在已驗證的隔離 DB 設定。
def actor(nonce, tag, nickname, admin=False):
    email = f"t27{nonce}{tag}@example.test"
    expect(call(GIN, "POST", "/api/auth/register",
                {"email": email, "password": "Abc1234", "nickname": nickname}), 201)
    data = expect(call(GIN, "POST", "/api/auth/login",
                       {"email": email, "password": "Abc1234"}), 200)
    result = {"id": data["user"]["id"], "email": email, "gin": data["access_token"]}
    if admin:
        sql(f"UPDATE accounts SET role='admin' WHERE id={int(result['id'])}")
    else:
        result["laravel"] = expect(call(LARAVEL, "POST", "/api/auth/login",
                                         {"email": email, "password": "Abc1234"}), 200)["access_token"]
    return result


# ids 由隔離 DB 取得完整排序集合，供全域列表與既有資料並存時精確比對。
def ids(table, where=""):
    check(table in ("accounts", "articles", "comments"), "未允許的列表資料表")
    order = "created_at DESC,id DESC" if table == "accounts" else f"{table}.created_at DESC,{table}.id DESC"
    raw = sql(f"SELECT id FROM {table} {where} ORDER BY {order}")
    return [int(value) for value in raw.splitlines()] if raw else []


# listed 逐頁核對完整 ID、總數及總頁數，包含合法超頁而非只檢查 HTTP 200。
def listed(base, path, token, expected, per_page=2, pages=(1, 2, 999999)):
    for page in pages:
        separator = "&" if "?" in path else "?"
        data = expect(call(base, "GET", f"{path}{separator}page={page}&per_page={per_page}", token=token), 200)
        check([row["id"] for row in data["list"]] == expected[(page - 1) * per_page:page * per_page],
              f"列表 ID/排序不符：{path} 第 {page} 頁")
        check(data["pagination"] == {"page": page, "per_page": per_page,
                                      "total": len(expected),
                                      "total_pages": (len(expected) + per_page - 1) // per_page},
              f"列表精確分頁不符：{path} 第 {page} 頁")
        if page == 1:
            first = data
    return first


# page_numbers 同時抽查開頭、第二頁、最後有資料頁與遠超總數的合法頁。
def page_numbers(expected, per_page):
    last = max(1, (len(expected) + per_page - 1) // per_page)
    return sorted({1, 2, last, 999999})


# keyword 以原始 URL 編碼傳送，避免測試客戶端先改寫特殊字元或 NUL。
def keyword(value):
    return "&filters%5Bkeyword%5D=" + quote(value, safe="")


# expected_search 以契約定義的 ASCII 摺疊和 literal contains 查隔離資料，納入前次執行資料。
def expected_search(value):
    folded = value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    if not folded or "\x00" in folded:
        return ids("accounts", "WHERE role='member'") if not folded else []
    pattern = literal(folded)
    return ids("accounts", "WHERE role='member' AND ("
               "strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')," + pattern + ")>0 OR "
               "strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')," + pattern + ")>0)")


# search_cases 將 fixture 的命中與刻意不命中一併比對，覆蓋 literal/ASCII/Unicode 邊界。
def search_cases(admin, first, second, third, fourth):
    base = "/api/members?page=1&per_page=20"
    # 逐一核對特殊字元和大小寫，並明列本次測資應命中/不命中的帳號。
    cases = [
        ("a_b", first["id"], third["id"]), ("%", first["id"], third["id"]),
        ("\\", first["id"], third["id"]), ("É", first["id"], second["id"]),
        ("é", second["id"], first["id"]), ("中", first["id"], second["id"]),
        ("b", third["id"], None), ("A1", third["id"], first["id"]),
        ("普通會員", second["id"], first["id"]), ("不存在t27", None, first["id"]),
        ("\x00", None, first["id"]), ("x" * 254, None, first["id"]),
    ]
    for term, must_include, must_exclude in cases:
        path = base + keyword(term)
        data = expect(call(GIN, "GET", path, token=admin["gin"]), 200)
        matches = expected_search(term)
        actual = [row["id"] for row in data["list"]]
        check(actual == matches[:20],
              f"字面搜尋結果不符：{term!r}，實際 {[row['id'] for row in data['list']]}")
        check(data["pagination"] == {"page": 1, "per_page": 20, "total": len(matches),
                                      "total_pages": (len(matches) + 19) // 20}, f"搜尋計數不符：{term!r}")
        check((must_include is None or must_include in matches)
              and (must_exclude is None or must_exclude not in matches), f"搜尋正反 fixture 不符：{term!r}")
    # U+0020 才移除；其他 Unicode 空白仍須參與精確比較。
    trimmed = expect(call(GIN, "GET", base + keyword("  普通會員  "), token=admin["gin"]), 200)
    check([row["id"] for row in trimmed["list"]] == expected_search("普通會員")[:20], "U+0020 trim 不符")
    empty = expect(call(GIN, "GET", base + keyword("   "), token=admin["gin"]), 200)
    check(empty["pagination"]["total"] == len(ids("accounts", "WHERE role='member'")), "空搜尋應不篩選")
    check(fourth["id"] not in [first["id"], second["id"], third["id"]], "搜尋 fixture 重複")
    email_matches = expected_search(first["email"].upper())
    check(email_matches == [first["id"]], "Email 字面包含/ASCII 摺疊測資不符")
    listed(GIN, "/api/members?filters%5Bkeyword%5D=" + quote(first["email"].upper(), safe=""),
           admin["gin"], email_matches, 1, (1, 2, 999999))
    broad = expected_search("t27")
    listed(GIN, "/api/members?filters%5Bkeyword%5D=t27", admin["gin"], broad, 2,
           page_numbers(broad, 2))
    for path, field, message in (
        (base + keyword("x" * 255), "filters.keyword", "欄位格式或範圍不正確"),
        (base + "&filters%5Bkeyword%5D=a&filters%5Bkeyword%5D=b", "filters.keyword", "此參數不可重複"),
        (base + "&filters%5Bkeyword%5D%5B%5D=x", "filters.keyword", "欄位型態不正確"),
        (base + "&strange=1", "strange", "不支援此欄位"),
        ("/api/members?per_page=20", "page", "此欄位為必填"),
    ):
        expect(call(GIN, "GET", path, token=admin["gin"]), 422, "E0001", {field: [message]})


# member_cases 覆蓋管理角色先驗、目標隱藏、欄位與狀態寫入的精確錯誤。
def member_cases(admin, member):
    path = f"/api/members/{member['id']}"
    status = path + "/status"
    # 權限先於格式與目標，防止會員從驗證訊息探測後台資源。
    expect(call(GIN, "GET", "/api/members?page=bad&per_page=2", token=member["gin"]), 403, "E0003")
    expect(call(GIN, "GET", path), 401, "E0002")
    expect(call(GIN, "PUT", "/api/members/nope/status", {"status": None}, member["gin"]), 403, "E0003")
    expect(call(GIN, "PUT", status, {"status": "disabled"}), 401, "E0002")
    detail = expect(call(GIN, "GET", path, token=admin["gin"]), 200)
    # 讀取僅回公開管理欄位；管理員 ID 對管理員也視為不存在。
    check(set(detail) == {"id", "email", "nickname", "role", "status", "created_at"}
          and detail["id"] == member["id"] and detail["role"] == "member", "會員詳情欄位")
    for target in (admin["id"], 9007199254740991):
        expect(call(GIN, "GET", f"/api/members/{target}", token=admin["gin"]), 404, "E0004")
        expect(call(GIN, "PUT", f"/api/members/{target}/status",
                    {"status": "disabled"}, admin["gin"]), 404, "E0004")
    for body, fields in (({}, {"status": ["此欄位為必填"]}),
                         ({"status": None}, {"status": ["此欄位不可為 null"]}),
                         ({"status": 1}, {"status": ["欄位型態不正確"]}),
                         ({"status": "paused"}, {"status": ["欄位格式或範圍不正確"]}),
                         ({"status": "active", "extra": 1}, {"extra": ["不支援此欄位"]})):
        expect(call(GIN, "PUT", status, body, admin["gin"]), 422, "E0001", fields)
    expect(call(GIN, "PUT", status, "{", admin["gin"]), 400, "E0005")
    expect(call(GIN, "PUT", "/api/members/nope/status", {"status": "disabled"}, admin["gin"]),
           422, "E0001", {"member_id": ["欄位格式或範圍不正確"]})
    expect(call(GIN, "GET", path + "?x=1", token=admin["gin"]), 422, "E0001", {"x": ["不支援此欄位"]})
    expect(call(GIN, "GET", path, body="{}", token=admin["gin"]), 422, "E0001",
           {"_body": ["此 API 不接受 Request Body"]})
    expect(call(GIN, "PUT", status + "?x=1", {"status": "active"}, admin["gin"]),
           422, "E0001", {"x": ["不支援此欄位"]})


# content_cases 核對公開與明確管理範圍、父文章隱藏留言及恢復權。
def content_cases(admin, member, second, nonce):
    title = "t27 管理文章 " + nonce
    # 正式 HTTP 建立文章及兩種留言，再以作者權限下架形成可見性差異。
    expect(call(GIN, "POST", "/api/articles", {"title": title, "body": "內容"}, member["gin"]), 201)
    article = int(sql(f"SELECT id FROM articles WHERE author_id={member['id']} AND title={literal(title)}"))
    body1, body2 = "t27 公開留言 " + nonce, "t27 隱藏留言 " + nonce
    expect(call(GIN, "POST", f"/api/articles/{article}/comments", {"body": body1}, member["gin"]), 201)
    public_comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body={literal(body1)}"))
    expect(call(GIN, "POST", f"/api/articles/{article}/comments", {"body": body2}, second["gin"]), 201)
    hidden_comment = int(sql(f"SELECT id FROM comments WHERE article_id={article} AND body={literal(body2)}"))
    expect(call(GIN, "PUT", f"/api/comments/{hidden_comment}/visibility",
                {"visibility": "hidden", "version": 1}, second["gin"]), 200)
    expect(call(GIN, "PUT", f"/api/articles/{article}/visibility",
                {"visibility": "hidden", "version": 1}, member["gin"]), 200)
    public_ids = ids("articles", "WHERE articles.visibility='visible'")
    # 公開、管理與管理員預設範圍共用全庫精確排序集合。
    all_ids = ids("articles")
    public = listed(GIN, "/api/articles", None, public_ids, 100, page_numbers(public_ids, 100))
    default_admin = listed(GIN, "/api/articles", admin["gin"], public_ids, 100, page_numbers(public_ids, 100))
    check(article not in [row["id"] for row in public["list"]]
          and default_admin["pagination"]["total"] == len(public_ids), "公開範圍洩漏下架")
    management = "/api/articles?filters%5Bscope%5D=management"
    expect(call(GIN, "GET", management + "&page=bad&per_page=20"), 401, "E0002")
    expect(call(GIN, "GET", management + "&page=bad&per_page=20", token=member["gin"]), 403, "E0003")
    managed = listed(GIN, "/api/articles?filters%5Bscope%5D=management", admin["gin"],
                     all_ids, 100, page_numbers(all_ids, 100))
    check(article in [row["id"] for row in managed["list"]], "管理範圍未含下架")
    expect(call(GIN, "GET", "/api/articles?page=1&per_page=20&filters%5Bscope%5D=other",
                token=admin["gin"]), 422, "E0001", {"filters.scope": ["欄位格式或範圍不正確"]})
    comment_ids = ids("comments")
    # 管理留言可見個別/父文章下架內容，恢復權由目前觀看者計算。
    admin_list = listed(GIN, "/api/comments", admin["gin"], comment_ids, 100,
                        page_numbers(comment_ids, 100))
    check({public_comment, hidden_comment}.issubset(set(comment_ids)), "管理留言漏列父文章/自身下架")
    hidden = next(row for row in admin_list["list"] if row["id"] == hidden_comment)
    public_under_hidden = next(row for row in admin_list["list"] if row["id"] == public_comment)
    check(hidden["visibility"] == "hidden" and hidden["can_restore"] is True
          and hidden["author"] == {"id": second["id"], "nickname": "普通會員é"}
          and public_under_hidden["visibility"] == "visible"
          and public_under_hidden["can_restore"] is False, "管理留言恢復權/作者")
    expect(call(GIN, "GET", "/api/comments?page=bad&per_page=2", token=member["gin"]), 403, "E0003")
    expect(call(GIN, "GET", "/api/comments?page=1&per_page=2&filters%5Bkeyword%5D=x",
                token=admin["gin"]), 422, "E0001", {"filters.keyword": ["不支援此欄位"]})
    expect(call(GIN, "GET", "/api/comments?page=1&per_page=2", body="{}", token=admin["gin"]),
           422, "E0001", {"_body": ["此 API 不接受 Request Body"]})
    return article, public_comment


# status_cases 核對跨後端 session 撤銷、同值副作用及最後提交的完整狀態。
def status_cases(admin, laravel_admin, member, article, comment):
    path = f"/api/members/{member['id']}/status"
    # 先建立兩個 Gin 和一個 Laravel 登入，區分同值與真正停用的撤銷效果。
    other_gin = expect(call(GIN, "POST", "/api/auth/login",
                            {"email": member["email"], "password": "Abc1234"}), 200)["access_token"]
    before = sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL")
    check(expect(call(GIN, "PUT", path, {"status": "active"}, admin["gin"]), 200) == {}, "同值寫入資料")
    check(sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NOT NULL") == before,
          "同值狀態重撤銷")
    sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '1 hour' WHERE id={member['id']}")
    # 停用與恢復都不應改內容或清除原有鎖定期限。
    until = sql(f"SELECT locked_until::text FROM accounts WHERE id={member['id']}")
    content = sql(f"SELECT a.visibility||','||c.visibility FROM articles a JOIN comments c ON c.article_id=a.id WHERE a.id={article} AND c.id={comment}")
    check(expect(call(GIN, "PUT", path, {"status": "disabled"}, admin["gin"]), 200) == {}, "停用回應")
    check(sql(f"SELECT status FROM accounts WHERE id={member['id']}") == "disabled"
          and sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NULL") == "0", "停用與撤銷未原子落地")
    disabled_detail = expect(call(GIN, "GET", f"/api/members/{member['id']}", token=admin["gin"]), 200)
    check(disabled_detail["status"] == "disabled"
          and member["id"] in ids("accounts", "WHERE role='member'"), "停用會員應仍可管理搜尋")
    for base, token in ((GIN, member["gin"]), (GIN, other_gin), (LARAVEL, member["laravel"])):
        expect(call(base, "GET", "/api/me", token=token), 403, "E0102")
    expect(call(GIN, "PUT", path, {"status": "active"}, admin["gin"]), 200)
    for base, token in ((GIN, member["gin"]), (GIN, other_gin), (LARAVEL, member["laravel"])):
        expect(call(base, "GET", "/api/me", token=token), 401, "E0002")
    check(sql(f"SELECT locked_until::text FROM accounts WHERE id={member['id']}") == until
          and sql(f"SELECT a.visibility||','||c.visibility FROM articles a JOIN comments c ON c.article_id=a.id WHERE a.id={article} AND c.id={comment}") == content,
          "恢復改動鎖定或內容")
    # 在同一帳號列前排隊 Gin 再 Laravel；觀察兩者確實等待後才釋放鎖。
    holder = locked_sql(f"SELECT id FROM accounts WHERE id={member['id']} FOR UPDATE", "SELECT 1")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            gin_future = pool.submit(call, GIN, "PUT", path, {"status": "disabled"}, admin["gin"])
            wait_lock(fragment="SELECT id,role,status FROM accounts")
            laravel_future = pool.submit(call, LARAVEL, "PUT", path, {"status": "active"}, laravel_admin["laravel"])
            wait_lock(2)
            finish_lock(holder)
            result_gin = gin_future.result(timeout=12)
            result_laravel = laravel_future.result(timeout=12)
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate()
    expect(result_gin, 200)
    expect(result_laravel, 200)
    check(sql(f"SELECT status FROM accounts WHERE id={member['id']}") == "active"
          and sql(f"SELECT count(*) FROM login_sessions WHERE account_id={member['id']} AND revoked_at IS NULL") == "0",
          "跨後端狀態最後提交或撤銷不符")


# settings_cases 覆蓋欄位驗證、原子清鎖、跨後端完整值及時間計數邊界。
def settings_cases(admin, laravel_admin, member):
    path = "/api/settings/login-lockout"
    original = expect(call(GIN, "GET", path, token=admin["gin"]), 200)
    # 先測角色與解析，再逐欄檢查完整值必填、型態及上下限。
    check(set(original) == {"enabled", "window_minutes", "failure_threshold", "lock_seconds"}, "設定四欄")
    expect(call(GIN, "GET", path), 401, "E0002")
    expect(call(GIN, "PUT", path, "{", member["gin"]), 403, "E0003")
    expect(call(GIN, "GET", path + "?x=1", token=member["gin"]), 403, "E0003")
    expect(call(GIN, "GET", path, body="{}", token=admin["gin"]), 422, "E0001",
           {"_body": ["此 API 不接受 Request Body"]})
    for body, code, fields in ((None, "E0001", {"_body": ["Request Body 必須為 JSON object"]}),
                               ([], "E0001", {"_body": ["Request Body 必須為 JSON object"]}),
                               ("{", "E0005", None)):
        expect(call(GIN, "PUT", path, body, admin["gin"]), 400 if code == "E0005" else 422, code, fields)
    for name in original:
        omitted = {key: value for key, value in original.items() if key != name}
        expect(call(GIN, "PUT", path, omitted, admin["gin"]), 422, "E0001", {name: ["此欄位為必填"]})
        expect(call(GIN, "PUT", path, {**original, name: None}, admin["gin"]), 422, "E0001", {name: ["此欄位不可為 null"]})
        wrong = 1 if name == "enabled" else "1"
        expect(call(GIN, "PUT", path, {**original, name: wrong}, admin["gin"]), 422, "E0001", {name: ["欄位型態不正確"]})
    for name, values in {"window_minutes": (0, 61), "failure_threshold": (0, 101), "lock_seconds": (0, 86401)}.items():
        for value in values:
            expect(call(GIN, "PUT", path, {**original, name: value}, admin["gin"]), 422, "E0001",
                   {name: ["欄位格式或範圍不正確"]})
    expect(call(GIN, "PUT", path, {**original, "extra": 1}, admin["gin"]), 422, "E0001",
           {"extra": ["不支援此欄位"]})
    expect(call(GIN, "PUT", path + "?x=1&x=2", original, admin["gin"]), 422, "E0001",
           {"x": ["此參數不可重複"]})
    check(expect(call(GIN, "GET", path, token=admin["gin"]), 200) == original, "無效設定改變資料")
    try:
        # 真正關閉時同交易清鎖換世代；數值變更與重複關閉不得重做副作用。
        enabled = {"enabled": True, "window_minutes": 5, "failure_threshold": 2, "lock_seconds": 120}
        expect(call(GIN, "PUT", path, enabled, admin["gin"]), 200)
        epoch = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}"))
        status = sql(f"SELECT status FROM accounts WHERE id={member['id']}")
        sql(f"UPDATE accounts SET locked_until=clock_timestamp()+interval '1 hour' WHERE id={member['id']}")
        until = sql(f"SELECT locked_until::text FROM accounts WHERE id={member['id']}")
        numeric = {**enabled, "lock_seconds": 600}
        expect(call(GIN, "PUT", path, numeric, admin["gin"]), 200)
        check(sql(f"SELECT locked_until::text FROM accounts WHERE id={member['id']}") == until,
              "數值改動延長既有鎖定")
        failures = int(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}"))
        off = {**numeric, "enabled": False}
        expect(call(GIN, "PUT", path, off, admin["gin"]), 200)
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={member['id']}") == "t"
              and int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}")) == epoch + 1
              and int(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}")) == failures
              and sql(f"SELECT status FROM accounts WHERE id={member['id']}") == status,
              "關閉未原子清鎖換世代或刪除事件")
        expect(call(GIN, "PUT", path, off, admin["gin"]), 200)
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}")) == epoch + 1,
              "同值關閉重複清計數")
        # 兩端以不同完整設定排隊並觀察設定列鎖等待，最後應是 Laravel 完整值。
        one = {"enabled": False, "window_minutes": 7, "failure_threshold": 11, "lock_seconds": 333}
        two = {"enabled": False, "window_minutes": 9, "failure_threshold": 13, "lock_seconds": 555}
        holder = locked_sql("SELECT id FROM login_lockout_settings WHERE id=1 FOR UPDATE", "SELECT 1")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(call, GIN, "PUT", path, one, admin["gin"])
                wait_lock()
                second = pool.submit(call, LARAVEL, "PUT", path, two, laravel_admin["laravel"])
                wait_lock(2)
                finish_lock(holder)
                expect(first.result(timeout=12), 200)
                expect(second.result(timeout=12), 200)
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.communicate()
        check(expect(call(GIN, "GET", path, token=admin["gin"]), 200) == two,
              "跨後端設定非最後完整提交")
        # 先重開，再讓兩後端同帳號錯密碼碰到門檻；左邊界事件不計。
        expect(call(GIN, "PUT", path, enabled, admin["gin"]), 200)
        sql(f"INSERT INTO login_failures(account_id,failure_epoch,occurred_at) SELECT id,failure_epoch,clock_timestamp()-interval '6 minutes' FROM accounts WHERE id={member['id']}")
        wrong = {"email": member["email"], "password": "Wrong123"}
        count_before = int(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}"))
        expect(call(GIN, "POST", "/api/auth/login", wrong), 401, "E0101")
        locked = call(LARAVEL, "POST", "/api/auth/login", wrong)
        expect(locked, 429, "E0105")
        check(int(sql(f"SELECT count(*) FROM login_failures WHERE account_id={member['id']}")) == count_before + 2,
              "跨後端錯密碼未各記一筆")
        check(1 <= int(locked[2]["Retry-After"]) <= 120, "跨後端門檻缺少剩餘時間")
        prior = sql(f"SELECT locked_until::text FROM accounts WHERE id={member['id']}")
        again = call(GIN, "POST", "/api/auth/login", {"email": member["email"], "password": "Abc1234"})
        expect(again, 429, "E0105")
        check(sql(f"SELECT locked_until::text FROM accounts WHERE id={member['id']}") == prior,
              "鎖定重試延長期限")
        sql(f"UPDATE accounts SET locked_until=clock_timestamp()-interval '1 second' WHERE id={member['id']}")
        before = int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}"))
        expect(call(GIN, "POST", "/api/auth/login", wrong), 401, "E0101")
        check(int(sql(f"SELECT failure_epoch FROM accounts WHERE id={member['id']}")) == before + 1,
              "到期未換計數世代")
        expect(call(LARAVEL, "POST", "/api/auth/login",
                    {"email": member["email"], "password": "Abc1234"}), 200)
        check(sql(f"SELECT locked_until IS NULL FROM accounts WHERE id={member['id']}") == "t", "成功登入未清鎖")
    finally:
        expect(call(GIN, "PUT", path, original, admin["gin"]), 200)


# plans 對照 Gin 送出的 count/list SQL，留下短字、長字、文章及留言的實際計畫。
def plans():
    # 先記隔離資料規模，避免把小表循序掃描直接判作產品缺陷。
    print("PLAN FIXTURE accounts total=" + sql("SELECT count(*) FROM accounts") +
          " members=" + sql("SELECT count(*) FROM accounts WHERE role='member'") +
          " articles=" + sql("SELECT count(*) FROM articles") +
          " comments=" + sql("SELECT count(*) FROM comments"))
    queries = []
    for term in ("中", "A1", "普通會員"):
        # HTTP 會先把關鍵字的 ASCII 大寫轉小寫；計畫參數須對應真正送進查詢的值。
        folded = term.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        base = " FROM accounts WHERE role='member' AND (strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')," + literal(folded) + ")>0 OR strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')," + literal(folded) + ")>0)"
        print(f"PLAN FIXTURE keyword={term} matched=" + sql("SELECT count(*)" + base))
        queries.extend(((f"members {term} count", "SELECT count(*)" + base),
                        (f"members {term} list", "SELECT id,email,nickname,role,status,created_at" + base +
                         " ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET 0")))
    queries.extend((("public articles count", "SELECT count(*) FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible'"),
                    ("public articles list", "SELECT articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.visibility='visible' ORDER BY articles.created_at DESC,articles.id DESC LIMIT 20 OFFSET 0"),
                    ("comments count", "SELECT count(*) FROM comments JOIN accounts ON accounts.id=comments.author_id"),
                    ("comments list", "SELECT comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname FROM comments JOIN accounts ON accounts.id=comments.author_id ORDER BY comments.created_at DESC,comments.id DESC LIMIT 20 OFFSET 0")))
    for name, query in queries:
        print("PLAN " + name + ":\n" + sql("EXPLAIN (ANALYZE, BUFFERS) " + query))
    extension = sql("SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'")
    print("QUERY COUNT source: Gin members/adminComments each use one count and at most one list query; row loop performs no SQL. "
          + ("pg_stat_statements present; per-request attribution unavailable."
             if extension == "1" else "Runtime statement-count instrumentation unavailable (pg_stat_statements absent)."))


# window_boundary 以單一 DB 時刻核對登入計數 SQL 的開左閉右界；HTTP 無法注入同一時鐘。
def window_boundary():
    count = sql("WITH t AS (SELECT clock_timestamp() AS stamp), events AS ("
                "SELECT stamp-interval '5 minutes' AS occurred_at FROM t UNION ALL "
                "SELECT stamp-interval '5 minutes'+interval '1 microsecond' FROM t UNION ALL "
                "SELECT stamp FROM t) SELECT count(*) FROM events,t WHERE "
                "occurred_at>t.stamp-interval '5 minutes' AND occurred_at<=t.stamp")
    check(count == "2", "登入失敗視窗開左閉右 SQL 邊界")
    print("WINDOW SQL exact left/inside/right count=2; HTTP uses near-boundary fixture without clock injection")


# expiry_admin_write 驗證管理員寫入等帳號鎖後過期又停用時，交易內重驗先回到期且不改設定。
def expiry_admin_write(nonce):
    admin = actor(nonce, "expiry", "到期管理員", True)
    # 建立符合八小時 claims 的短剩餘有效期 session，確保 outer auth 先通過。
    path = "/api/settings/login-lockout"
    before = expect(call(GIN, "GET", path, token=admin["gin"]), 200)
    current = int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint"))
    issued = current - 28800 + 5
    expires = issued + 28800
    jti = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    claims = {"iss": "ai-workflow-fullstack:gin", "aud": "ai-workflow-fullstack-api:gin",
              "sub": str(admin["id"]), "jti": jti, "iat": issued, "nbf": issued, "exp": expires}
    sql("INSERT INTO login_sessions (jti,account_id,backend_id,issued_at,expires_at) VALUES ("
        f"{literal(jti)},{admin['id']},'gin',to_timestamp({issued}),to_timestamp({expires}))")
    token = sign_token({"alg": "HS256", "typ": "JWT"}, claims)
    expect(call(GIN, "GET", path, token=token), 200)
    after = {**before, "lock_seconds": before["lock_seconds"] + 1
             if before["lock_seconds"] < 86400 else before["lock_seconds"] - 1}
    holder = locked_sql(f"SELECT id FROM accounts WHERE id={admin['id']} FOR UPDATE",
                        f"UPDATE accounts SET status='disabled' WHERE id={admin['id']}", seconds=8)
    # 讓已進入寫入交易的請求等鎖，提交停用時 JWT 同時超過到期點。
    try:
        check(int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) < expires,
              "受控請求前 JWT 已到期")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(call, GIN, "PUT", path, after, token)
            wait_lock(fragment="SELECT id,role,status FROM accounts")
            finish_lock(holder)
            result = pending.result(timeout=12)
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate()
    check(int(sql("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint")) >= expires
          and sql(f"SELECT status FROM accounts WHERE id={admin['id']}") == "disabled", "到期/停用前提不成立")
    expect(result, 401, "E0002")
    check(expect(call(GIN, "GET", path, token=admin["gin"]), 403, "E0102")
          and sql("SELECT enabled||','||window_minutes||','||failure_threshold||','||lock_seconds "
                  "FROM login_lockout_settings WHERE id=1") ==
          f"{str(before['enabled']).lower()},{before['window_minutes']},{before['failure_threshold']},{before['lock_seconds']}",
          "到期寫入改變設定")


# main 在任何 HTTP/SQL 寫入前重用既有 Docker 身分守衛，支援唯讀 guard 模式。
def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    check(len(sys.argv) <= 2 and (len(sys.argv) == 1 or sys.argv[1] == "--guard-only"), "不支援此參數")
    guard()
    if len(sys.argv) == 2:
        print("PASS ticket25_test guard（DB/Gin/Laravel/NGINX）")
        return
    nonce = secrets.token_hex(5)
    # 每次以 nonce 建立可共存測資，依序驗證管理讀寫、競爭及計畫。
    print("FIXTURE t27" + nonce)
    admin = actor(nonce, "admin", "管理員", True)
    laravel_admin = actor(nonce, "ladm", "後台管理員", True)
    laravel_admin["laravel"] = expect(call(LARAVEL, "POST", "/api/auth/login",
                                           {"email": laravel_admin["email"], "password": "Abc1234"}), 200)["access_token"]
    first = actor(nonce, "first", "A_B%\\É中")
    second = actor(nonce, "second", "普通會員é")
    third = actor(nonce, "third", "A1B")
    fourth = actor(nonce, "fourth", "未搜尋目標")
    member_cases(admin, first)
    listed(GIN, "/api/members", admin["gin"], ids("accounts", "WHERE role='member'"), 100, (1, 999999))
    search_cases(admin, first, second, third, fourth)
    article, comment = content_cases(admin, first, second, nonce)
    status_cases(admin, laravel_admin, first, article, comment)
    settings_cases(admin, laravel_admin, third)
    plans()
    window_boundary()
    expiry_admin_write(nonce)
    print("PASS Ticket 27 Gin administration/lockout HTTP/SQL")


if __name__ == "__main__":
    main()
