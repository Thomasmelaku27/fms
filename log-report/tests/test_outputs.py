import json

def test_report():
    """Verify all success criteria from instruction.md."""
    with open("/app/report.json") as f:
        report = json.load(f)

    assert report["total_requests"] == 6
    assert report["unique_ips"] == 3
    assert report["top_path"] == "/index.html"