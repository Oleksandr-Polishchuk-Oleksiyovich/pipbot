from gateway import host_info


def test_host_snapshot_graceful_and_redacted(monkeypatch):
    monkeypatch.setattr(host_info, "_META", "http://127.0.0.1:9/")  # недоступний metadata
    h = host_info.host_snapshot()
    assert h["gce"]["name"].startswith("unavailable")
    for k in ("memory_mb", "cron", "docker", "services_running", "timers", "listening_ports", "top_mem_processes", "os"):
        assert k in h
    assert "_etc_cron.d" in h["cron"]


def test_run_redacts_and_missing_binary(monkeypatch):
    assert host_info._run(["definitely-not-a-binary-xyz"]) is None
    out = host_info._run(["echo", "TOKEN=abcdef123456 hello"])
    assert "abcdef123456" not in " ".join(out)


def test_top_processes_names_only():
    h = host_info.host_snapshot()
    for line in h["top_mem_processes"] or []:
        assert len(line.split()) == 3  # comm rss pmem — без аргументів


def test_gcp_readonly_skipped_without_scope():
    r = host_info._gcp_readonly("p", ["https://www.googleapis.com/auth/devstorage.read_only"])
    assert r["status"].startswith("skipped")
