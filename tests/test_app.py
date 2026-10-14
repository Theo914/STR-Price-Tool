import sys
import threading
import urllib.error
import urllib.request

import pytest

from parity.app import (
    _launcher_html,
    build_auto_command,
    create_app_server,
    parse_run_payload,
)


def test_single_launcher_accepts_bare_id():
    request = parse_run_payload({
        "mode": "single",
        "listings": ["1069699"],
        "guests": 2,
        "currency": "AUTO",
    })

    assert request["listings"] == ["1069699"]


def test_single_launcher_accepts_full_url_and_defaults_mode():
    request = parse_run_payload({
        "listings": ["https://www.travelstaytion.com/stay/1069699?source=desk"],
        "guests": 3,
        "currency": "gbp",
    })

    assert request == {
        "mode": "single",
        "listings": ["1069699"],
        "guests": 3,
        "currency": "GBP",
    }


def test_single_launcher_rejects_non_travelstaytion_url():
    with pytest.raises(ValueError, match="TravelStaytion"):
        parse_run_payload({"listings": ["https://example.com/stay/1069699"]})


def test_launcher_rejects_portfolio_mode():
    with pytest.raises(ValueError, match="one TravelStaytion listing"):
        parse_run_payload({"mode": "portfolio", "listings": ["1"]})


def test_single_launcher_rejects_multiple_ids():
    with pytest.raises(ValueError, match="exactly one"):
        parse_run_payload({"mode": "single", "listings": ["1", "2"]})


def test_auto_command_is_argument_list_without_shell_or_arbitrary_paths(tmp_path):
    command = build_auto_command({
        "mode": "single",
        "listings": ["1069699"],
        "guests": 2,
        "currency": "GBP",
    }, tmp_path)

    assert command[:4] == [sys.executable, "-m", "parity.cli", "auto"]
    assert command[4] == "1069699"
    assert "GBP" in command
    # The app has an operator at the visible browser to clear human checks.
    assert "--interactive-captcha" in command


def test_launcher_keeps_javascript_regex_and_newline_escapes_intact():
    rendered = _launcher_html("safe-token")

    assert "join('\\n')" in rendered
    assert "Check one TravelStaytion property" in rendered
    assert "Up to 10" not in rendered
    assert 'id="clear"' in rendered
    assert 'id="canonical"' in rendered
    assert "Unverified candidates are shown as review links" in rendered
    assert "Technical log" in rendered
    assert "--ts:#173f35" in rendered


def test_local_app_serves_launcher_and_rejects_post_without_token(tmp_path):
    server = create_app_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        assert "Find, verify and compare" in body
        request = urllib.request.Request(
            f"http://{host}:{port}/api/run",
            data=b'{"mode":"single","listings":["1069699"]}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        assert caught.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_app_serves_result_downloads(tmp_path):
    (tmp_path / "latest-results.json").write_text('{"ok":true}', encoding="utf-8")
    (tmp_path / "latest-results.csv").write_bytes(b"status\nPRICED\n")
    server = create_app_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/downloads/results.csv", timeout=2
        ) as response:
            assert response.read().decode("utf-8") == "status\nPRICED\n"
            assert response.headers.get_content_type() == "text/csv"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
