import pandas as pd
from fastapi.testclient import TestClient

from sim_telemetry.cli import main
from sim_telemetry.dashboard import create_app
from sim_telemetry.protocol import FRAME_SIZE

# --- CLI ----------------------------------------------------------------------------------


def test_simulate_preview_prints_frames(capsys):
    assert main(["simulate", "--count", "3", "--seed", "1"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("aa 55 09 00 00")  # sync, length 9, seq 0
    assert "seq=0" in lines[0]


def test_simulate_record_analyze_round_trip(tmp_path, capsys):
    capture = tmp_path / "capture.bin"
    session = tmp_path / "session.csv"

    assert main(["simulate", "--out", str(capture), "--seconds", "60", "--seed", "1"]) == 0
    assert capture.stat().st_size == 60 * 50 * FRAME_SIZE

    assert main(["record", "--port", str(capture), "--out", str(session)]) == 0
    df = pd.read_csv(session)
    assert len(df) == 3000
    assert df["seq"].is_monotonic_increasing

    capsys.readouterr()
    assert main(["analyze", str(session)]) == 0
    out = capsys.readouterr().out
    assert "Samples            3000" in out
    assert "Braking events" in out


def test_record_from_noisy_capture_reports_errors(tmp_path, capsys):
    capture = tmp_path / "noisy.bin"
    session = tmp_path / "noisy.parquet"
    main(["simulate", "--out", str(capture), "--seconds", "20", "--noise", "0.05", "--seed", "2"])
    assert main(["record", "--port", str(capture), "--out", str(session)]) == 0
    out = capsys.readouterr().out
    assert "crc_errors=0 " not in out  # with 5 % noise some CRC errors are certain
    assert 900 < len(pd.read_parquet(session)) < 1000


def test_record_from_loopback_for_a_short_time(tmp_path):
    session = tmp_path / "loop.csv"
    assert main(["record", "--port", "loop://", "--out", str(session), "--seconds", "0.5"]) == 0
    assert len(pd.read_csv(session)) > 5


# --- dashboard ----------------------------------------------------------------------------


def test_dashboard_serves_page_and_streams_over_websocket():
    app = create_app("sim", rate_hz=100, push_interval=0.05)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Chart.js/4.4.1/chart.umd.min.js" in page.text

        samples = []
        with client.websocket_connect("/ws") as ws:
            for _ in range(40):
                message = ws.receive_json()
                assert set(message) == {"samples", "stats", "link"}
                samples.extend(message["samples"])
                if len(samples) >= 10:
                    break
        assert len(samples) >= 10
        assert {"speed_kmh", "gear", "rpm", "throttle_pct"} <= set(samples[0])

        stats = client.get("/api/stats").json()
        assert stats["source"] == "sim"
        assert stats["stats"]["total_frames"] >= 10
