from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from docktarr.ssh_client import SSHClient

log = logging.getLogger("docktarr.hw")

Kind = Literal["quicksync", "nvenc", "vcn", "videotoolbox", "none"]


@dataclass(frozen=True)
class HWAccelerator:
    kind: Kind
    device_paths: list[str]
    vendor: str
    model: str
    codecs_decode: list[str] = field(default_factory=list)
    codecs_encode: list[str] = field(default_factory=list)
    hdr_tone_mapping: bool = False
    driver_version: str | None = None


@dataclass(frozen=True)
class HWCapabilityReport:
    by_host: dict[str, list[HWAccelerator]] = field(default_factory=dict)


_INTEL_RE = re.compile(
    r"VGA compatible controller.*Intel Corporation\s+([^\[]+)\s*\[([^\]]+)\]\s*\[([0-9a-f:]+)\]",
    re.IGNORECASE,
)
_NVIDIA_RE = re.compile(
    r"VGA compatible controller.*NVIDIA Corporation\s+(\S+)\s*\[([^\]]+)\]",
    re.IGNORECASE,
)
_AMD_RE = re.compile(
    r"VGA compatible controller.*(?:Advanced Micro Devices|AMD)\s+([^\[]+)\s*\[([^\]]+)\]",
    re.IGNORECASE,
)


def detect_from_outputs(
    *, lspci_output: str, dri_output: str, nvidia_smi_output: str
) -> list[HWAccelerator]:
    """Pure function — takes captured command output, returns detected accelerators."""
    accelerators: list[HWAccelerator] = []

    # Intel QuickSync
    if m := _INTEL_RE.search(lspci_output):
        model = m.group(2).strip()
        dev_paths = []
        if "renderD128" in dri_output:
            dev_paths.append("/dev/dri/renderD128")
        if "card0" in dri_output:
            dev_paths.append("/dev/dri/card0")
        # Tone mapping on 9th gen+ (Ice Lake and later). Coffee Lake (8th gen) = decode only for HDR.
        hdr = (
            "ICE" in model.upper()
            or "TIGER" in model.upper()
            or "ALDER" in model.upper()
        )
        accelerators.append(
            HWAccelerator(
                kind="quicksync",
                vendor="Intel",
                model=model,
                device_paths=dev_paths,
                codecs_decode=["h264", "hevc", "vp9"],
                codecs_encode=["h264", "hevc"],
                hdr_tone_mapping=hdr,
            )
        )

    # NVIDIA NVENC
    if (m := _NVIDIA_RE.search(lspci_output)) and "NVIDIA-SMI" in nvidia_smi_output:
        model = m.group(2).strip()
        driver_m = re.search(r"Driver Version:\s*(\S+)", nvidia_smi_output)
        accelerators.append(
            HWAccelerator(
                kind="nvenc",
                vendor="NVIDIA",
                model=model,
                device_paths=["/dev/nvidia0", "/dev/nvidiactl"],
                codecs_decode=["h264", "hevc", "av1", "vp9"],
                codecs_encode=["h264", "hevc", "av1"],
                hdr_tone_mapping=True,
                driver_version=driver_m.group(1) if driver_m else None,
            )
        )

    # AMD VCN
    if m := _AMD_RE.search(lspci_output):
        model = m.group(2).strip()
        dev_paths = ["/dev/dri/renderD128"] if "renderD128" in dri_output else []
        accelerators.append(
            HWAccelerator(
                kind="vcn",
                vendor="AMD",
                model=model,
                device_paths=dev_paths,
                codecs_decode=["h264", "hevc"],
                codecs_encode=["h264", "hevc"],
                hdr_tone_mapping=False,
            )
        )

    return accelerators


async def _detect_on_host(client: SSHClient) -> list[HWAccelerator]:
    lspci = await client.run("lspci 2>/dev/null | grep -iE '(vga|display|3d)' || true")
    dri = await client.run("ls -la /dev/dri/ 2>/dev/null || true")
    nvidia_smi = await client.run("nvidia-smi 2>/dev/null || true")
    return detect_from_outputs(
        lspci_output=lspci.stdout,
        dri_output=dri.stdout,
        nvidia_smi_output=nvidia_smi.stdout,
    )


async def run_hw_capability(hosts: dict[str, SSHClient]) -> HWCapabilityReport:
    by_host: dict[str, list[HWAccelerator]] = {}
    for host, client in hosts.items():
        try:
            by_host[host] = await _detect_on_host(client)
            log.info("hw_capability[%s]: %d accelerator(s)", host, len(by_host[host]))
        except Exception as e:
            log.error("hw_capability[%s] failed: %s", host, e)
            by_host[host] = []
    return HWCapabilityReport(by_host=by_host)
