"""CQ10：沿用隔離 guard，以公開 HTTP 與真實 DB 鎖驗證 Symfony 內容整理。"""

import json
import os
import secrets
import selectors
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from zoneinfo import ZoneInfo

import ticket57_http as shared


# row 保留完整 DB 時間精度及所有欄位，避免秒級 API 時間掩蓋同值寫入。
def row(table, identifier):
    shared.check(table in ("articles", "comments") and type(identifier) is int,
                 "只准讀取內容測資")
    return json.loads(shared.sql(f"SELECT to_jsonb(t) FROM {table} t WHERE id={identifier}"))


# create_article 經公開端點建立唯一文章，DB 僅用來定位已提交的測資。
def create_article(backend, actor, title):
    shared.check(shared.expect(shared.request(backend, "POST", "/api/articles",
                 {"title": title, "body": "原始文章"}, actor["token"]), 201) == {}, "建立文章 data")
    return shared.record_id("articles", "title", title)


# create_comment 經父文章端點建立留言，讓後續跨後端讀取具有真實寫入來源。
def create_comment(backend, actor, article_id, body):
    shared.check(shared.expect(shared.request(backend, "POST", f"/api/articles/{article_id}/comments",
                 {"body": body}, actor["token"]), 201) == {}, "建立留言 data")
    return shared.record_id("comments", "body", body)


# login 取得同帳號在另一後端的合法 token，不把後端綁定 JWT 混用。
def login(backend, actor):
    result = shared.expect(shared.request(backend, "POST", "/api/auth/login",
                           {"email": actor["email"], "password": "Abcd1234"}), 200)
    shared.check(result["user"]["id"] == actor["id"], "跨後端登入帳號不符")
    return result["access_token"]


# exact_list 比對實際權限範圍、排序及 total；舊測資不能讓只含一筆的斷言假通過。
def exact_list(path, token, table, where, expected_own=None):
    expected = json.loads(shared.sql(
        f"SELECT json_build_object('total',(SELECT count(*) FROM {table} {where}),"
        "'ids',(SELECT coalesce(json_agg(id),'[]') FROM "
        f"(SELECT id FROM {table} {where} ORDER BY created_at DESC,id DESC LIMIT 100) page))"))
    identifiers, total = expected["ids"], expected["total"]
    if expected_own is not None:
        shared.check(set(identifiers) == set(expected_own) and total == len(expected_own), "fixture 的預期集合不符")
    separator = "&" if "?" in path else "?"
    data = shared.expect(shared.request("symfony", "GET", path + separator + "page=1&per_page=100",
                                       token=token), 200)
    shared.check(set(data) == {"list", "pagination"} and
                 [item["id"] for item in data["list"]] == identifiers and
                 data["pagination"] == {"page": 1, "per_page": 100, "total": total,
                                        "total_pages": (total + 99) // 100},
                 "列表範圍、排序或分頁計數不符")
    for item in data["list"]:
        shared.check(set(item["author"]) == {"id", "nickname"}, "作者資料洩漏非公開欄位")
    # 合法超頁仍回原頁碼及真實 total，不以空頁代表資料不存在。
    page = (total + 99) // 100 + 1
    empty = shared.expect(shared.request("symfony", "GET",
                          path + separator + f"page={page}&per_page=100", token=token), 200)
    shared.check(empty == {"list": [], "pagination": {"page": page, "per_page": 100,
                 "total": total, "total_pages": (total + 99) // 100}}, "超頁契約")
    return data["list"]


# scoped_cases 區別公開、本人與管理範圍，並保留父文章隱藏時的作者專用留言讀取。
def scoped_cases(nonce):
    owner = shared.user("symfony", nonce + "s", "member")
    other = shared.user("symfony", nonce + "s", "other")
    admin = shared.user("symfony", nonce + "s", "admin")
    article_id = create_article("symfony", owner, f"cq10-{nonce}-scope")
    other_article = create_article("symfony", other, f"cq10-{nonce}-other")
    path = f"/api/articles/{article_id}"
    visible = create_comment("symfony", other, article_id, f"cq10-{nonce}-visible")
    hidden = create_comment("symfony", owner, article_id, f"cq10-{nonce}-hidden")
    admin_comment = create_comment("symfony", admin, article_id, f"cq10-{nonce}-admin")
    hidden_path = f"/api/comments/{hidden}"
    visible_path = f"/api/comments/{visible}"
    shared.expect(shared.request("symfony", "PUT", hidden_path + "/visibility",
                  {"visibility": "hidden", "version": 1}, owner["token"]), 200)

    # 公開父文章仍不能透過列表看到自己的個別下架留言；專用詳情則可讀。
    comment_where = f"WHERE article_id={article_id}"
    exact_list(path + "/comments", owner["token"], "comments", comment_where + " AND visibility='visible'",
               [visible, admin_comment])
    exact_list(path + "/comments", admin["token"], "comments", comment_where,
               [visible, hidden, admin_comment])
    shared.expect(shared.request("symfony", "GET", visible_path, token=owner["token"]), 403, "E0003")
    shared.expect(shared.request("symfony", "GET", hidden_path, token=other["token"]), 404, "E0004")
    shared.expect(shared.request("symfony", "GET", hidden_path), 401, "E0002")

    # 下架後的文章預覽只含 visible 留言；留言作者不因此取得父文章預覽權。
    shared.expect(shared.request("symfony", "PUT", path + "/visibility",
                  {"visibility": "hidden", "version": 1}, owner["token"]), 200)
    for token in (None, other["token"]):
        shared.expect(shared.request("symfony", "GET", path, token=token), 404, "E0004")
        shared.expect(shared.request("symfony", "GET", path + "/comments?page=1&per_page=100", token=token),
                      404, "E0004")
    exact_list(path + "/comments", owner["token"], "comments", comment_where + " AND visibility='visible'",
               [visible, admin_comment])
    exact_list(path + "/comments", admin["token"], "comments", comment_where,
               [visible, hidden, admin_comment])
    own = exact_list("/api/me/comments", owner["token"], "comments", f"WHERE author_id={owner['id']}", [hidden])
    detail = shared.expect(shared.request("symfony", "GET", hidden_path, token=owner["token"]), 200)
    shared.check(own == [detail] and detail["article_id"] == article_id and detail["can_restore"] is True,
                 "父文章下架後遺失自己的下架留言或擴張欄位")
    # 非父文章作者仍能讀自己的留言，但專用回應不得夾帶無權讀取的父文章內容。
    comment_fields = {"id", "body", "author", "article_id", "version", "created_backend",
                      "created_at", "updated_at", "visibility", "can_restore"}
    other_own = exact_list("/api/me/comments", other["token"], "comments", f"WHERE author_id={other['id']}", [visible])
    other_detail = shared.expect(shared.request("symfony", "GET", visible_path, token=other["token"]), 200)
    shared.check(other_own == [other_detail] and set(other_detail) == comment_fields and
                 other_detail["article_id"] == article_id and other_detail["visibility"] == "visible" and
                 other_detail["body"] == f"cq10-{nonce}-visible", "父文章不可讀時自己的 visible 留言詳情不符")
    shared.expect(shared.request("symfony", "GET", path, token=other["token"]), 404, "E0004")
    exact_list("/api/me/articles", owner["token"], "articles", f"WHERE author_id={owner['id']}", [article_id])
    exact_list("/api/me/articles", other["token"], "articles", f"WHERE author_id={other['id']}", [other_article])
    for token in (None, owner["token"], admin["token"]):
        exact_list("/api/articles", token, "articles", "WHERE visibility='visible'")
    exact_list("/api/articles?filters%5Bscope%5D=management", admin["token"], "articles", "")
    exact_list("/api/comments", admin["token"], "comments", "")

    # 同時含多種錯誤時逐層排除，確認認證／角色、格式、可讀性、權限、版本的先後。
    bad_management = "/api/articles?filters%5Bscope%5D=management&page=bad&per_page=100"
    for token, status, code in ((None, 401, "E0002"), (other["token"], 403, "E0003")):
        shared.expect(shared.request("symfony", "GET", bad_management, token=token), status, code)
        shared.expect(shared.request("symfony", "GET", "/api/comments?page=bad&per_page=100", token=token),
                      status, code)
    shared.expect(shared.request("symfony", "GET", bad_management, token=admin["token"]), 422, "E0001",
                  {"page": ["欄位格式或範圍不正確"]})
    for resource_path, body, table, identifier in (
            (path, {"title": "權限檢查", "body": "權限檢查", "version": 1}, "articles", article_id),
            (hidden_path, {"body": "權限檢查", "version": 1}, "comments", hidden)):
        before = row(table, identifier)
        invalid = dict(body, version=0)
        shared.expect(shared.request("symfony", "PUT", resource_path, invalid), 401, "E0002")
        shared.expect(shared.request("symfony", "PUT", resource_path, invalid, other["token"]),
                      422, "E0001", {"version": ["欄位格式或範圍不正確"]})
        shared.expect(shared.request("symfony", "PUT", resource_path, body, other["token"]), 404, "E0004")
        shared.expect(shared.request("symfony", "PUT", resource_path, body, admin["token"]), 403, "E0003")
        shared.check(row(table, identifier) == before, "拒絕流程留下部分寫入")
    shared.expect(shared.request("symfony", "PUT", f"/api/articles/{other_article}",
                  {"title": "他人", "body": "他人", "version": 2}, owner["token"]), 403, "E0003")

    # 父文章下架禁止任何內文修改，包括管理員自己的留言；合法可見性操作仍可完成。
    for identifier, actor in ((hidden, owner), (visible, other), (admin_comment, admin)):
        comment_path = f"/api/comments/{identifier}"
        before = row("comments", identifier)
        shared.expect(shared.request("symfony", "PUT", comment_path,
                      {"body": "父文章下架不可修改", "version": 999}, actor["token"]), 403, "E0003")
        shared.check(row("comments", identifier) == before, "下架父文章仍改寫留言")
        target = "visible" if before["visibility"] == "hidden" else "hidden"
        shared.expect(shared.request("symfony", "PUT", comment_path + "/visibility",
                      {"visibility": target, "version": before["version"]}, actor["token"]), 200)
        shared.check(row("comments", identifier)["visibility"] == target, "父文章下架錯誤阻止可見性操作")
    # 個別留言也下架後作者例外仍成立；此時父文章與其他留言的可讀性不能隨之放寬。
    other_own = exact_list("/api/me/comments", other["token"], "comments", f"WHERE author_id={other['id']}", [visible])
    other_detail = shared.expect(shared.request("symfony", "GET", visible_path, token=other["token"]), 200)
    shared.check(other_own == [other_detail] and set(other_detail) == comment_fields and
                 other_detail["article_id"] == article_id and other_detail["visibility"] == "hidden" and
                 other_detail["can_restore"] is True and other_detail["body"] == f"cq10-{nonce}-visible",
                 "父文章不可讀時自己的 hidden 留言詳情不符")
    shared.expect(shared.request("symfony", "GET", path, token=other["token"]), 404, "E0004")
    shared.expect(shared.request("symfony", "GET", path + "/comments?page=1&per_page=100", token=other["token"]),
                  404, "E0004")
    for actor, status, code in ((owner, 403, "E0003"), (admin, 403, "E0003"), (other, 404, "E0004")):
        count = shared.sql(f"SELECT count(*) FROM comments WHERE article_id={article_id}")
        shared.expect(shared.request("symfony", "POST", path + "/comments",
                      {"body": "父文章下架不可新增"}, actor["token"]), status, code)
        shared.check(shared.sql(f"SELECT count(*) FROM comments WHERE article_id={article_id}") == count,
                     "下架父文章新增留言留下資料")
    comments_before = shared.sql(f"SELECT json_agg(to_jsonb(c) ORDER BY id) FROM comments c WHERE article_id={article_id}")
    shared.expect(shared.request("symfony", "PUT", path + "/visibility",
                  {"visibility": "visible", "version": 2}, owner["token"]), 200)
    shared.check(shared.sql(f"SELECT json_agg(to_jsonb(c) ORDER BY id) FROM comments c WHERE article_id={article_id}")
                 == comments_before, "恢復文章改變個別留言狀態")
    print("PASS CQ10 scoped：精確列表、預覽、作者例外、錯誤順序與父文章限制", flush=True)


# changed 核對真實變更的版本與微秒時間，不能只靠秒級顯示判斷更新。
def changed(before, after, message):
    shared.check(after["version"] == before["version"] + 1 and
                 datetime.fromisoformat(after["updated_at"]) > datetime.fromisoformat(before["updated_at"]), message)


# lifecycle_cases 對文章與留言重播同值、過期、作者下架及管理員接管／恢復的完整狀態。
def lifecycle_cases(nonce):
    owner = shared.user("symfony", nonce + "l", "member")
    admin = shared.user("symfony", nonce + "l", "admin")
    peer_token = login("aspnet-core", owner)
    parent = create_article("symfony", owner, f"cq10-{nonce}-parent")
    article = create_article("symfony", owner, f"cq10-{nonce}-lifecycle")
    comment = create_comment("symfony", owner, parent, f"cq10-{nonce}-lifecycle-comment")
    for table, identifier in (("articles", article), ("comments", comment)):
        path = f"/api/{table}/{identifier}"
        before = row(table, identifier)
        body = {"body": before["body"], "version": before["version"]}
        if table == "articles":
            body["title"] = before["title"]
        shared.check(shared.expect(shared.request("symfony", "PUT", path, body, owner["token"]), 200) == {},
                     "同值寫入 data")
        shared.check(row(table, identifier) == before, "同值寫入改變完整資料列")
        body["body"] = "第一次實際修改"
        shared.expect(shared.request("symfony", "PUT", path, body, owner["token"]), 200)
        after = row(table, identifier)
        changed(before, after, "內容修改未增加版本或時間")
        shared.expect(shared.request("symfony", "PUT", path, body, owner["token"]), 409, "E0201")
        shared.check(row(table, identifier) == after, "過期同值改變資料")
        shared.check(shared.expect(shared.request("aspnet-core", "GET", path, token=peer_token), 200) ==
                     shared.expect(shared.request("symfony", "GET", path, token=owner["token"]), 200),
                     "跨後端修改讀回不符")

        # 作者自己的下架內容可修改，但不自動恢復；留言的父文章此時維持公開。
        shared.expect(shared.request("symfony", "PUT", path + "/visibility",
                      {"visibility": "hidden", "version": after["version"]}, owner["token"]), 200)
        before = row(table, identifier)
        body.update(body="下架後修改", version=before["version"])
        shared.expect(shared.request("symfony", "PUT", path, body, owner["token"]), 200)
        after = row(table, identifier)
        changed(before, after, "下架內容修改版本或時間未更新")
        shared.check(after["visibility"] == "hidden" and after["restore_control"] == "author",
                     "下架編修意外恢復或改變控制權")

        # 管理員重下架他人內容必須接管；作者重下架同值不能奪回恢復權。
        shared.expect(shared.request("symfony", "PUT", path + "/visibility",
                      {"visibility": "hidden", "version": after["version"]}, admin["token"]), 200)
        takeover = row(table, identifier)
        changed(after, takeover, "管理員接管未增加版本與時間")
        shared.check(takeover["restore_control"] == "admin", "管理員接管失敗")
        shared.check(shared.expect(shared.request("symfony", "GET", path, token=owner["token"]), 200)
                     ["can_restore"] is False and
                     shared.expect(shared.request("symfony", "GET", path, token=admin["token"]), 200)
                     ["can_restore"] is True, "接管後恢復資格不符")
        shared.expect(shared.request("symfony", "PUT", path + "/visibility",
                      {"visibility": "hidden", "version": takeover["version"]}, owner["token"]), 200)
        shared.check(row(table, identifier) == takeover, "作者重下架更動管理員控制或同值時間")
        for version in (1, takeover["version"]):
            shared.expect(shared.request("symfony", "PUT", path + "/visibility",
                          {"visibility": "visible", "version": version}, owner["token"]), 403, "E0003")
            shared.check(row(table, identifier) == takeover, "拒絕恢復留下資料變更")
        shared.expect(shared.request("symfony", "PUT", path + "/visibility",
                      {"visibility": "visible", "version": takeover["version"]}, admin["token"]), 200)
        restored = row(table, identifier)
        changed(takeover, restored, "管理員恢復版本或時間不符")
        shared.check(restored["visibility"] == "visible" and restored["restore_control"] is None,
                     "管理員恢復未清除控制者")
        shared.check(shared.expect(shared.request("aspnet-core", "GET", path, token=peer_token), 200) ==
                     shared.expect(shared.request("symfony", "GET", path, token=owner["token"]), 200),
                     "跨後端恢復讀回不符")

    # 管理員操作自己的內容使用作者控制，不能誤當成接管別人的內容。
    own_article = create_article("symfony", admin, f"cq10-{nonce}-admin-own")
    own_comment = create_comment("symfony", admin, parent, f"cq10-{nonce}-admin-own-comment")
    for table, identifier in (("articles", own_article), ("comments", own_comment)):
        path = f"/api/{table}/{identifier}/visibility"
        shared.expect(shared.request("symfony", "PUT", path,
                      {"visibility": "hidden", "version": 1}, admin["token"]), 200)
        hidden = row(table, identifier)
        shared.check(hidden["restore_control"] == "author" and hidden["version"] == 2, "管理員自己下架控制者")
        shared.expect(shared.request("symfony", "PUT", path,
                      {"visibility": "hidden", "version": 2}, admin["token"]), 200)
        shared.check(row(table, identifier) == hidden, "管理員自己重下架非同值")
        shared.expect(shared.request("symfony", "PUT", path,
                      {"visibility": "visible", "version": 2}, admin["token"]), 200)
        shared.check(row(table, identifier)["restore_control"] is None, "管理員自己恢復未清除控制者")
    print("PASS CQ10 lifecycle：文章／留言同值、過期、下架編修、恢復接管與跨後端讀回", flush=True)


# version_cases 真正同時送出兩後端的同版本更新，勝者由提交結果決定。
def version_cases(nonce):
    owner = shared.user("symfony", nonce + "v", "member")
    peer_token = login("aspnet-core", owner)
    article = create_article("symfony", owner, f"cq10-{nonce}-version")
    comment = create_comment("symfony", owner, article, f"cq10-{nonce}-version-comment")
    for table, identifier in (("articles", article), ("comments", comment)):
        before = row(table, identifier)
        path = f"/api/{table}/{identifier}"
        barrier = Barrier(2)
        bodies = [{"body": text, "version": 1} for text in ("Symfony 競爭值", "ASP.NET Core 競爭值")]
        if table == "articles":
            for body in bodies:
                body["title"] = before["title"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(shared.synchronized_request, backend, "PUT", path, body, token, barrier)
                       for backend, body, token in zip(("symfony", "aspnet-core"), bodies,
                                                     (owner["token"], peer_token))]
            results = [future.result(timeout=20) for future in futures]
        shared.check(sorted(result[0] for result in results) == [200, 409], "跨後端版本競爭必須一成功一衝突")
        for result in results:
            shared.expect(result, result[0], "E0201" if result[0] == 409 else None)
        winner = next(index for index, result in enumerate(results) if result[0] == 200)
        committed = row(table, identifier)
        changed(before, committed, "跨後端競爭版本或時間不符")
        shared.check(committed["body"] == bodies[winner]["body"] and committed["created_backend"] == "symfony",
                     "競爭 DB 內容不等於成功者或建立來源被改寫")
        primary = shared.expect(shared.request("symfony", "GET", path, token=owner["token"]), 200)
        peer = shared.expect(shared.request("aspnet-core", "GET", path, token=peer_token), 200)
        # psql owner 使用 UTC，API 契約使用臺北秒級文字；原始 DB 精度仍保留在 changed／同值斷言。
        display_time = datetime.fromisoformat(committed["updated_at"]).astimezone(ZoneInfo("Asia/Taipei"))
        shared.check(primary == peer and primary["body"] == committed["body"] and primary["version"] == 2 and
                     primary["updated_at"] == display_time.strftime("%Y-%m-%d %H:%M:%S"),
                     "競爭提交時間、版本或跨後端讀回不一致")
    print("PASS CQ10 version：Symfony／ASP.NET Core 文章及留言同步同版本競爭", flush=True)


# wait_line 只等原生 psql 的明確輸出標記，避免任意等待時間充當鎖已取得的證據。
def wait_line(process, expected):
    deadline = time.monotonic() + 5
    output = b""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            shared.check(selector.select(max(0, deadline - time.monotonic())), "psql 取得父文章鎖逾時")
            chunk = os.read(process.stdout.fileno(), 4096)
            shared.check(chunk != b"", "psql 在鎖定前退出")
            output += chunk
            if expected.encode() in output.splitlines():
                return
    raise AssertionError("psql 未確認父文章鎖")


# blocked_parent 找出實際等待父文章 FOR UPDATE 的連線，鎖鏈必須連到本案例 holder。
def blocked_parent(holder_pid, previous=()):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        waiting = json.loads(shared.sql(
            "SELECT coalesce(json_agg(json_build_object('pid',pid,'blockers',pg_blocking_pids(pid))),'[]') "
            "FROM pg_stat_activity WHERE datname='articles' AND usename='articles_app' "
            "AND state='active' AND wait_event_type='Lock' AND query ~* 'SELECT.*articles.*FOR UPDATE'"))
        for item in waiting:
            if item["pid"] not in previous and any(pid in (holder_pid, *previous) for pid in item["blockers"]):
                return item["pid"]
        time.sleep(0.025)
    raise AssertionError("未觀察到兩個不同帳號的父文章實際鎖等待")


# parent_overlap 用外部唯讀列鎖控制提交順序，兩個 API 的父檢查與留言寫入仍由原交易完成。
def parent_overlap(nonce, hide_backend, comment_backend, operation, comment_first):
    suffix = f"{hide_backend}-{operation}-{int(comment_first)}"
    owner = shared.user(hide_backend, nonce + suffix, "member")
    commenter = shared.user(comment_backend, nonce + suffix, "other")
    article = create_article(hide_backend, owner, f"cq10-{nonce}-{suffix}")
    path = f"/api/articles/{article}"
    original = create_comment(comment_backend, commenter, article, f"cq10-{nonce}-{suffix}-original")
    before_article, before_comment = row("articles", article), row("comments", original)
    body = f"cq10-{nonce}-{suffix}-write"
    hide_args = (hide_backend, "PUT", path + "/visibility", {"visibility": "hidden", "version": 1}, owner["token"])
    comment_args = ((comment_backend, "POST", path + "/comments", {"body": body}, commenter["token"])
                    if operation == "create" else
                    (comment_backend, "PUT", f"/api/comments/{original}", {"body": body, "version": 1}, commenter["token"]))
    appname = f"cq10-{nonce}-{suffix}"
    shared.check(shared.SAFE, "holder 必須在 guard 後執行")
    command = ["docker", "compose", "--profile", "*", "exec", "-T", "-e", "PGAPPNAME=" + appname,
               "db", "psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1",
               "-U", "articles_owner", "-d", "articles"]
    holder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, bufsize=1)
    holder_pid = None
    pool = ThreadPoolExecutor(max_workers=2)
    futures = []
    try:
        # 只鎖本案例父列，不修改資料、全域時間、trigger 或管理設定。
        holder.stdin.write("BEGIN;\nSET LOCAL idle_in_transaction_session_timeout='20s';\n"
                           f"SELECT id FROM articles WHERE id={article} FOR UPDATE;\n\\echo cq10-locked\n")
        holder.stdin.flush()
        wait_line(holder, "cq10-locked")
        holder_pid = int(shared.sql(f"SELECT pid FROM pg_stat_activity WHERE application_name='{appname}'"))
        first_args, second_args = ((comment_args, hide_args) if comment_first else (hide_args, comment_args))
        futures.append(pool.submit(shared.request, *first_args))
        first_pid = blocked_parent(holder_pid)
        futures.append(pool.submit(shared.request, *second_args))
        second_pid = blocked_parent(holder_pid, (first_pid,))
        shared.check(first_pid != second_pid and not any(future.done() for future in futures),
                     "兩個 API 必須先同時阻塞在父列鎖鏈")
        # 釋放純鎖交易後才等待 API 提交，避免測試自行造成死鎖。
        holder.stdin.write("ROLLBACK;\n\\q\n")
        holder.stdin.flush()
        results = [future.result(timeout=20) for future in futures]
        comment_result, hide_result = results if comment_first else results[::-1]
        shared.expect(hide_result, 200)
        status = (201 if operation == "create" else 200) if comment_first else (404 if operation == "create" else 403)
        shared.expect(comment_result, status, None if comment_first else ("E0004" if operation == "create" else "E0003"))
        committed_article = row("articles", article)
        changed(before_article, committed_article, "父文章競爭下架版本或時間不符")
        shared.check(committed_article["visibility"] == "hidden" and committed_article["restore_control"] == "author",
                     "父文章競爭最終狀態不符")
        committed_comment = row("comments", original)
        comment_ids = [original]
        if operation == "create":
            shared.check(committed_comment == before_comment and
                         shared.sql(f"SELECT count(*) FROM comments WHERE article_id={article}") ==
                         ("2" if comment_first else "1") and
                         shared.sql(f"SELECT count(*) FROM comments WHERE article_id={article} AND body='{body}'") ==
                         ("1" if comment_first else "0"), "留言建立競爭有遺失或部分寫入")
            if comment_first:
                created_id = shared.record_id("comments", "body", body)
                created = row("comments", created_id)
                shared.check(created["version"] == 1 and created["created_at"] == created["updated_at"] and
                             created["body"] == body and created["author_id"] == commenter["id"] and
                             created["article_id"] == article and created["created_backend"] == comment_backend and
                             created["visibility"] == "visible" and created["restore_control"] is None,
                             "競爭成功新增留言的版本、來源或狀態不符")
                comment_ids.append(created_id)
        elif comment_first:
            changed(before_comment, committed_comment, "留言先編修未提交版本與時間")
            shared.check(committed_comment["body"] == body and committed_comment["visibility"] == "visible",
                         "留言先編修內容不符")
        else:
            shared.check(committed_comment == before_comment, "父文章先下架卻留下部分留言編修")

        # 提交後以同帳號的各後端 token 讀回，涵蓋兩種建立來源與編修／拒絕後的共同資料。
        owner_peer = login(comment_backend, owner)
        primary = shared.expect(shared.request(hide_backend, "GET", path, token=owner["token"]), 200)
        peer = shared.expect(shared.request(comment_backend, "GET", path, token=owner_peer), 200)
        shared.check(primary == peer and primary["created_backend"] == hide_backend and
                     primary["visibility"] == "hidden" and primary["version"] == committed_article["version"],
                     "競爭文章跨後端讀回不符")
        commenter_peer = login(hide_backend, commenter)
        for identifier in comment_ids:
            comment_path = f"/api/comments/{identifier}"
            primary = shared.expect(shared.request(comment_backend, "GET", comment_path, token=commenter["token"]), 200)
            peer = shared.expect(shared.request(hide_backend, "GET", comment_path, token=commenter_peer), 200)
            persisted = row("comments", identifier)
            shared.check(primary == peer and primary["body"] == persisted["body"] and
                         primary["version"] == persisted["version"] and primary["created_backend"] == comment_backend,
                         "競爭留言跨後端讀回不符")
        print(f"PASS CQ10 parent-lock：{hide_backend} hide／{comment_backend} {operation}／"
              f"{'comment-first' if comment_first else 'hide-first'}；兩個父列等待已觀察", flush=True)
    finally:
        # 任一斷言失敗也先撤回 holder；只終止本案例連線，再收齊 HTTP 工作及管線。
        try:
            if holder.poll() is None:
                holder.stdin.write("ROLLBACK;\n\\q\n")
                holder.stdin.flush()
            holder.communicate(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            if holder_pid is not None:
                shared.sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid={holder_pid} "
                           f"AND application_name='{appname}'")
            if holder.poll() is None:
                holder.terminate()
            holder.communicate(timeout=5)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            shared.check(shared.sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{appname}'") == "0",
                         "父文章 holder 連線未清理")


# main 一律先走原 guard；共用 suite 與新增缺口都成功才回報本票 HTTP 範圍完成。
def main():
    shared.check(sys.argv[1:] == [], "CQ10 driver 不接受改寫測試目標或略過案例")
    shared.guard()
    # 首筆 fixture 寫入前拒絕任何同名未知 DDL，避免先改資料才發現殘留。
    shared.clear_boundary_trigger()
    nonce = secrets.token_hex(5)
    shared.backend_cases("symfony", nonce)
    shared.contract_cases("symfony", nonce)
    shared.database_cases("symfony", nonce)
    shared.ordered_cases("symfony", nonce)
    scoped_cases(nonce)
    lifecycle_cases(nonce)
    version_cases(nonce)
    for hide_backend, comment_backend in (("symfony", "aspnet-core"), ("aspnet-core", "symfony")):
        for operation in ("create", "edit"):
            for comment_first in (False, True):
                parent_overlap(nonce, hide_backend, comment_backend, operation, comment_first)
    print("PASS CQ10 HTTP：共用 Symfony 22 操作／contract／database／18 ordered 與新增內容回歸", flush=True)
    print("範圍限制：搜尋 U+0000 政策未決；完整 invalid 矩陣、查詢計畫、瀏覽器及正式 review 由 Main 另驗。")


if __name__ == "__main__":
    main()
