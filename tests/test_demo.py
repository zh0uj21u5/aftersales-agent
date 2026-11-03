"""Exercise the offline release demo through the real harness and stores."""

import io
import re
from pathlib import Path

import pytest
from rich.console import Console

from aftersales.core.tracing import read_trace
from scripts.demo import run_demo


def test_demo_proves_consent_ownership_approval_and_refund(tmp_path: Path) -> None:
    output = io.StringIO()
    evidence = run_demo(
        tmp_path / "demo",
        Console(file=output, force_terminal=True, color_system="truecolor", legacy_windows=False),
    )
    assert evidence[0].seed_hashes == evidence[1].seed_hashes
    # Lines 26000/14000, coupon2000 -> paid24700/13300; 3000*2*24700/26000=5700.
    assert evidence[0].amount_cents == 5700
    # B: 15000*3=45000 cents, no coupon/prior refund; 45000>20000 needs approval.
    assert evidence[1].amount_cents == 45000
    assert evidence[1].blocked_rules == ("P01", "P02", "P08")
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
    assert "APIToken=0" in re.sub(r"\s+", "", plain) and "\x1b[" in output.getvalue()
    for scene in evidence:
        assert read_trace(scene.trace)[-1].event == "run_end"
    with pytest.raises(FileExistsError):
        run_demo(tmp_path / "demo", Console(file=io.StringIO()))
