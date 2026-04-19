from pathlib import Path
import pytest
from doctarr.hw_capability import (
    HWAccelerator,
    detect_from_outputs,
    HWCapabilityReport,
    run_hw_capability,
)
from doctarr.ssh_client import _FakeSSHConnection, SSHClient, SSHRef


FIXTURES = Path(__file__).parent / "fixtures"


def test_detect_intel_quicksync():
    lspci = (FIXTURES / "lspci_intel.txt").read_text()
    dri_ls = "crw-rw---- 1 root root 226, 128 Apr 17 15:54 renderD128"
    accelerators = detect_from_outputs(
        lspci_output=lspci, dri_output=dri_ls, nvidia_smi_output=""
    )
    assert len(accelerators) == 1
    a = accelerators[0]
    assert a.kind == "quicksync"
    assert a.vendor == "Intel"
    assert "UHD Graphics 630" in a.model
    assert "/dev/dri/renderD128" in a.device_paths


def test_detect_nvidia_nvenc():
    lspci = (FIXTURES / "lspci_nvidia.txt").read_text()
    nvidia_smi = (
        "NVIDIA-SMI 535.54.03    Driver Version: 535.54.03    CUDA Version: 12.2"
    )
    accelerators = detect_from_outputs(
        lspci_output=lspci, dri_output="", nvidia_smi_output=nvidia_smi
    )
    assert len(accelerators) == 1
    assert accelerators[0].kind == "nvenc"
    assert accelerators[0].vendor == "NVIDIA"


def test_detect_none_when_no_gpu():
    accelerators = detect_from_outputs(
        lspci_output="", dri_output="", nvidia_smi_output=""
    )
    assert accelerators == []


@pytest.mark.asyncio
async def test_run_hw_capability_gathers_all_hosts():
    intel_lspci = (FIXTURES / "lspci_intel.txt").read_text()
    responses = {
        "lspci": intel_lspci,
        "ls -la /dev/dri": "crw-rw---- 1 root root 226, 128 Apr 17 15:54 renderD128",
        "nvidia-smi": "command not found",
    }
    client = SSHClient(
        ref=SSHRef(host="zion", username="x", password="y"),
        _connection_factory=_FakeSSHConnection.factory(responses),
    )
    report = await run_hw_capability(hosts={"zion": client})
    assert "zion" in report.by_host
    accelerators = report.by_host["zion"]
    assert len(accelerators) == 1
    assert accelerators[0].kind == "quicksync"
