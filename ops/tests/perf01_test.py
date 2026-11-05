"""PERF01 離線回歸；只模擬 Docker／HTTP 邊界，絕不連線現有或新資料庫。"""

from contextlib import ExitStack
import base64
import copy
import io
import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import unittest
from unittest.mock import MagicMock, patch

import perf01_http as perf
from perf01_cases import cases, expected_rows, fixture, verify_response


# snapshot 產生有完整 counter 維度的已知樣本，測試不省略 rows／buffers 等度量。
def snapshot(calls=0):
    entry = {"dbid": 100, "userid": 101, "queryid": "42", "toplevel": True, "query": "SELECT $1"}
    entry.update({name: calls if name == "calls" else 0 for name in perf.METRICS})
    return {"reset": "2026-01-01", "dealloc": 0, "entries": [entry] if calls else []}


# log_record 保留原生 JSON 的關聯欄位，讓 caller chain 真正執行事件／bind 解碼。
def log_record(message="duration: 0.1 ms  statement: SELECT 1", line=1, query_id=42):
    return {"user": "articles_app", "dbname": "articles", "session_id": "abc.123", "line_num": line,
            "pid": 123, "timestamp": "2026-01-01", "query_id": query_id, "message": message,
            "application_name": "perf01-calibration", "remote_host": "10.251.135.3"}


# PerfTests 每例建立臨時自有 state；目標拒絕測試會走正式 main／state／guard。
class PerfTests(unittest.TestCase):
    # setUp 只在系統臨時目錄建私密 fixture，禁止測試碰真實 .local 或 Docker。
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("subprocess.run", side_effect=AssertionError("離線測試未設定外部工具替身")))
        perf.infer_parameter_types.cache_clear()
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.target = root / ".local/perf01-fixture"
        self.evidence = self.target / ".local/perf01-evidence"
        for name, path in (("ROOT", root), ("TARGET", self.target), ("EVIDENCE", self.evidence)):
            self.stack.enter_context(patch.object(perf, name, path))
        perf.private_dir(self.evidence)
        perf.private_dir(self.target / ".local/keys")
        perf.save(self.target / ".env", b"DB=fresh\n")
        (self.target / "application.py").write_text("pass\n")
        for filename in perf.FILES:
            for directory in (root, self.target):
                path = directory / "ops/tests" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture instrumentation\n")
        manifest = perf.source_files()
        self.value = {"head": perf.HEAD, "source": perf.digest(perf.encoded(manifest)), "manifest": manifest,
                      "env_hash": perf.digest((self.target / ".env").read_bytes()), "keys": {}, "operations": [],
                      "cluster": {"system": "123", "database": "articles", "role": "articles_owner"},
                      "initialized": True, "calibrated": True, "images": {}, "completed": [], "level": None,
                      "db_id": "d" * 64, "db_command": ["postgres"], "db_config_hash": "b" * 64,
                      "ownership": {"nonce": "f" * 64, "network_id": "e" * 64,
                                    "volume_created": "2026-01-01T00:00:00Z", "volume_mountpoint": "/volumes/perf01/_data"}}
        for index, backend in enumerate(perf.BACKENDS):
            path = self.target / ".local/keys" / (backend + ".key")
            perf.save(path, str(index).encode())
            self.value["keys"][backend] = perf.digest(path.read_bytes())
        perf.save(self.evidence / "prepare-receipt.json", {"state": "complete"})
        perf.save(self.evidence / "state.json", self.value)

    # db_start_receipt 模擬已確認的原始自有啟動，供真實 lifecycle guard 比對精確 ID、映像與 cluster。
    def db_start_receipt(self):
        self.value["db_image"] = "sha256:db"
        return {"kind": "db-start", "state": "complete", "source": self.value["source"], "cluster": self.value["cluster"],
                "detail": {"project": perf.PROJECT, "volume": perf.VOLUME, "db_id": self.value["db_id"], "image": self.value["db_image"], "ownership": self.value["ownership"]}}

    # test_help_and_version 保證兩個安全入口不要求 fixture 或 Docker。
    def test_help_and_version(self):
        with patch.object(perf, "docker") as docker, patch.object(perf, "lifecycle_lock") as lock, patch("sys.stdout", new_callable=io.StringIO):
            for argument in ("--help", "--version"):
                with self.assertRaises(SystemExit) as outcome:
                    perf.main([argument])
                self.assertEqual(outcome.exception.code, 0)
            docker.assert_not_called()
            lock.assert_not_called()

    # test_pending_main_refuses_before_docker 從 CLI 驗證 pending 不會重送 seed／登入或清除 receipt。
    def test_pending_main_refuses_before_docker(self):
        self.value["operations"] = [{"state": "pending", "kind": "seed"}]
        perf.save(self.evidence / "state.json", self.value)
        with patch.object(perf, "docker") as docker, patch.object(perf, "failure_cleanup") as cleanup, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(perf.main(["run"]), 1)
            docker.assert_not_called()
            cleanup.assert_not_called()
        self.assertEqual(perf.load(self.evidence / "state.json")["operations"][0]["state"], "pending")

    # test_source_and_key_guard 每種漂移皆必須在 Docker／SQL 前拒絕。
    def test_source_and_key_guard(self):
        self.assertEqual(perf.state()["source"], self.value["source"])
        (self.target / "application.py").write_text("changed\n")
        with self.assertRaisesRegex(perf.Failure, "manifest"):
            perf.state()
        (self.target / "application.py").write_text("pass\n")
        key = self.target / ".local/keys/laravel.key"
        key.chmod(0o644)
        with self.assertRaisesRegex(perf.Failure, "key"):
            perf.state()

    # test_generated_dotnet_outputs 只略過固定產生目錄，其他來源漂移與輸出 symlink 都須拒絕。
    def test_generated_dotnet_outputs(self):
        for directory in perf.DOTNET_OUTPUTS:
            output = self.target / directory / "generated.cs"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("generated\n")
        self.assertEqual(perf.state()["source"], self.value["source"])
        unexpected = self.target / "backends/aspnet-core/unexpected.cs"
        unexpected.write_text("untracked product input\n")
        with self.assertRaisesRegex(perf.Failure, "manifest"):
            perf.state()
        unexpected.unlink()
        (self.target / "backends/aspnet-core/obj/link").symlink_to(self.target / "application.py")
        with self.assertRaisesRegex(perf.Failure, "symlink"):
            perf.state()

    # test_executor_must_match_qualified_copy 不讓新 ROOT 程式冒用舊 fixture source 身分。
    def test_executor_must_match_qualified_copy(self):
        (perf.ROOT / "ops/tests/perf01_http.py").write_text("changed executor\n")
        with patch.object(perf, "docker") as docker, self.assertRaisesRegex(perf.Failure, "執行端"):
            perf.state()
        docker.assert_not_called()

    # test_preinit_refresh_audits_and_preserves_keys 驗完整資格同步及 readback，不刪初始 receipt 或重新產生憑證。
    def test_preinit_refresh_audits_and_preserves_keys(self):
        self.value.update(initialized=False, cluster=None, calibrated=False)
        old_env = b"DB=fresh\nPERF01_SOURCE=" + self.value["source"].encode() + b"\n"
        perf.save(self.target / ".env", old_env)
        self.value["env_hash"] = perf.digest(old_env)
        perf.save(self.evidence / "state.json", self.value)
        receipt = (self.evidence / "prepare-receipt.json").read_bytes()
        keys = {backend: (self.target / ".local/keys" / (backend + ".key")).read_bytes() for backend in perf.BACKENDS}
        (perf.ROOT / "ops/tests/perf01_http.py").write_text("new qualified executor\n")
        with patch.object(perf, "preflight") as preflight, patch("sys.stdout", new_callable=io.StringIO):
            perf.refresh()
            preflight.assert_called_once_with()
        refreshed = perf.state()
        self.assertNotEqual(refreshed["source"], self.value["source"])
        self.assertEqual(refreshed["manifest"]["application.py"], self.value["manifest"]["application.py"])
        self.assertEqual((self.evidence / "prepare-receipt.json").read_bytes(), receipt)
        self.assertTrue((self.target / ".env").read_bytes().startswith(b"DB=fresh\n"))
        self.assertEqual({backend: (self.target / ".local/keys" / (backend + ".key")).read_bytes() for backend in perf.BACKENDS}, keys)
        audit = perf.load(next(self.evidence.glob("refresh-*.json")))
        self.assertEqual((audit["state"], audit["old_source"], audit["new_source"]), ("complete", self.value["source"], refreshed["source"]))
        self.assertEqual(refreshed["operations"][0]["state"], "complete")
        self.assertEqual(set(audit["files"]), {"ops/tests/" + filename for filename in perf.FILES})

    # test_refresh_refuses_initialized_fixture 既有 DB 不能透過 pre-init refresh 採納不同程式。
    def test_refresh_refuses_initialized_fixture(self):
        with patch.object(perf, "preflight") as preflight, self.assertRaisesRegex(perf.Failure, "恢復身分不完整"):
            perf.refresh()
        preflight.assert_not_called()

    # test_stopped_refresh_checks_actual_target 初始化後只容許有完整恢復證據的精確停止容器更新四個工具檔。
    def test_stopped_refresh_checks_actual_target(self):
        self.value.update(logging=False, logging_original=[], db_image="sha256:db")
        old_calibration = b'[{"kind":"native-pass"}]\n'
        perf.save(self.evidence / "calibration.json", old_calibration)
        backup = self.evidence / ("calibration-" + self.value["source"] + ".json")
        perf.save(backup, old_calibration)
        self.value["operations"] = [self.db_start_receipt(), {"kind": "logging", "state": "complete", "detail": {"enabled": False, "original": []}, "cluster": self.value["cluster"]}]
        completed = ["small/1/laravel", "small/1/symfony"]
        originals = {}
        references = {}
        for unit in completed:
            index = self.evidence / (unit.replace("/", "-") + "-index.json")
            perf.save(index, {"unit": unit, "source": self.value["source"]})
            originals[index] = index.read_bytes()
            references[unit] = {"index": index.name, "sha256": perf.digest(originals[index])}
        attempts = [{"unit": unit, "status": "complete"} for unit in completed]
        self.value.update(level="small", completed=completed, unit_results=references, attempts=attempts)
        old_env = b"DB=fresh\nPERF01_SOURCE=" + self.value["source"].encode() + b"\n"
        perf.save(self.target / ".env", old_env)
        self.value["env_hash"] = perf.digest(old_env)
        perf.save(self.evidence / "state.json", self.value)
        perf.save(self.evidence / "recovery-1.json", {"state": "complete", "cluster": self.value["cluster"], "action": "restore-logging-and-stop"})
        containers = self.containers()
        containers[0]["State"]["Running"] = False
        (perf.ROOT / "ops/tests/perf01_http.py").write_text("new qualified stopped executor\n")
        # reply 只替換 metadata，正式 stopped_refresh_guard／container_guard 及 audit 寫入皆照常走。
        def reply(*arguments):
            return self.inspect_reply(containers, *arguments)
        # previous_index 以固定已完成索引替代昂貴原生矩陣，refresh 本身仍執行實際保存與資格切換。
        def previous_index(value, unit):
            self.assertEqual(value["source"], self.value["source"])
            reference = value["unit_results"][unit]
            self.assertEqual(perf.digest(perf.evidence_file(reference["index"]).read_bytes()), reference["sha256"])
            return reference
        with patch.object(perf, "docker", return_value=b"id"), patch.object(perf, "json_docker", side_effect=reply), patch.object(perf, "verify_unit", side_effect=previous_index) as verify, patch.object(perf, "sql") as sql, patch("sys.stdout", new_callable=io.StringIO):
            containers[0]["State"]["Running"] = True
            with self.assertRaisesRegex(perf.Failure, "必須停止"):
                perf.refresh()
            self.assertFalse(list(self.evidence.glob("refresh-*.json")))
            containers[0]["State"]["Running"] = False
            perf.save(self.evidence / "state.json", {**self.value, "level": "medium"})
            with self.assertRaisesRegex(perf.Failure, "不在本次小量"):
                perf.refresh()
            perf.save(self.evidence / "state.json", self.value)
            perf.refresh()
            self.assertEqual([call.args[1] for call in verify.call_args_list], completed)
            sql.assert_not_called()
        refreshed = perf.state()
        self.assertEqual((refreshed["cluster"], refreshed["db_id"], refreshed["keys"]), (self.value["cluster"], self.value["db_id"], self.value["keys"]))
        self.assertEqual(refreshed["operations"][-1]["kind"], "stopped-fixture-refresh")
        audit = perf.load(next(self.evidence.glob("refresh-*.json")))
        self.assertEqual(audit["state"], "complete")
        self.assertEqual(audit["superseded_units"], references)
        self.assertEqual((refreshed["level"], refreshed["completed"], refreshed["unit_results"], refreshed["attempts"]), ("small", [], {}, attempts))
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.assertFalse(refreshed["calibrated"])
        self.assertEqual(backup.read_bytes(), old_calibration)
        with patch.object(perf, "docker") as docker, self.assertRaisesRegex(perf.Failure, "calibrate"):
            perf.run_matrix()
        docker.assert_not_called()

    # test_calibration_archive_is_immutable 新來源的校準原件只建立一次；同名不同內容必須拒絕而非覆寫。
    def test_calibration_archive_is_immutable(self):
        contents = perf.encoded([{"kind": "native-pass", "calls": 21}])
        perf.archive_calibration(self.value["source"], contents)
        perf.archive_calibration(self.value["source"], contents)
        path = self.evidence / ("calibration-" + self.value["source"] + ".json")
        with self.assertRaisesRegex(perf.Failure, "拒絕覆寫"):
            perf.archive_calibration(self.value["source"], b"[]")
        self.assertEqual(path.read_bytes(), contents)

    # test_refresh_carries_only_approved_ping_source 六組證據經明確承接，案例變更或未知來源均不得借用舊結果。
    def test_refresh_carries_only_approved_ping_source(self):
        units = sorted(perf.PING_CARRY_UNITS)
        references = {unit: {"index": unit.replace("/", "-") + ".json", "sha256": perf.digest(unit.encode())} for unit in units}
        self.value.update(level="small", completed=units, unit_results=references)
        old_env = b"DB=fresh\nPERF01_SOURCE=" + self.value["source"].encode() + b"\n"
        perf.save(self.target / ".env", old_env)
        self.value["env_hash"] = perf.digest(old_env)
        perf.save(self.evidence / "state.json", self.value)
        perf.save(self.evidence / "calibration.json", [{"kind": "native-pass"}])
        driver = perf.ROOT / "ops/tests/perf01_http.py"
        driver.write_text("new ping recognition\n")
        case_file = perf.ROOT / "ops/tests/perf01_cases.py"
        case_original = case_file.read_bytes()
        # old_index 驗證 refresh 呼叫的舊來源及原始窗口掃描要求，再回傳固定索引身分。
        def old_index(value, unit, *, audit_no_ping=False):
            self.assertTrue(audit_no_ping)
            self.assertEqual(value["source"], self.value["source"])
            return references[unit]
        with patch.object(perf, "PING_CARRY_SOURCE", self.value["source"]), patch.object(perf, "stopped_refresh_guard"), patch.object(perf, "verify_unit", side_effect=old_index) as verify, patch("sys.stdout", new_callable=io.StringIO):
            case_file.write_text("changed case\n")
            with self.assertRaisesRegex(perf.Failure, "案例或 Compose"):
                perf.refresh()
            verify.assert_not_called()
            case_file.write_bytes(case_original)
            perf.refresh()
            self.assertEqual([call.args[1] for call in verify.call_args_list], units)
        refreshed = perf.state()
        self.assertEqual((refreshed["completed"], refreshed["unit_results"], refreshed["calibrated"]), (units, references, False))
        carried = {unit: {**reference, "source": self.value["source"]} for unit, reference in references.items()}
        self.assertEqual(refreshed["operations"][-1]["detail"]["carried_units"], carried)
        audit = perf.load(next(self.evidence.glob("refresh-*.json")))
        self.assertEqual((audit["state"], audit["new_source"], audit["carried_units"]), ("complete", refreshed["source"], carried))

    # test_carried_unit_needs_exact_current_refresh 舊 index 僅能以當前精確承接資格引用，並再次查核原始窗口。
    def test_carried_unit_needs_exact_current_refresh(self):
        unit = "small/1/laravel"
        source = perf.PING_CARRY_SOURCE
        window = {"source": source, "backend": "laravel", "round": 1, "level": "small", "case": {"id": "sample"},
                  "oracle_verified": True, "sql": [], "counters": [], "all_sql_calls": 0, "failures": []}
        fixture = {"accounts": [], "articles": [], "comments": []}
        perf.save(self.evidence / "small-statistics.json", {"level": "small"})
        # store_index 用固定案例的完整最小證據，保留真實的 hash 及原始窗口讀回流程。
        def store_index(raw):
            path = self.evidence / "old-window.json"
            perf.save(path, {**window, "raw": raw})
            result = {"case": "sample", "window": path.name, "window_sha256": perf.digest(path.read_bytes()), "plans": [],
                      "fetch": {"queries": {}, "excluded": {}, "all_sql_calls": 0}, "phase": "case-0000"}
            index = self.evidence / "old-index.json"
            perf.save(index, {"unit": unit, "source": source, "fixture_sha256": perf.digest(perf.encoded(fixture)),
                              "case_ids": ["sample"], "cold": result, "warm": [result] * 3, "results": [result], "comparisons": {}})
            return {"index": index.name, "sha256": perf.digest(index.read_bytes())}
        reference = store_index("")
        files = {name: {"old": item, "new": item} for name, item in self.value["manifest"].items()}
        detail = {"old_source": source, "new_source": self.value["source"], "files": files, "carried_units": {unit: {**reference, "source": source}}}
        receipt = {"kind": "stopped-fixture-refresh", "state": "complete", "source": source, "cluster": self.value["cluster"], "detail": detail}
        value = {**self.value, "unit_results": {unit: reference}, "operations": [receipt]}
        with patch.object(perf, "fixture", return_value=fixture), patch.object(perf, "cases", return_value=[{"id": "sample", "path": "/api/members", "status": 401}]):
            self.assertEqual(perf.verify_unit(value, unit), {**reference, "source": source, "carried": True})
            with self.assertRaisesRegex(perf.Failure, "當前承接資格"):
                perf.verify_unit({**value, "operations": []}, unit)
            with self.assertRaisesRegex(perf.Failure, "當前承接資格"):
                perf.verify_unit({**value, "source": "f" * 64}, unit)
            ping = perf.encoded(log_record("duration: 0.016 ms  statement: -- ping", query_id=0)).decode() + "\n"
            contaminated = store_index(ping)
            value["unit_results"][unit] = contaminated
            detail["carried_units"][unit] = {**contaminated, "source": source}
            with self.assertRaisesRegex(perf.Failure, "舊窗口含"):
                perf.verify_unit(value, unit)
            clock = "duration: 0.05 ms  statement: SELECT floor(extract(epoch FROM clock_timestamp()))::bigint AS now"
            affected = store_index(perf.encoded(log_record(clock)).decode() + "\n")
            value["unit_results"][unit] = affected
            detail["carried_units"][unit] = {**affected, "source": source}
            with self.assertRaisesRegex(perf.Failure, "NestJS 時間查詢"):
                perf.verify_unit(value, unit, audit_no_clock=True)

    # test_two_generation_carry_requires_original_receipt 早期六組需逐代核對同一索引，不能只靠最後一次承接。
    def test_two_generation_carry_requires_original_receipt(self):
        older, recent = perf.PING_CARRY_SOURCE, perf.CLOCK_CARRY_SOURCE
        next_source = self.value["source"]
        old_unit, recent_unit = "small/1/laravel", "small/1/gin"
        old_ref = {"index": "old-index.json", "sha256": "a" * 64}
        recent_ref = {"index": "recent-index.json", "sha256": "b" * 64}
        files = {name: {"old": item, "new": item} for name, item in self.value["manifest"].items()}
        # receipt 組成兩次固定來源轉換，供成功及缺少祖先／偽造 SHA 的情境共用。
        def receipt(old, new, units):
            return {"kind": "stopped-fixture-refresh", "state": "complete", "source": old, "cluster": self.value["cluster"],
                    "detail": {"old_source": old, "new_source": new, "files": files, "carried_units": units}}
        first = receipt(older, recent, {old_unit: {**old_ref, "source": older}})
        second = receipt(recent, next_source, {old_unit: {**old_ref, "source": older}, recent_unit: {**recent_ref, "source": recent}})
        value = {**self.value, "operations": [first, second]}
        self.assertEqual(perf.verify_carried_unit(value, old_unit, old_ref, older), recent)
        self.assertEqual(perf.verify_carried_unit(value, recent_unit, recent_ref, recent), recent)
        with self.assertRaisesRegex(perf.Failure, "第一次承接"):
            perf.verify_carried_unit({**value, "operations": [second]}, old_unit, old_ref, older)
        forged = copy.deepcopy(first)
        forged["detail"]["carried_units"][old_unit]["sha256"] = "f" * 64
        with self.assertRaisesRegex(perf.Failure, "當前承接資格"):
            perf.verify_carried_unit({**value, "operations": [forged, second]}, old_unit, old_ref, older)
        with self.assertRaisesRegex(perf.Failure, "當前承接資格"):
            perf.verify_carried_unit(value, old_unit, old_ref, recent)

    # test_resume_verifies_identity_before_completion 重啟後原 cluster 未核對成功前，resume receipt 必須維持 pending。
    def test_resume_verifies_identity_before_completion(self):
        self.value["operations"] = [self.db_start_receipt()]
        perf.save(self.evidence / "state.json", self.value)
        stopped = self.containers()[0]
        stopped["State"]["Running"] = False
        running = copy.deepcopy(stopped)
        running["State"]["Running"] = True
        running["State"]["Health"] = {"Status": "healthy"}
        # wrong_identity 模擬讀回不同 cluster；須保留未知結果且不能接續 schema／其他 SQL 操作。
        def wrong_identity(value):
            operation = perf.load(self.evidence / "state.json")["operations"][-1]
            self.assertEqual((operation["kind"], operation["state"]), ("db-resume", "pending"))
            raise perf.Failure("cluster system_identifier 已改變")
        with patch.object(perf, "config_guard"), patch.object(perf, "container_guard", side_effect=[{"db": stopped}, {"db": running}]), patch.object(perf, "docker", return_value=b"ready") as docker, patch.object(perf, "json_docker", return_value=[running]), patch.object(perf, "identity", side_effect=wrong_identity), patch.object(perf, "schema_snapshot") as schema, patch.object(perf, "query_json") as query:
            with self.assertRaisesRegex(perf.Failure, "system_identifier"):
                perf.initialize()
            self.assertEqual(docker.call_args_list[0].args, ("start", self.value["db_id"]))
            self.assertEqual(docker.call_args_list[1].args[:4], ("exec", self.value["db_id"], "sh", "-c"))
            schema.assert_not_called()
            query.assert_not_called()
        self.assertEqual(perf.load(self.evidence / "state.json")["operations"][-1]["state"], "pending")

    # test_config_guard_keeps_native_db_discoverable 原生 DB 使用 Compose 自己的 hash，錯誤輸出不能變成 discovery label。
    def test_config_guard_keeps_native_db_discoverable(self):
        config = {"name": perf.PROJECT, "volumes": {"postgres_data": {"name": perf.VOLUME}},
                  "networks": {"default": {"name": perf.NETWORK, "ipam": {"config": [{"subnet": perf.SUBNET}]}}},
                  "services": {"db": {"image": perf.IMAGE, "command": ["postgres"]}}}
        for index, backend in enumerate(perf.BACKENDS):
            config["services"][backend] = {
                "image": f"perf01_test_{backend}:{self.value['source']}", "healthcheck": {"disable": True},
                "ports": [{"host_ip": "127.0.0.1", "published": str(18501 + index), "target": 8080 if backend == "aspnet-core" else 8000}],
                "build": {"context": str(self.target), "labels": {"org.perf01.source": self.value["source"]}},
            }
        for output in ("db " + "b" * 64, "sql " + "b" * 64, "db incomplete"):
            with self.subTest(output=output), patch.object(perf, "compose", side_effect=[perf.encoded(config), output.encode()]) as compose:
                if output.startswith("db b"):
                    self.assertEqual(perf.config_guard(self.value), config)
                    self.assertEqual(self.value["db_config_hash"], "b" * 64)
                else:
                    with self.assertRaisesRegex(perf.Failure, "config hash"):
                        perf.config_guard(self.value)
                self.assertEqual([call.args for call in compose.call_args_list], [("config", "--format", "json"), ("config", "--hash", "db")])

    # test_route_supernets 普通乾淨路由可用，較廣或無法解析的候選衝突不可漏掉。
    def test_route_supernets(self):
        perf.route_guard("Destination Gateway Flags\ndefault 1.1.1.1 UG\n127 127.0.0.1 UCS\n10.251.57/24 link#1 U")
        for route in ("10/8", "10.251/16", "10.251.135/24", "10.251.135.1", "10.bad/16"):
            with self.subTest(route=route), self.assertRaises(perf.Failure):
                perf.route_guard("Destination Gateway\n" + route + " link#1")

    # test_preflight_builtin_null_ipam 以真實 host／none metadata 走完整 preflight，不弱化 bridge 資料驗證。
    def test_preflight_builtin_null_ipam(self):
        networks = [
            {"Name": "host", "Driver": "host", "IPAM": {"Driver": "default", "Options": None, "Config": None}},
            {"Name": "none", "Driver": "null", "IPAM": {"Driver": "default", "Options": None, "Config": None}},
            {"Name": "bridge", "Driver": "bridge", "IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}},
        ]
        # docker_reply 僅模擬 daemon metadata，實際 json_docker、路由與 preflight 流程照常執行。
        def docker_reply(*arguments):
            if arguments[:3] == ("network", "ls", "-q"):
                return b"host-id\nnone-id\nbridge-id\n"
            if arguments[:2] == ("network", "inspect"):
                return perf.encoded(networks)
            if arguments[0] in ("ps", "volume"):
                return b""
            self.fail("未預期的 Docker 呼叫")
        with patch.object(perf, "docker", side_effect=docker_reply), patch.object(perf, "tool", return_value=b"Destination Gateway\ndefault 1.1.1.1\n127 127.0.0.1"), patch("socket.socket") as socket_factory:
            perf.preflight()
            listener = socket_factory.return_value.__enter__.return_value
            self.assertEqual([call.args for call in listener.bind.call_args_list], [(("127.0.0.1", port),) for port in range(18501, 18513)])
            for malformed in (None, {}, {"Config": None}, {"Config": []}, {"Config": [None]}, {"Config": [{}]}, {"Config": [{"Subnet": "invalid"}]}, {"Config": [{"Subnet": "10.251.0.0/16"}]}):
                networks[2]["IPAM"] = malformed
                with self.subTest(ipam=malformed), self.assertRaises(perf.Failure):
                    perf.preflight()

    # containers 建造只用於 mock inspect 的新資源身分，錯誤案例不替換正式 guard。
    def containers(self, backend=None):
        labels = {"com.docker.compose.project": perf.PROJECT, "com.docker.compose.project.working_dir": str(self.target),
                  "com.docker.compose.service": "db", "org.perf01.owner": "f" * 64,
                  "com.docker.compose.config-hash": "b" * 64, "com.docker.compose.oneoff": "False"}
        db = {"Id": "d" * 64, "Config": {"Labels": labels, "Image": perf.IMAGE, "Cmd": ["postgres"], "Healthcheck": {"Test": ["CMD-SHELL", "pg_isready -U articles_owner -d articles"]}},
              "Image": "sha256:db", "HostConfig": {"PortBindings": {}}, "State": {"Running": True},
              "NetworkSettings": {"Networks": {perf.NETWORK: {"IPAddress": "10.251.135.2", "NetworkID": "e" * 64}}},
              "Mounts": [{"Type": "volume", "Name": perf.VOLUME, "Destination": "/var/lib/postgresql", "Source": "/volumes/perf01/_data"}]}
        result = [db]
        if backend:
            self.value["images"][backend] = "sha256:app"
            result.append({"Id": "a" * 64, "Config": {"Labels": {**labels, "com.docker.compose.service": backend, "org.perf01.source": self.value["source"]},
                           "Env": ["DB_HOST=db", "DB_DATABASE=articles", "DB_USERNAME=articles_app"], "Healthcheck": {"Test": ["NONE"]}},
                           "Image": "sha256:app", "State": {"Running": True},
                           "HostConfig": {"PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18501"}]}},
                           "NetworkSettings": {"Networks": {perf.NETWORK: {"IPAddress": "10.251.135.3", "NetworkID": "e" * 64}}},
                           "Mounts": [{"Destination": "/run/secrets/laravel-jwt.key", "Source": str(self.target / ".local/keys/laravel.key"), "RW": False}]})
        return result

    # inspect_reply 模擬外部 metadata，guard 的實際決策與 caller 保持正式程式。
    def inspect_reply(self, containers, *arguments):
        if arguments[0] == "inspect":
            return containers
        if arguments[:2] == ("image", "inspect"):
            return [{"Id": "sha256:db"}]
        if arguments[:2] == ("volume", "inspect"):
            return [{"Name": perf.VOLUME, "Driver": "local", "Options": {}, "CreatedAt": "2026-01-01T00:00:00Z", "Mountpoint": "/volumes/perf01/_data",
                     "Labels": {"com.docker.compose.project": perf.PROJECT, "com.docker.compose.volume": "postgres_data", "org.perf01.owner": "f" * 64}}]
        if arguments[:2] == ("network", "inspect"):
            return [{"Name": perf.NETWORK, "Id": "e" * 64, "Driver": "bridge",
                     "Labels": {"com.docker.compose.project": perf.PROJECT, "com.docker.compose.network": "default", "org.perf01.owner": "f" * 64},
                     "IPAM": {"Config": [{"Subnet": perf.SUBNET, "Gateway": "10.251.135.1"}]}, "Containers": {item["Id"]: {} for item in containers if item["State"]["Running"]}}]
        self.fail("未預期的 Docker metadata 呼叫")

    # test_actual_target_guards 用正式 container_guard 驗錯 project／volume／port／image，SQL 完全不觸及。
    def test_actual_target_guards(self):
        self.value["operations"] = [self.db_start_receipt()]
        diagnostics = {"project": "project／service", "volume": "DB volume", "port": "backend ports",
                       "image": "immutable image", "network": "未知 network", "healthcheck": "healthcheck",
                       "db-id": "DB container ID"}
        for fault, diagnostic in diagnostics.items():
            with self.subTest(fault=fault):
                containers = self.containers("laravel")
                if fault == "project":
                    containers[0]["Config"]["Labels"]["com.docker.compose.project"] = "demo"
                elif fault == "volume":
                    containers[0]["Mounts"][0]["Name"] = "old_data"
                elif fault == "port":
                    containers[1]["HostConfig"]["PortBindings"]["8000/tcp"][0]["HostPort"] = "8081"
                elif fault == "image":
                    containers[0]["Image"] = "sha256:wrong"
                elif fault == "network":
                    containers[0]["NetworkSettings"]["Networks"]["unknown"] = {}
                elif fault == "healthcheck":
                    containers[1]["Config"]["Healthcheck"]["Test"] = ["CMD", "curl"]
                else:
                    containers[0]["Id"] = "e" * 64
                # callback 只提供 mock metadata，不繞過正式 guard。
                def reply(*arguments):
                    return self.inspect_reply(containers, *arguments)
                with patch.object(perf, "docker", return_value=b"id"), patch.object(perf, "json_docker", side_effect=reply), patch.object(perf, "sql") as sql:
                    with self.assertRaisesRegex(perf.Failure, diagnostic):
                        perf.container_guard(self.value, "laravel")
                    sql.assert_not_called()

    # test_owned_resume_and_stop_recheck_nonce_and_ids 已完成 receipt 仍不能授權替換過的 DB、network 或 volume。
    def test_owned_resume_and_stop_recheck_nonce_and_ids(self):
        self.value["operations"] = [self.db_start_receipt()]
        perf.save(self.evidence / "state.json", self.value)
        for phase in ("init", "stop"):
            for fault in ("db-nonce", "network-nonce", "network-id", "volume-nonce", "volume-created"):
                with self.subTest(phase=phase, fault=fault):
                    containers = self.containers()
                    containers[0]["State"]["Running"] = phase == "stop"
                    if fault == "db-nonce":
                        containers[0]["Config"]["Labels"]["org.perf01.owner"] = "0" * 64

                    # metadata_reply 每次只替換單一資源，防止測試靠另一個不相關 guard 提早通過。
                    def metadata_reply(*arguments):
                        reply = self.inspect_reply(containers, *arguments)
                        if arguments[:2] == ("network", "inspect"):
                            if fault == "network-nonce":
                                reply[0]["Labels"]["org.perf01.owner"] = "0" * 64
                            elif fault == "network-id":
                                reply[0]["Id"] = "0" * 64
                        if arguments[:2] == ("volume", "inspect"):
                            if fault == "volume-nonce":
                                reply[0]["Labels"]["org.perf01.owner"] = "0" * 64
                            elif fault == "volume-created":
                                reply[0]["CreatedAt"] = "2026-02-01T00:00:00Z"
                        return reply

                    with patch.object(perf, "config_guard"), patch.object(perf, "docker", return_value=containers[0]["Id"].encode()) as docker, patch.object(perf, "json_docker", side_effect=metadata_reply), patch.object(perf, "sql") as sql, patch.object(perf, "failure_cleanup") as cleanup, patch("sys.stderr", new_callable=io.StringIO):
                        self.assertEqual(perf.main([phase]), 1)
                        self.assertTrue(all(call.args[0] == "ps" for call in docker.call_args_list))
                        sql.assert_not_called()
                        cleanup.assert_not_called()

    # test_startup_listener_and_recovery 走 main→實際啟動／readiness／停止鏈，確保只送一次 up 並精確恢復自有容器。
    def test_startup_listener_and_recovery(self):
        for mode in ("warm", "cold", "bad-body", "unknown-start"):
            with self.subTest(mode=mode):
                containers = self.containers("laravel")
                db_id, app_id = (item["Id"] for item in containers)
                containers[1]["State"]["Running"] = False
                self.value.update(images={}, operations=[self.db_start_receipt()], logging=False, logging_original=[], schema={"known": True})
                perf.save(self.evidence / "state.json", self.value)
                stops, startup, probes = [], [], []
                # compose_reply 模擬 up 送出後的未知結果，並先從磁碟驗證完整 image receipt 已落盤。
                def compose_reply(*arguments, **kwargs):
                    startup.append(arguments)
                    if arguments[0] == "up":
                        saved = perf.load(self.evidence / "state.json")
                        receipt = saved["operations"][-1]
                        self.assertEqual((receipt["state"], receipt["detail"], saved["images"]["laravel"]),
                                         ("pending", {"backend": "laravel", "image": "sha256:app"}, "sha256:app"))
                        containers[1]["State"]["Running"] = True
                        if mode == "unknown-start":
                            # 恢復已證明的啟動時，獨立未知的業務寫入仍不可被解除。
                            saved["operations"].append({"kind": "login", "state": "pending"})
                            perf.save(self.evidence / "state.json", saved)
                            raise perf.Failure("native up 結果未知")
                # metadata_reply 保留與實際 inspect 相同的獨立快照，避免測試共享可變容器狀態。
                def metadata_reply(*arguments):
                    if arguments[:2] == ("image", "inspect") and arguments[2].startswith("perf01_test_laravel:"):
                        return [{"Id": "sha256:app", "Config": {"Labels": {"org.perf01.source": self.value["source"]}}}]
                    if arguments[0] == "inspect":
                        return copy.deepcopy([item for item in containers if item["Id"] in arguments[1:]])
                    return self.inspect_reply(containers, *arguments)
                # docker_reply 第一筆 procfs 無 listener，第二筆才出現 LISTEN；停止只允許剛驗過的精確 ID。
                def docker_reply(*arguments, **kwargs):
                    if arguments[0] == "ps":
                        return "\n".join(item["Id"] for item in containers if arguments[1] == "-aq" or item["State"]["Running"]).encode()
                    if arguments[0] == "exec":
                        self.assertEqual(arguments[1], app_id)
                        self.assertEqual(perf.load(self.evidence / "state.json")["operations"][-1]["state"], "complete")
                        probes.append(arguments)
                        return b"sl local_address rem_address st\n" + (b"0: 00000000:1F40 00000000:0000 0A\n" if len(probes) > 1 else b"")
                    if arguments[0] == "stop":
                        container_id = arguments[-1]
                        self.assertIn(container_id, (db_id, app_id))
                        stops.append(container_id)
                        next(item for item in containers if item["Id"] == container_id)["State"]["Running"] = False
                        return b""
                    self.fail("未預期的 Docker 呼叫")
                # execute 保留正式 state 與啟動鏈，測試替換範圍限矩陣進入 startup 的入口。
                def execute(owned):
                    value = perf.state()
                    perf.container_guard(value)
                    perf.identity(value)
                    owned()
                    perf.start_backend(value, "laravel", readiness=mode != "cold")
                response = MagicMock(code=200)
                response.__enter__.return_value = response
                response.read.return_value = b"unexpected" if mode == "bad-body" else b"OK"
                transport = [urllib.error.URLError(ConnectionRefusedError("private")), response] if mode == "warm" else [response]
                with patch.object(perf, "run_matrix", side_effect=execute), patch.object(perf, "config_guard"), patch.object(perf, "identity"), patch.object(perf, "schema_snapshot", return_value=self.value["schema"]), patch.object(perf, "query_json", side_effect=[[], 0]), patch.object(perf, "sql"), patch.object(perf, "compose", side_effect=compose_reply), patch.object(perf, "docker", side_effect=docker_reply), patch.object(perf, "json_docker", side_effect=metadata_reply), patch("time.sleep"), patch("socket.create_connection") as connect, patch("urllib.request.OpenerDirector.open", side_effect=transport) as send, patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
                    self.assertEqual(perf.main(["run"]), 1 if mode in ("bad-body", "unknown-start") else 0)
                    self.assertEqual(connect.call_count, 0 if mode == "unknown-start" else 1)
                    self.assertEqual(send.call_count, {"warm": 2, "cold": 0, "bad-body": 1, "unknown-start": 0}[mode])
                    self.assertTrue(all(call.args[0].get_method() == "GET" and call.args[0].full_url.endswith("/up") for call in send.call_args_list))
                    self.assertNotIn("private", diagnostic.getvalue())
                self.assertEqual(startup, [("build", "laravel"), ("up", "-d", "--no-deps", "laravel")])
                self.assertEqual(len(probes), 0 if mode == "unknown-start" else 2)
                self.assertEqual(stops, [app_id, db_id] if mode in ("bad-body", "unknown-start") else [])
                operations = perf.load(self.evidence / "state.json")["operations"]
                self.assertEqual(operations[1]["state"], "recovered" if mode == "unknown-start" else "complete")
                if mode == "unknown-start":
                    self.assertEqual(operations[2], {"kind": "login", "state": "pending"})

    # test_run_entry_wrong_target 真正 run_matrix caller chain 不得在錯 volume 時進入 identity／seed。
    def test_run_entry_wrong_target(self):
        self.value["operations"] = [self.db_start_receipt()]
        perf.save(self.evidence / "state.json", self.value)
        containers = self.containers()
        containers[0]["Mounts"][0]["Name"] = "demo_postgres_data"
        # callback 僅替換 Docker 回覆，run_matrix 與 container_guard 都執行原程式。
        def reply(*arguments):
            return self.inspect_reply(containers, *arguments)
        with patch.object(perf, "config_guard"), patch.object(perf, "docker", return_value=b"id"), patch.object(perf, "json_docker", side_effect=reply), patch.object(perf, "sql") as sql:
            with self.assertRaisesRegex(perf.Failure, "volume"):
                perf.run_matrix()
            sql.assert_not_called()

    # test_unknown_login_is_not_retried 透過真正登入 wrapper 與 HTTP read 失敗保留 pending，第二次不能再送。
    def test_unknown_login_is_not_retried(self):
        class Response:
            code = 200
            # __enter__ 模擬已收到 HTTP header，之後 body read 仍可能未知。
            def __enter__(self):
                return self
            # __exit__ 保留原 read 例外，不假冒失敗前尚未送出。
            def __exit__(self, *arguments):
                return False
            # read 模擬送出登入後讀取逾時，不能因此重做 POST。
            def read(self, limit):
                raise TimeoutError("secret must not reach console")
        with patch("urllib.request.OpenerDirector.open", return_value=Response()) as send:
            for _ in range(2):
                with self.assertRaises(perf.Failure):
                    perf.authenticate(self.value, "laravel", "member")
            self.assertEqual(send.call_count, 1)
        self.assertEqual(perf.load(self.evidence / "state.json")["operations"][0]["state"], "pending")

    # test_tool_unknown_transport 保證外部 timeout 不輸出 argv／payload 且 receipt 保留。
    def test_tool_unknown_transport(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("secret", 1)), self.assertRaises(perf.Failure):
            with perf.mutation(self.value, "seed", {"fixture": "small"}):
                perf.tool(["false"], b"secret")
        self.assertEqual(perf.load(self.evidence / "state.json")["operations"][0]["state"], "pending")

    # test_lock_refuses_second_owner 直接走原生 flock，而非 mock 一個永遠成功的 lock。
    def test_lock_refuses_second_owner(self):
        with perf.lifecycle_lock():
            with self.assertRaisesRegex(perf.Failure, "lifecycle"):
                with perf.lifecycle_lock():
                    self.fail("第二個 owner 不應進入")

    # test_main_cleanup_stays_inside_acquired_lock 確認取鎖失敗無副作用，原命令出錯後恢復仍持同一把鎖。
    def test_main_cleanup_stays_inside_acquired_lock(self):
        with perf.lifecycle_lock(), patch.object(perf, "run_matrix") as run, patch.object(perf, "stop") as stop, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(perf.main(["run"]), 1)
            run.assert_not_called()
            stop.assert_not_called()

        # execute 表示目標已通過精確身分讀回，故後續失敗需要受限恢復。
        def execute(owned):
            owned()
            raise perf.Failure("受控執行失敗")

        # recovery 檢查當下的原生鎖；若 CLI 已釋放再取鎖，外層的取鎖次數也會揭露空窗。
        def recovery():
            with self.assertRaisesRegex(perf.Failure, "lifecycle"):
                with perf.lifecycle_lock():
                    self.fail("恢復期間應維持原鎖")
        with patch.object(perf, "lifecycle_lock", wraps=perf.lifecycle_lock) as lock, patch.object(perf, "run_matrix", side_effect=execute), patch.object(perf, "stop", side_effect=recovery), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(perf.main(["run"]), 1)
            self.assertEqual(lock.call_count, 2)  # 原命令一次，recovery 內驗證拒絕一次。

    # test_conflicting_init_and_stop_do_not_adopt_database 兩個入口都不能以同名容器替代完整 db-start receipt。
    def test_conflicting_init_and_stop_do_not_adopt_database(self):
        self.value.update(initialized=False, cluster=None, operations=[])
        perf.save(self.evidence / "state.json", self.value)
        with patch.object(perf, "config_guard"), patch.object(perf, "docker", return_value=b"matching-id") as docker, patch.object(perf, "query_json") as sql, patch.object(perf, "compose") as compose, patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
            self.assertEqual(perf.main(["init"]), 1)
            self.assertEqual(perf.main(["stop"]), 1)
            docker.assert_called_once_with("ps", "-aq", "--filter", "label=com.docker.compose.project=" + perf.PROJECT)
            compose.assert_not_called()
            sql.assert_not_called()
            self.assertIn("新 project 名稱已被使用", diagnostic.getvalue())
            self.assertIn("缺少固定 DB 的啟動身分", diagnostic.getvalue())

    # startup_case 只替換 Docker 與已渲染設定，讓 main→initialize→guard→identity→stop 全部走真實程式。
    def startup_case(self, fault):
        initial = {**self.value, "initialized": False, "cluster": None, "operations": [], "db_id": None}
        initial.pop("ownership")
        perf.save(self.evidence / "state.json", initial)
        env_path = self.evidence / "db-create.env"
        if env_path.exists():
            env_path.unlink()
        containers = self.containers()
        db = containers[0]
        db["State"] = {"Running": False, "Status": "created", "Health": {"Status": "healthy"}}
        db["HostConfig"]["NetworkMode"] = "e" * 64
        db["NetworkSettings"]["Networks"] = {"e" * 64: {"NetworkID": ""}}
        volume = self.inspect_reply(containers, "volume", "inspect")[0]
        network = self.inspect_reply(containers, "network", "inspect")[0]
        events, probes, cached_images = [], [], []
        config = {"services": {"db": {"environment": {"POSTGRES_DB": "articles", "POSTGRES_USER": "articles_owner",
                  "POSTGRES_PASSWORD": "a" * 64, "PERF01_FIXTURE": "isolated-perf01"}}}}
        save = perf.save

        # state_write 模擬建立證明無法持久化；即使資源已建立也不得先啟動再補 receipt。
        def state_write(path, value, **kwargs):
            if (fault == "receipt-unknown" and path.name == "state.json" and isinstance(value, dict)
                    and value.get("db_id") and value.get("ownership")):
                raise OSError("受控 receipt 寫入失敗")
            return save(path, value, **kwargs)

        # labels_from 只解析測試 mock 的 argv，以便各競爭案例分別保留真正不同的 owner nonce。
        def labels_from(arguments):
            return dict(arguments[index + 1].split("=", 1) for index, argument in enumerate(arguments) if argument == "--label")

        # compose_reply 初始化只能在 ownership 與 SQL cluster 已確認後才進入集中 SQL。
        def compose_reply(*arguments, **kwargs):
            events.append(arguments[0])
            self.assertEqual(perf.load(self.evidence / "state.json")["operations"][0]["state"], "complete")
            self.assertIn(arguments, (("build", "sql"), ("run", "--rm", "--no-deps", "sql")))
            if fault == "build-unknown":
                raise perf.Failure("集中 SQL build 結果未知")
            return b""

        # docker_reply 以資源生命週期回覆真實 JSON，驗 exact-ID、秘密不進 argv、先落盤後 start 與 TCP readiness。
        def docker_reply(*arguments, **kwargs):
            self.assertNotIn("a" * 64, " ".join(arguments))
            if fault == "image-missing" and (arguments[:2] in (("volume", "create"), ("network", "create")) or arguments[0] == "create"):
                events.append("claim")
            if arguments[0] == "ps":
                return db["Id"].encode() if arguments[1] == "-aq" or db["State"]["Running"] else b""
            if arguments[:2] == ("volume", "create"):
                self.assertEqual(cached_images, ["sha256:db"])
                self.assertEqual(perf.load(self.evidence / "state.json")["db_image"], cached_images[0])
                if fault != "volume":
                    volume["Labels"].update(labels_from(arguments))
                return perf.VOLUME.encode()
            if arguments[:2] == ("network", "create"):
                if fault != "network":
                    network["Labels"].update(labels_from(arguments))
                return network["Id"].encode()
            if arguments[0] == "create":
                if fault == "db-conflict":
                    raise perf.Failure("同名 container 已被競爭者建立")
                if fault != "db":
                    db["Config"]["Labels"].update(labels_from(arguments))
                self.assertEqual(labels_from(arguments)["com.docker.compose.config-hash"], "b" * 64)
                self.assertEqual(labels_from(arguments)["com.docker.compose.oneoff"], "False")
                self.assertEqual(arguments[arguments.index("--network") + 1], network["Id"])
                self.assertIn("type=volume,src=" + perf.VOLUME + ",dst=/var/lib/postgresql,volume-nocopy", arguments)
                self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(env_path.read_text(), "".join(key + "=" + value + "\n" for key, value in config["services"]["db"]["environment"].items()))
                if fault == "create-unknown":
                    raise perf.Failure("create 結果未知")
                return db["Id"].encode()
            if arguments[:2] == ("volume", "inspect"):
                return perf.encoded([volume])
            if arguments[:2] == ("network", "inspect"):
                network["Containers"] = {db["Id"]: {}} if db["State"]["Running"] else {}
                return perf.encoded([network])
            if arguments[:2] == ("image", "inspect"):
                if fault == "image-missing" and arguments[2] == perf.IMAGE:
                    raise perf.Failure("鎖定映像未快取")
                if arguments[2] == perf.IMAGE:
                    cached_images.append("sha256:db")
                image = {"Id": "sha256:db"} if arguments[2] == perf.IMAGE else {"Id": "sha256:app", "Config": {"Labels": {"org.perf01.source": self.value["source"]}}}
                return perf.encoded([image])
            if arguments[0] == "inspect":
                return perf.encoded(containers)
            if arguments[0] == "start":
                events.append("start")
                recorded = perf.load(self.evidence / "state.json")
                self.assertEqual(arguments[1], recorded["db_id"])
                self.assertEqual(recorded["ownership"]["nonce"], db["Config"]["Labels"]["org.perf01.owner"])
                self.assertEqual(recorded["operations"][0]["detail"]["ownership"], recorded["ownership"])
                self.assertEqual(recorded["operations"][0]["state"], "pending")
                db["State"].update(Running=True, Status="running")
                db["NetworkSettings"]["Networks"] = {perf.NETWORK: {"NetworkID": network["Id"]}}
                if fault == "start-unknown":
                    raise perf.Failure("start 結果未知")
                return b""
            if arguments[0] == "exec":
                if arguments[2:4] == ("sh", "-c"):
                    self.assertEqual(arguments[1], db["Id"])
                    self.assertIn("pg_isready -h 127.0.0.1", arguments[4])
                    probes.append(arguments)
                    return b"ready" if len(probes) > 1 else b""
                events.append("SQL")
                self.assertIn(db["Id"], arguments)
                self.assertGreaterEqual(len(probes), 2)
                query = kwargs["payload"].decode()
                if "pg_control_system()" in query:
                    return perf.encoded(self.value["cluster"])
                if "pg_available_extensions" in query:
                    return b"1"
                if "information_schema.columns" in query:
                    return perf.encoded({"known": True})
                if "rolconfig" in query:
                    return b"[]"
                if "json_object_agg" in query:
                    return b"{}"
                return b""
            if arguments[0] == "stop":
                events.append("stop")
                self.assertEqual(arguments[-1], db["Id"])
                db["State"].update(Running=False, Status="exited")
                return b""
            self.fail("未預期的 Docker 呼叫：" + arguments[0])

        with patch.object(perf, "config_guard", return_value=config), patch.object(perf, "preflight"), patch.object(perf, "compose", side_effect=compose_reply), patch.object(perf, "docker", side_effect=docker_reply), patch.object(perf, "save", side_effect=state_write), patch("time.sleep"), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
            result = perf.main(["init"])
            recorded = perf.load(self.evidence / "state.json")
            if fault in ("db", "db-conflict", "network", "volume", "create-unknown", "start-unknown", "receipt-unknown"):
                before_stop = list(events)
                self.assertEqual(perf.main(["stop"]), 1)
                self.assertEqual(events, before_stop)
                # pending 初始操作禁止重送，不能把第二次 init 當成重新認領。
                self.assertEqual(perf.main(["init"]), 1)
                self.assertEqual(events, before_stop)
            elif fault == "none":
                self.assertEqual(perf.main(["stop"]), 0)
                self.assertFalse(db["State"]["Running"])
        return result, recorded, events, diagnostic.getvalue()

    # test_missing_cached_image_fails_before_claims 未快取鎖定映像時只讀失敗，不留下 pending 或占用新資源名稱。
    def test_missing_cached_image_fails_before_claims(self):
        result, value, events, diagnostic = self.startup_case("image-missing")
        self.assertEqual(result, 1)
        self.assertIn("鎖定映像未快取", diagnostic)
        self.assertEqual(value["operations"], [])
        self.assertEqual(events, [])
        self.assertIsNone(value["db_id"])
        self.assertIsNone(value["cluster"])
        self.assertFalse((self.evidence / "db-create.env").exists())

    # test_post_preflight_rival_is_never_started 分別競爭 DB、network、volume，真實 init/stop 都不得觸及 SQL 或清理。
    def test_post_preflight_rival_is_never_started(self):
        for rival in ("db", "db-conflict", "network", "volume"):
            with self.subTest(rival=rival):
                result, value, events, diagnostic = self.startup_case(rival)
                self.assertEqual(result, 1)
                self.assertEqual(events, [], diagnostic)
                self.assertEqual(value["operations"][0]["state"], "pending")
                self.assertIsNone(value["cluster"])
                self.assertIn("競爭者" if rival == "db-conflict" else "nonce", diagnostic)

    # test_fresh_db_ownership_is_durable_before_recovery 成功、建立未知、啟動未知與後續 build 失敗均保留正確恢復資格。
    def test_fresh_db_ownership_is_durable_before_recovery(self):
        for fault in ("create-unknown", "start-unknown", "receipt-unknown", "build-unknown", "none"):
            with self.subTest(fault=fault):
                result, value, events, diagnostic = self.startup_case(fault)
                self.assertEqual(result, 0 if fault == "none" else 1, diagnostic)
                if fault in ("create-unknown", "start-unknown", "receipt-unknown"):
                    self.assertEqual(value["operations"][0]["state"], "pending")
                    self.assertEqual(events, ["start"] if fault == "start-unknown" else [])
                    self.assertIsNone(value["cluster"])
                else:
                    self.assertEqual(perf.owned_db(value), "d" * 64)
                    self.assertEqual(value["cluster"], self.value["cluster"])
                    self.assertEqual(events[:3], ["start", "SQL", "build"])
                    self.assertEqual(events[-1], "stop")
                    self.assertEqual(value["initialized"], fault == "none")
                    self.assertEqual(value["operations"][-1]["state"], "pending" if fault == "build-unknown" else "complete")

    # test_stopped_db_needs_restored_logging_receipt 已停機只能引用最新同容器 readback，不能自行推定恢復。
    def test_stopped_db_needs_restored_logging_receipt(self):
        self.value.update(operations=[self.db_start_receipt()], logging_original=[])
        containers = self.containers()
        containers[0]["State"]["Running"] = False
        receipt = {"state": "complete", "cluster": self.value["cluster"], "db_id": self.value["db_id"],
                   "action": "restore-logging-and-stop", "logging_readback": []}
        path = self.evidence / "recovery-1.json"
        # docker_reply 僅提供 inspect 名稱及沒有執行中的容器，不允許任何 stop／exec。
        def docker_reply(*arguments):
            self.assertEqual(arguments[0], "ps")
            return self.value["db_id"].encode() if arguments[1] == "-aq" else b""
        # metadata_reply 模擬固定容器的停止狀態，正式 guard 仍檢查完整身分。
        def metadata_reply(*arguments):
            return self.inspect_reply(containers, *arguments)
        with patch.object(perf, "config_guard"), patch.object(perf, "docker", side_effect=docker_reply), patch.object(perf, "json_docker", side_effect=metadata_reply), patch.object(perf, "sql") as sql, patch("sys.stderr", new_callable=io.StringIO) as diagnostic, patch("sys.stdout", new_callable=io.StringIO) as progress:
            for fault, logging, recorded in (("enabled", True, receipt), ("unknown", None, receipt), ("missing", False, None),
                                               ("pending", False, {**receipt, "state": "pending"}), ("cluster", False, {**receipt, "cluster": None})):
                with self.subTest(fault=fault):
                    perf.save(self.evidence / "state.json", {**self.value, "logging": logging})
                    if recorded:
                        perf.save(path, recorded)
                    elif path.exists():
                        path.unlink()
                    self.assertEqual(perf.main(["stop"]), 1)
            self.assertEqual(progress.getvalue(), "")
            self.assertIn("logging 恢復", diagnostic.getvalue())
            perf.save(path, receipt)
            staged = self.evidence / "recovery-2.json.pending"
            perf.save(staged, {"state": "pending"})
            self.assertEqual(perf.main(["stop"]), 1)
            self.assertEqual(progress.getvalue(), "")
            staged.unlink()
            self.assertEqual(perf.main(["stop"]), 0)
            self.assertIn('"status": "stopped"', progress.getvalue())
            sql.assert_not_called()

    # test_docker_argv_pins_authorized_local_daemon 以真正 wrapper 核對 preflight／Compose argv 及環境，不連本機 socket。
    def test_docker_argv_pins_authorized_local_daemon(self):
        ambient = {"DOCKER_HOST": "ssh://remote", "DOCKER_CONTEXT": "remote", "DOCKER_TLS": "1",
                   "DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/elsewhere", "DOCKER_CONFIG": "/elsewhere",
                   "COMPOSE_PROJECT_NAME": "remote"}
        response = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.dict(os.environ, ambient), patch("subprocess.run", return_value=response) as runner:
            perf.docker("ps")
            perf.compose("config", "--format", "json")
        for request in runner.call_args_list:
            self.assertEqual(request.args[0][:3], ["docker", "--host", "unix:///Users/ksy/.orbstack/run/docker.sock"])
            self.assertTrue(ambient.keys().isdisjoint(request.kwargs["env"]))

    # test_counter_invalidation reset、eviction、遺失及下降均不會被解釋為少一次查詢。
    def test_counter_invalidation(self):
        self.assertEqual(sum(item["calls"] for item in perf.delta(snapshot(1), snapshot(21))), 20)
        for field, replacement in (("reset", "new"), ("dealloc", 1), ("entries", [])):
            after = snapshot(2)
            after[field] = replacement
            with self.subTest(field=field), self.assertRaises(perf.Failure):
                perf.delta(snapshot(1), after)
        with self.assertRaises(perf.Failure):
            perf.delta(snapshot(2), snapshot(1))

    # test_capture_owner_activity_and_app_pollution 從正式 capture 全鏈驗 owner 觀測不致假失敗、app 污染仍拒絕。
    def test_capture_owner_activity_and_app_pollution(self):
        for mode in ("owner", "pollution", "native", "foreign", "orphan"):
            with self.subTest(mode=mode):
                raw = bytearray()
                calls = 0
                snapshots = 0
                owner_calls = 0
                sent = []
                # append_app 加入真實格式 duration 與原生 calls，不能只讓 helper 假成功。
                def append_app():
                    nonlocal calls
                    calls += 1
                    if mode in ("native", "foreign", "orphan"):
                        # 與本輪 PostgreSQL 原生紀錄相同：前兩筆尚無 app name，ready 才開始帶入。
                        native = [log_record("connection authenticated: user=articles_app method=trust", 1),
                                  log_record("connection authorized: user=articles_app database=articles application_name=perf01-calibration", 2),
                                  log_record("connection ready: setup total=1.224 ms", 3),
                                  log_record("duration: 0.132 ms  statement: SELECT 1;", 4),
                                  log_record("disconnection: session time: 0:00:00.001", 5)]
                        for record in native[:2]:
                            del record["application_name"]
                        if mode == "foreign":
                            native[2]["application_name"] = "unknown-client"
                        if mode == "orphan":
                            native = native[:2]
                        raw.extend(b"".join(perf.encoded(record) + b"\n" for record in native))
                    else:
                        raw.extend(perf.encoded(log_record(line=calls)) + b"\n")
                # owner_sql 模擬每次控制查詢都新增 owner connection log，底層 JSON 解碼與游標仍用正式程式。
                def owner_sql(value, query, app=False):
                    nonlocal snapshots, owner_calls
                    self.assertFalse(app)
                    owner_calls += 1
                    raw.extend(perf.encoded({"user": "articles_owner", "dbname": "articles", "message": "connection authorized", "line_num": owner_calls}) + b"\n")
                    if "pg_stat_activity" in query:
                        return "[]"
                    if "pg_stat_statements_info" in query:
                        snapshots += 1
                        if mode == "pollution" and snapshots == 2:
                            append_app()
                        return json.dumps(snapshot(calls))
                    if "pg_current_logfile" in query:
                        return json.dumps({"path": "log/perf01.json", "size": len(raw), "prefix": base64.b64encode(raw[:128]).decode()})
                    if "pg_read_binary_file" in query:
                        match = perf.re.search(r",(\d+),(\d+)\)", query)
                        self.assertIsNotNone(match)
                        start, length = map(int, match.groups())
                        return base64.b64encode(raw[start:start + length]).decode()
                    self.fail("未預期的控制 SQL")
                # action 是單次已知請求的觀測 seam；污染必須在送出它之前被拒絕。
                def action():
                    sent.append(True)
                    append_app()
                    return "response"
                with patch.object(perf, "sql", side_effect=owner_sql), patch("time.sleep"):
                    old_invalid = set(self.evidence.glob("invalid-window-*.json"))
                    if mode == "pollution":
                        with self.assertRaisesRegex(perf.Failure, "背景 app"):
                            perf.capture(self.value, action, host="10.251.135.3", calibration=True)
                        self.assertEqual(sent, [])
                    elif mode in ("foreign", "orphan"):
                        with self.assertRaisesRegex(perf.Failure, "非校準" if mode == "foreign" else "孤立"):
                            perf.capture(self.value, action, host="10.251.135.3", calibration=True)
                        self.assertEqual(sent, [True])
                    else:
                        response, evidence = perf.capture(self.value, action, host="10.251.135.3", calibration=True)
                        self.assertEqual(response, "response")
                        self.assertEqual(evidence["all_sql_calls"], 1)
                        self.assertGreater(owner_calls, 5)
                        self.assertIn("articles_owner", evidence["raw"])
                        self.assertEqual(len(evidence["sql"]), 1)
                    if mode in ("pollution", "foreign", "orphan"):
                        invalid = set(self.evidence.glob("invalid-window-*.json")) - old_invalid
                        self.assertEqual(len(invalid), 1)
                        record = perf.load(invalid.pop())
                        self.assertTrue(record["raw"] and record["records"] and record["cursor_before"] and record["cursor_after"])
                        self.assertIsNotNone(record["before"])
                        self.assertIsNotNone(record["after"])

    # test_bind_decode 特殊字元、NULL、引號及截斷拒絕都從正式 parser 驗證。
    def test_bind_decode(self):
        self.assertEqual(perf.binds("parameters: $1 = 'a%_\\陳''quote', $2 = NULL, $3 = ''"), ["a%_\\陳'quote", None, ""])
        for invalid in ("parameters: $2 = 'a'", "parameters: $1 = 'cut", "parameters: $1 = 'a',..."):
            with self.subTest(invalid=invalid), self.assertRaises(perf.Failure):
                perf.binds(invalid)

    # test_protocol_and_utility_counts Parse／Bind 不算 SQL，SET／DISCARD 不可被過濾。
    def test_protocol_and_utility_counts(self):
        records = [log_record("duration: 0.1 ms  parse <unnamed>: SELECT $1", 1),
                   log_record("duration: 0.1 ms  bind <unnamed>: SELECT $1", 2),
                   {**log_record("duration: 0.1 ms  execute <unnamed>: SELECT $1", 3), "detail": "parameters: $1 = 'x'"}]
        completed, protocol, failures = perf.events(records, calibration=True)
        self.assertEqual((len(completed), len(protocol), failures), (1, 2, []))
        perf.reconcile(perf.delta(snapshot(), snapshot(1)), completed)
        for query in ("SET TIME ZONE 'Asia/Taipei'", "DISCARD ALL", "RESET ALL"):
            self.assertEqual(perf.category(query), "connection")

    # test_log_cursor_and_client_rejection 游標截斷、prefix 改變、行號缺口及污染都使窗口失效。
    def test_log_cursor_and_client_rejection(self):
        before = {"path": "log/perf01.json", "size": 128, "prefix": "abc"}
        for after in ({**before, "size": 127}, {**before, "prefix": "wrong"}, {**before, "path": "other"}):
            with self.assertRaises(perf.Failure):
                perf.log_slice(self.value, before, after)
        with self.assertRaisesRegex(perf.Failure, "游標缺口"):
            perf.events([log_record(line=1), log_record(line=3)])
        with self.assertRaisesRegex(perf.Failure, "未知 app client"):
            perf.events([log_record()], expected_host="10.251.135.99")
        with self.assertRaisesRegex(perf.Failure, "非校準"):
            perf.events([{**log_record(), "application_name": "pollution"}], calibration=True)

    # test_compound_sql 拆分僅發生在語法外的分號，多形 query ID 不能借最後一筆偷偷對上。
    def test_compound_sql(self):
        query = "SELECT ';' /* ; */; SELECT $$a;b$$; SELECT 2"
        self.assertEqual(len(perf.split_statements(query)), 3)
        completed, _, _ = perf.events([log_record("duration: 0.1 ms  statement: SELECT 1; SELECT 2")])
        perf.reconcile(perf.delta(snapshot(), snapshot(2)), completed)
        counters = perf.delta(snapshot(), snapshot(1))
        with self.assertRaises(perf.Failure):
            perf.reconcile(counters, completed)

    # test_empty_ping_preserves_protocol_and_guards 精確空 ping 零 SQL，但錯 ID、bind 或後續 SQL 不可借用該分類。
    def test_empty_ping_preserves_protocol_and_guards(self):
        record = log_record("duration: 0.016 ms  statement: -- ping", query_id=0)
        completed, protocol, failures = perf.events([record], expected_host="10.251.135.3", calibration=True)
        perf.reconcile([], completed)
        self.assertEqual((completed, failures, protocol), ([], [], [{**record, "protocol_kind": "pgx-empty-ping"}]))
        for wrong in ({**record, "query_id": 42}, {**record, "detail": "parameters: $1 = 'secret'"},
                      {**record, "message": record["message"] + "\nSELECT 1"},
                      {**record, "message": record["message"].replace("statement:", "execute <unnamed>:")}):
            with self.subTest(message=wrong["message"]):
                sql, protocol, _ = perf.events([wrong])
                self.assertFalse(protocol)
                with self.assertRaises(perf.Failure):
                    perf.reconcile([], sql)
        with self.assertRaisesRegex(perf.Failure, "未知 app client"):
            perf.events([record], expected_host="10.251.135.99")
        with self.assertRaisesRegex(perf.Failure, "非校準 client"):
            perf.events([{**record, "application_name": "foreign"}], calibration=True)

    # test_npgsql_compound_startup 保留原生 multi-statement 格式及最後 ID，查詢改形、錯 ID 或其他後端一律不歸屬。
    def test_npgsql_compound_startup(self):
        raw = ("SELECT version()", "SELECT ns.nspname FROM pg_type WHERE typname='record'",
               "-- Load field definitions\nSELECT att.attname FROM pg_attribute AS att WHERE attnum > 0",
               "-- Load enum fields\nSELECT enumlabel FROM pg_enum")
        normalized = (raw[0], "SELECT ns.nspname FROM pg_type WHERE typname=$1",
                      "SELECT att.attname FROM pg_attribute AS att WHERE attnum > $1", "SELECT enumlabel FROM pg_enum")
        profile = tuple((perf.digest(query.encode()), perf.digest(counter.encode())) for query, counter in zip(raw, normalized))
        query_ids = ("11", "12", "13", "-5", "21", "22", "23", "24")
        sql = ("BEGIN", "SELECT count(*) FROM articles", "SELECT id FROM articles ORDER BY id LIMIT 20", "COMMIT")
        counters = [{"queryid": query_id, "query": query, "calls": 1} for query_id, query in zip(query_ids, (*normalized, *sql))]
        last_id = (1 << 64) - 5
        with patch.object(perf, "NPGSQL_STARTUP_QUERIES", profile):
            for fault in (None, "last-id", "raw-shape", "counter-shape", "wrong-backend"):
                with self.subTest(fault=fault):
                    batch = ";\n".join(raw).replace("'record'", "'void'") if fault == "raw-shape" else ";\n".join(raw)
                    records = [log_record("duration: 0.31 ms  statement: " + batch, query_id=11 if fault == "last-id" else last_id)]
                    records.extend(log_record("duration: 0.1 ms  statement: " + query, line=index + 2, query_id=int(query_ids[index + 4])) for index, query in enumerate(sql))
                    emitted, _, _ = perf.events(records)
                    measured = copy.deepcopy(counters)
                    if fault == "counter-shape":
                        measured[1]["query"] += " ORDER BY ns.nspname"
                    if fault:
                        with self.assertRaises(perf.Failure):
                            perf.reconcile(measured, emitted, "laravel" if fault == "wrong-backend" else "aspnet-core")
                        continue
                    perf.reconcile(measured, emitted, "aspnet-core")
                    self.assertEqual([event["query_id"] for event in emitted[:4]], ["11", "12", "13", str(last_id)])
                    self.assertEqual({event["logged_query_id"] for event in emitted[:4]}, {str(last_id)})
                    self.assertEqual([event["category"] for event in emitted[:4]], ["driver-metadata"] * 4)
                    self.assertEqual(len(emitted), 8)
                    self.assertEqual(sum(perf.fetch_counts({"counters": measured, "all_sql_calls": 8})["queries"].values()), 6)

    # test_app_quiet_exact_idle_host 保留同一後端的 idle pool，其他 IP 或本機 socket 都不得混入量測。
    def test_app_quiet_exact_idle_host(self):
        for host in ("10.251.135.3", "10.251.135.99", None):
            with self.subTest(host=host), patch.object(perf, "query_json", return_value=[{"client_addr": host, "state": "idle"}]) as query:
                if host == "10.251.135.3":
                    self.assertTrue(perf.app_quiet(self.value, expected_host="10.251.135.3"))
                else:
                    with self.assertRaisesRegex(perf.Failure, "未知 app session"):
                        perf.app_quiet(self.value, expected_host="10.251.135.3")
                self.assertIn("host(client_addr) AS client_addr", query.call_args.args[1])

    # test_fixture_and_oracle 確認資料級別與真實 20 筆作者關聯、hidden 父／子及 ASCII-only 搜尋。
    def test_fixture_and_oracle(self):
        small = fixture("small")
        medium = fixture("medium")
        self.assertEqual(tuple(len(small[name]) for name in ("accounts", "articles", "comments")), (65, 128, 256))
        self.assertEqual(tuple(len(medium[name]) for name in ("accounts", "articles", "comments")), (2049, 4096, 8192))
        for name in small:
            self.assertEqual(small[name], medium[name][:len(small[name])])
        public = expected_rows(small, "article-comments", "guest", article=1)
        self.assertGreaterEqual(len(public), 40)
        self.assertGreaterEqual(len({row["author"]["id"] for row in public}), 20)
        self.assertGreaterEqual(len(expected_rows(small, "own-articles", "member")), 40)
        self.assertGreaterEqual(len(expected_rows(small, "own-comments", "member")), 40)
        self.assertEqual(expected_rows(small, "members", "admin", "AsCiI"), expected_rows(small, "members", "admin", "ascii"))
        self.assertNotEqual(expected_rows(small, "members", "admin", "Ä"), expected_rows(small, "members", "admin", "ä"))
        self.assertTrue(all(row["role"] == "member" for row in expected_rows(small, "members", "admin", "a")))
        self.assertNotEqual(expected_rows(small, "members", "admin", "%"), expected_rows(small, "members", "admin", ""))

    # test_cases_exact_projection 真正 oracle 拒絕縮減 author／body、錯 total、錯排序與 NUL 政策。
    def test_cases_exact_projection(self):
        matrix = list(cases(fixture("small")))
        self.assertEqual(len({case["id"] for case in matrix}), len(matrix))
        valid = next(case for case in matrix if case["status"] == 200 and case.get("kind") == "public-articles" and case["per_page"] == 20 and case["page"] == 1)
        body = {"success": True, "message": "OK", "data": valid["expected"]}
        verify_response(valid, 200, body)
        for mutation in ("body", "author", "total", "sort", "boolean-page", "float-page", "extra-envelope"):
            incorrect = copy.deepcopy(body)
            if mutation in ("body", "author"):
                del incorrect["data"]["list"][0][mutation]
            elif mutation == "total":
                incorrect["data"]["pagination"]["total"] -= 1
            elif mutation == "sort":
                incorrect["data"]["list"].reverse()
            elif mutation == "boolean-page":
                incorrect["data"]["pagination"]["page"] = True
            elif mutation == "float-page":
                incorrect["data"]["pagination"]["page"] = 1.0
            else:
                incorrect["log_id"] = "unexpected"
            with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                verify_response(valid, 200, incorrect)
        nul = [case for case in matrix if "%00" in case["path"]]
        self.assertEqual(len(nul), 1)
        self.assertEqual(nul[0]["status"], 422)
        rejected = next(case for case in matrix if case["status"] == 401)
        error = {"success": False, "message": "登入已逾期，請重新登入", "error_code": "E0002", "log_id": "sample"}
        verify_response(rejected, 401, error)
        with self.assertRaisesRegex(AssertionError, "envelope 欄位"):
            verify_response(rejected, 401, {**error, "errors": None})

    # test_sensitive_http_failure_diagnostic 原始 transport exception 不得被固定公開診斷帶出。
    def test_sensitive_http_failure_diagnostic(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=OSError("secret-password")):
            with self.assertRaises(perf.Failure) as result:
                perf.request("laravel", "POST", "/api/auth/login", body={"password": "secret-password"})
            self.assertNotIn("secret", str(result.exception))

    # test_plan_reuse_and_provenance 真正重用原 HTTP 窗口的計畫；偽造 role／統計／origin 不能借檔名過關。
    def test_plan_reuse_and_provenance(self):
        self.value.update(level="small", statistics={"level": "small", "analyzed_at": 1, "tables": []})
        case = {"id": "public/guest/20/1", "per_page": 20}
        event = {"query": "SELECT count(*) FROM articles WHERE visibility=$1", "binds": ["visible"], "category": "count",
                 "settings": perf.startup_plan_settings("laravel"), "query_id": str((1 << 64) - 7)}
        window = {"source": self.value["source"], "backend": "laravel", "round": 1, "level": "small", "case": case,
                  "oracle_verified": True, "http": {"status": 200}, "sql": [event]}
        path = self.evidence / "origin.json"
        perf.save(path, window)
        origin = {name: window[name] for name in ("source", "backend", "round", "level")}
        origin.update(case=case["id"], window="origin.json", window_sha256=perf.digest(path.read_bytes()))
        context = {"role": "articles_app", "session_role": "articles_app", "database": "articles", "types": ["text"],
                   "settings": {"default_transaction_read_only": "on", "transaction_read_only": "on", "TimeZone": "Asia/Taipei",
                                "search_path": '"$user", public', "plan_cache_mode": "auto"}}
        explain = [{"Plan": {"Actual Rows": 1}, "Planning Time": 0.1, "Execution Time": 0.2, "Query Identifier": -7}]
        with patch.object(perf, "sql", side_effect=[json.dumps(["text"]), json.dumps(context) + "\n" + json.dumps(explain)]) as sql:
            original = perf.plan(self.value, event, case, origin)
            reused = perf.plan(self.value, event, case, {**origin, "backend": "fastapi", "round": 2})
            self.assertFalse(original["reused"])
            self.assertTrue(reused["reused"])
            self.assertEqual(reused["origin"], origin)
            self.assertEqual(sql.call_count, 2)
            self.assertIn("PREPARE perf01_plan(text) AS", sql.call_args.args[1])
            self.assertIn("EXPLAIN (ANALYZE,BUFFERS,VERBOSE,SETTINGS,FORMAT JSON)", sql.call_args.args[1])
            self.assertTrue(sql.call_args.kwargs["app"])
            plan_path = self.evidence / ("plan-" + original["hash"] + ".json")
            stored = perf.load(plan_path)
            for fault in ("role", "statistics", "origin", "timezone", "search_path", "plan_cache_mode", "native-type", "query-id", "untyped"):
                wrong = copy.deepcopy(stored)
                if fault == "role":
                    wrong["context"]["role"] = "articles_owner"
                elif fault == "statistics":
                    wrong["statistics"]["analyzed_at"] = 2
                elif fault == "origin":
                    wrong["origin"]["source"] = "f" * 64
                elif fault == "timezone":
                    wrong["context"]["settings"]["TimeZone"] = "UTC"
                elif fault == "search_path":
                    wrong["context"]["settings"]["search_path"] = "public"
                elif fault == "plan_cache_mode":
                    wrong["context"]["settings"]["plan_cache_mode"] = "force_generic_plan"
                elif fault == "native-type":
                    wrong["context"]["types"] = ["bigint"]
                elif fault == "query-id":
                    wrong["explain"][0]["Query Identifier"] = -6
                else:
                    del wrong["typing"]
                wrong["proof_sha256"] = perf.digest(perf.encoded({key: value for key, value in wrong.items() if key != "proof_sha256"}))
                perf.save(plan_path, wrong)
                with self.subTest(fault=fault), self.assertRaises(perf.Failure):
                    perf.plan(self.value, event, case, origin)
            perf.save(plan_path, stored)
            perf.save(path, {**window, "oracle_verified": False})
            with self.assertRaisesRegex(perf.Failure, "窗口 hash"):
                perf.plan(self.value, event, case, origin)
            self.assertEqual(sql.call_count, 2)

    # test_native_typed_context_boundaries 保留數字關鍵字文字型別，並區分 psycopg 寬度、Npgsql LIMIT 及 Express UTC。
    def test_native_typed_context_boundaries(self):
        query = "SELECT id FROM articles WHERE visibility=$1 AND id=$2 ORDER BY id LIMIT $3 OFFSET $4"
        inferred = ["text", "bigint", "bigint", "bigint"]
        event = {"query": query, "binds": ["123", "9", "20", "0"], "query_id": "42", "settings": perf.startup_plan_settings("django")}
        boundaries = ((-2147483649, "bigint"), (-2147483648, "integer"), (-32769, "integer"), (-32768, "smallint"),
                      (32767, "smallint"), (32768, "integer"), (2147483647, "integer"), (2147483648, "bigint"))
        for value, kind in boundaries:
            with self.subTest(value=value):
                event["binds"][1] = str(value)
                self.assertEqual(perf.typed_plan_context("django", event, inferred)["parameter_types"], ["text", kind, "smallint", "smallint"])
        self.assertEqual(perf.typed_plan_context("aspnet-core", event, inferred)["parameter_types"], ["text", "bigint", "integer", "bigint"])
        express = {**event, "settings": perf.startup_plan_settings("express")}
        self.assertEqual(perf.typed_plan_context("express", express, inferred)["effective_settings"],
                         {"TimeZone": "UTC", "search_path": '"$user", public', "plan_cache_mode": "auto", "statement_timeout": "10000"})
        laravel = {**event, "settings": [*perf.startup_plan_settings("laravel"), "set time zone 'Asia/Taipei'", 'set search_path to "public"']}
        self.assertEqual(perf.typed_plan_context("laravel", laravel, inferred)["effective_settings"]["search_path"], "public")
        with self.assertRaisesRegex(perf.Failure, "startup SET"):
            perf.typed_plan_context("laravel", {**event, "settings": [*event["settings"], "SET enable_seqscan=off"]}, inferred)
        with self.assertRaisesRegex(perf.Failure, "int8"):
            perf.typed_plan_context("django", {**event, "binds": ["123", str(1 << 63), "20", "0"]}, inferred)

    # test_n_plus_one_counts_all_fetch_queries 刻意逐列取作者會失敗，即使被錯標為 auth；SET 差異則明示保留。
    def test_n_plus_one_counts_all_fetch_queries(self):
        self.assertEqual(perf.category("SELECT nickname FROM accounts WHERE id=$1"), "data-fetch")
        pair = {}
        for size in (1, 20):
            counters = [{"queryid": "1", "query": "SELECT count(*) FROM articles", "calls": 1},
                        {"queryid": "2", "query": "SELECT id FROM articles ORDER BY id LIMIT $1", "calls": 1},
                        {"queryid": "3", "query": "SET TIME ZONE 'Asia/Taipei'", "calls": size}]
            evidence = {"counters": counters, "all_sql_calls": 2 + size}
            pair[size] = perf.fetch_counts(evidence)
        valid = perf.compare_fetch_pair(pair)
        self.assertNotEqual(valid["excluded_1"], valid["excluded_20"])
        for size in (1, 20):
            evidence = {"counters": [{"queryid": "4", "query": "SELECT nickname FROM accounts WHERE id=$1", "calls": size}], "all_sql_calls": size}
            pair[size] = perf.fetch_counts(evidence)
        with self.assertRaisesRegex(perf.Failure, "1→20"):
            perf.compare_fetch_pair(pair)

    # test_nestjs_clock_exact_auth_and_counted 鎖後時間查詢只依精確語句歸類，並保留原生 SELECT 次數。
    def test_nestjs_clock_exact_auth_and_counted(self):
        query = "SELECT floor(extract(epoch FROM clock_timestamp()))::bigint AS now"
        self.assertEqual(perf.category(query), "auth")
        self.assertEqual(perf.category("  " + query.lower().replace(" as ", "\n as ") + "  "), "auth")
        for near in (query + ", 1", query.replace("floor", "ceil"), query.replace("AS now", "AS now_s")):
            with self.subTest(query=near):
                self.assertEqual(perf.category(near), "data-fetch")
        counted = perf.fetch_counts({"counters": [{"queryid": "42", "query": query, "calls": 1}], "all_sql_calls": 1})
        self.assertEqual(counted, {"queries": {"42": 1}, "excluded": {}, "all_sql_calls": 1})

    # test_cold_warm_share_oracle_and_plans 冷／暖機都經正式 measure_case 驗完整內容與每個 count/list 計畫。
    def test_cold_warm_share_oracle_and_plans(self):
        self.value.update(level="small", statistics={"level": "small", "analyzed_at": 1, "tables": []})
        case = next(item for item in cases(fixture("small")) if item.get("kind") == "public-articles" and item["actor"] == "guest" and item["page"] == 1 and item["per_page"] == 20)
        queries = ("SELECT count(*) FROM articles", "SELECT id FROM articles ORDER BY id LIMIT 20")
        emitted = [{"query": query, "binds": [], "category": kind, "session_id": "sample", "query_id": str(index + 1)} for index, (query, kind) in enumerate(zip(queries, ("count", "list")))]
        evidence = {"before": snapshot(), "after": snapshot(2), "counters": [{"queryid": str(index + 1), "query": query, "calls": 1} for index, query in enumerate(queries)],
                    "sql": emitted, "failures": [], "categories": {"count": 1, "list": 1}, "all_sql_calls": 2, "raw": "raw native SQL"}
        response = {"success": True, "message": "OK", "data": case["expected"]}
        directory = self.evidence / "attempt-cold-warm"
        perf.private_dir(directory)
        # captured 提供已完成窗口；正式 oracle／plan／provenance／保存路徑均不替換。
        def captured(*arguments, **kwargs):
            return (200, copy.deepcopy(response)), copy.deepcopy(evidence)
        context = {"role": "articles_app", "session_role": "articles_app", "database": "articles", "types": [],
                   "settings": {"default_transaction_read_only": "on", "transaction_read_only": "on", "TimeZone": "Asia/Taipei",
                                "search_path": '"$user", public', "plan_cache_mode": "auto"}}
        # explained 模擬真實 EXPLAIN 對每條底層 SQL 各自回傳 query ID。
        def explained(value, commands, app):
            self.assertTrue(app)
            query_id = 1 if queries[0] in commands else 2
            return json.dumps(context) + "\n" + json.dumps([{"Plan": {"Actual Rows": 1}, "Planning Time": 0.1, "Execution Time": 0.2, "Query Identifier": query_id}])
        with patch.object(perf, "container_guard"), patch.object(perf, "capture", side_effect=captured), patch.object(perf, "sql", side_effect=explained):
            for label in ("cold", "warm-0"):
                result = perf.measure_case(self.value, "laravel", 1, case, None, {}, directory, label, "10.251.135.3")
                self.assertEqual(len(result["plans"]), 2)
                stored = perf.load(perf.evidence_file(result["window"]))
                self.assertTrue(stored["oracle_verified"])
                self.assertNotIn("expected", stored["case"])
                self.assertEqual(stored["case"]["expected_page_ids"], [row["id"] for row in case["expected"]["list"]])
            response["data"] = {"list": [], "pagination": case["expected"]["pagination"]}
            with self.assertRaises(AssertionError):
                perf.measure_case(self.value, "laravel", 1, case, None, {}, directory, "bad-warm", "10.251.135.3")
            self.assertTrue((directory / "bad-warm-failed-window.json").exists())
            response.clear()
            with self.assertRaisesRegex(AssertionError, "envelope"):
                perf.measure_case(self.value, "laravel", 1, case, None, {}, directory, "bad-envelope", "10.251.135.3")
            self.assertFalse((directory / "bad-envelope-window.json").exists())
            self.assertFalse((directory / "bad-envelope-result.json").exists())
            self.assertNotIn("oracle_verified", perf.load(directory / "bad-envelope-failed-window.json"))

    # test_sql_window_bounds_refuse_before_plan 經 measure_case 檢查重複或無界 SQL 留下失敗窗口，且不能產生合格計畫。
    def test_sql_window_bounds_refuse_before_plan(self):
        case = next(item for item in cases(fixture("small")) if item.get("kind") == "article-comments" and item["actor"] == "guest")
        response = (200, {"success": True, "message": "OK", "data": case["expected"]})
        directory = self.evidence / "bounds"
        perf.private_dir(directory)
        queries = ("SELECT count(*) FROM comments", "SELECT id FROM comments ORDER BY id LIMIT 1",
                   'SELECT "id", "author_id", "visibility" FROM "articles" WHERE "id" = $1 LIMIT 1',
                   "SELECT id FROM login_sessions WHERE id = $1")
        emitted = [{"query": query, "binds": ["1"] if "$1" in query else [], "category": perf.category(query),
                    "session_id": "sample", "query_id": str(index)} for index, query in enumerate(queries, 1)]
        counters = [{"queryid": index, "query": query, "calls": 1, "rows": 1} for index, query in enumerate(queries, 1)]
        evidence = {"sql": emitted, "counters": counters, "failures": []}
        self.assertEqual(perf.verify_window_sql(case, evidence), emitted[:2])
        no_list = {"sql": emitted[:1] + emitted[2:], "counters": counters[:1] + counters[2:], "failures": []}
        empty = {**case, "expected": {**case["expected"], "list": []}}
        self.assertEqual(perf.verify_window_sql(empty, no_list), emitted[:1])
        with self.assertRaisesRegex(perf.Failure, "DB list"):
            perf.verify_window_sql(case, no_list)
        with self.assertRaisesRegex(perf.Failure, "拒絕案例"):
            perf.verify_window_sql({**case, "status": 404}, evidence)

        for fault in ("duplicate-count", "duplicate-list", "full-table", "wrong-route", "wrong-bind", "parent-rows", "auth-rows"):
            with self.subTest(fault=fault):
                window, current = copy.deepcopy(evidence), dict(case)
                if fault in ("duplicate-count", "duplicate-list"):
                    index = 0 if fault == "duplicate-count" else 1
                    window["sql"].append(window["sql"][index].copy())
                    window["counters"][index]["calls"] += 1
                elif fault == "full-table":
                    window["sql"][2].update(query="SELECT id, body FROM articles", binds=[])
                elif fault == "wrong-route":
                    current["path"] = "/api/articles?page=1"
                elif fault == "wrong-bind":
                    window["sql"][2]["binds"] = ["4"]
                else:
                    window["counters"][2 if fault == "parent-rows" else 3]["rows"] = 2
                with patch.object(perf, "container_guard"), patch.object(perf, "capture", return_value=(response, window)), patch.object(perf, "plan") as plan:
                    with self.assertRaises(perf.Failure):
                        perf.measure_case(self.value, "laravel", 1, current, None, {}, directory, fault, "10.251.135.3")
                    plan.assert_not_called()
                self.assertFalse((directory / (fault + "-window.json")).exists())
                self.assertNotIn("oracle_verified", perf.load(directory / (fault + "-failed-window.json")))

    # test_http_unknown_receipt_and_main_cleanup 走 send/read→登入 receipt→main→限定 cleanup；未知結果第二次不會再 POST。
    def test_http_unknown_receipt_and_main_cleanup(self):
        for mode in ("send", "read", "interrupt"):
            with self.subTest(mode=mode):
                self.value["operations"] = []
                perf.save(self.evidence / "state.json", self.value)
                class Response:
                    code = 200
                    # __enter__ 表示 header 已收到，body 仍可能未讀完。
                    def __enter__(self):
                        return self
                    # __exit__ 不吞掉原始未知結果或取消。
                    def __exit__(self, *arguments):
                        return False
                    # read 分別模擬截斷 response 與操作中斷，均不可假設寫入未發生。
                    def read(self, limit):
                        if mode == "interrupt":
                            raise KeyboardInterrupt()
                        raise http.client.IncompleteRead(b"private-partial-response")
                # execute 沿用正式 state 與 authenticate，只有實際 Docker job 被此有限入口取代。
                def execute(owned):
                    perf.state()
                    owned()
                    perf.authenticate(self.value, "laravel", "member")
                with patch.object(perf, "run_matrix", side_effect=execute), patch.object(perf, "stop") as stop, patch("urllib.request.OpenerDirector.open", side_effect=http.client.HTTPException("private-send-error") if mode == "send" else None, return_value=Response()) as send, patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
                    self.assertEqual(perf.main(["run"]), 130 if mode == "interrupt" else 1)
                    self.assertEqual(perf.main(["run"]), 1)
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(stop.call_count, 1)
                    self.assertNotIn("private-", diagnostic.getvalue())
                self.assertEqual(perf.load(self.evidence / "state.json")["operations"][0]["state"], "pending")

    # test_saved_successful_token_is_reused 已確認登入的下一次執行只做 /me 讀回，不重送登入或延長期限。
    def test_saved_successful_token_is_reused(self):
        token = "header." + base64.urlsafe_b64encode(perf.encoded({"exp": int(time.time()) + 3600})).decode().rstrip("=") + ".signature"
        with patch.object(perf, "request", side_effect=[(200, {"success": True, "data": {"access_token": token}}),
                                                        (200, {"data": {"id": 2, "role": "member", "status": "active"}})]) as request:
            self.assertEqual(perf.actor_token(self.value, "laravel", "member"), token)
            self.assertEqual(perf.actor_token(self.value, "laravel", "member"), token)
            self.assertEqual([call.args[1] for call in request.call_args_list], ["POST", "GET"])
        self.assertEqual(len(self.value["operations"]), 1)
        self.assertEqual(self.value["operations"][0]["state"], "complete")

    # test_finite_matrix_retains_required_classes 所有 keyword 首頁仍測 1/20，完整分頁限必要分支與代表搜尋。
    def test_finite_matrix_retains_required_classes(self):
        from perf01_cases import KEYWORDS
        for level in ("small", "medium"):
            matrix = list(cases(fixture(level)))
            self.assertLessEqual(len(matrix), 140)
            for index in range(len(KEYWORDS)):
                first = [case for case in matrix if case.get("kind") == "members" and f"/keyword-{index}/" in case["id"] and case["page"] == 1]
                self.assertEqual({case["per_page"] for case in first}, {1, 20})
            for case in matrix:
                if case["status"] == 200 and case["page"] > 1:
                    self.assertEqual(case["per_page"], 20)
                    if case["kind"] == "members":
                        self.assertIn(int(case["id"].split("keyword-")[1].split("/")[0]), (0, 3, 5, 18))

    # test_partial_run_uses_new_attempt_and_saved_tokens 部分 GET 中斷後保留舊檔，以新 namespace 重跑且不再次登入。
    def test_partial_run_uses_new_attempt_and_saved_tokens(self):
        self.value.update(level="small", schema={"known": True}, statistics={"level": "small", "analyzed_at": 1, "tables": []})
        matrix = [case for case in cases(fixture("small")) if case.get("kind") == "public-articles" and case["actor"] == "guest" and case["page"] == 1]
        for actor, kind, path in (("member", "own-articles", "/api/me/articles"), ("admin", "members", "/api/members")):
            for size in (1, 20):
                base = copy.deepcopy(matrix[0])
                base.update(id=f"{kind}/{actor}/0/keyword-0/{size}/1", kind=kind, actor=actor, per_page=size, path=path)
                matrix.append(base)
        fail = True
        posts = []
        # http 僅模擬登入及保存後 /me 讀回，正式 token receipt／檔案與重用流程仍執行。
        def http(backend, method, path, token=None, body=None):
            if method == "POST":
                actor = "admin" if body["email"] == perf.ADMIN_EMAIL else "member"
                posts.append(actor)
                token = "header." + base64.urlsafe_b64encode(perf.encoded({"exp": int(time.time()) + 3600})).decode().rstrip("=") + ".signature"
                return 200, {"success": True, "data": {"access_token": token}}
            actor = "member" if len(posts) == 2 and http.me_calls % 2 == 0 else "admin"
            http.me_calls += 1
            return 200, {"data": {"id": 2 if actor == "member" else 1, "role": actor, "status": "active"}}
        http.me_calls = 0
        # logging 只替代 DB 邊界，保持 run_backend 的啟停與 finally 分支狀態。
        def logging(value, enabled):
            value["logging"] = enabled
        # measured 留下一份實際私密檔再模擬唯讀窗口中斷，以驗 namespace 與保留行為。
        def measured(value, backend, number, case, token, contexts, directory, label, host):
            perf.save(directory / (label + "-sample.json"), {"case": case["id"]}, exclusive=True)
            if fail and label == "case-0000":
                raise perf.Failure("known read interruption")
            return {"case": case["id"], "fetch": {"queries": {"42": 1}, "excluded": {}, "all_sql_calls": 1}, "phase": label}
        found = {"laravel": {"NetworkSettings": {"Networks": {perf.NETWORK: {"IPAddress": "10.251.135.3"}}}}}
        with patch.object(perf, "verify_fixture"), patch.object(perf, "fixture", return_value={"accounts": [], "articles": [], "comments": []}), patch.object(perf, "cases", return_value=matrix), patch.object(perf, "start_backend"), patch.object(perf, "stop_backend"), patch.object(perf, "logging_settings", side_effect=logging), patch.object(perf, "schema_snapshot", return_value=self.value["schema"]), patch.object(perf, "container_guard", return_value=found), patch.object(perf, "request", side_effect=http), patch.object(perf, "measure_case", side_effect=measured), patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(perf.Failure, "known read"):
                perf.run_backend(self.value, "laravel", 1)
            old = self.evidence / self.value["attempts"][0]["path"] / "case-0000-sample.json"
            original = old.read_bytes()
            fail = False
            perf.run_backend(self.value, "laravel", 1)
        self.assertEqual(posts, ["member", "admin"])
        self.assertEqual(old.read_bytes(), original)
        self.assertNotEqual(self.value["attempts"][0]["path"], self.value["attempts"][1]["path"])
        self.assertEqual([item["status"] for item in self.value["attempts"]], ["incomplete", "complete"])
        self.assertEqual(self.value["completed"], ["small/1/laravel"])
        self.assertTrue(perf.evidence_file(self.value["unit_results"]["small/1/laravel"]["index"]).is_file())


if __name__ == "__main__":
    unittest.main()
