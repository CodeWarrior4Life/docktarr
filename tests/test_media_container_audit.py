import pytest
from doctarr.media_container_audit import (
    audit_plex_container,
    AuditStatus,
    AuditFinding,
)
from doctarr.docker_manager import ContainerInfo
from doctarr.yaml_config import MediaContainer
from doctarr.hw_capability import HWAccelerator


PLEX_CONTAINER = MediaContainer(
    name="Plex",
    host="zion",
    kind="plex",
    expected_devices=["/dev/dri"],
    auto_remediate=False,
    pref_file="/config/Library/Application Support/Plex Media Server/Preferences.xml",
    required_prefs={"HardwareAcceleratedCodecs": "1"},
)


def test_audit_aligned_when_device_and_pref_present():
    info = ContainerInfo(
        name="Plex",
        status="running",
        image="plex:latest",
        env={"PUID": "1026"},
        device_paths=["/dev/dri"],
    )
    prefs = {"HardwareAcceleratedCodecs": "1"}
    available_hw = [
        HWAccelerator(
            kind="quicksync",
            device_paths=["/dev/dri/renderD128"],
            vendor="Intel",
            model="UHD 630",
        )
    ]
    finding = audit_plex_container(PLEX_CONTAINER, info, prefs, available_hw)
    assert finding.status == AuditStatus.ALIGNED


def test_audit_degraded_when_device_missing_but_hw_available():
    info = ContainerInfo(
        name="Plex",
        status="running",
        image="plex:latest",
        env={},
        device_paths=[],
    )
    prefs = {"HardwareAcceleratedCodecs": "1"}
    hw = [
        HWAccelerator(
            kind="quicksync",
            device_paths=["/dev/dri/renderD128"],
            vendor="Intel",
            model="UHD 630",
        )
    ]
    finding = audit_plex_container(PLEX_CONTAINER, info, prefs, hw)
    assert finding.status == AuditStatus.DEGRADED
    assert "device" in finding.reason.lower()


def test_audit_degraded_when_pref_missing():
    info = ContainerInfo(
        name="Plex",
        status="running",
        image="plex:latest",
        env={},
        device_paths=["/dev/dri"],
    )
    prefs = {}  # HardwareAcceleratedCodecs missing
    hw = [
        HWAccelerator(
            kind="quicksync",
            device_paths=["/dev/dri/renderD128"],
            vendor="Intel",
            model="UHD 630",
        )
    ]
    finding = audit_plex_container(PLEX_CONTAINER, info, prefs, hw)
    assert finding.status == AuditStatus.DEGRADED
    assert "hardwareacceleratedcodecs" in finding.reason.lower()


def test_audit_incapable_when_no_hw_on_host():
    info = ContainerInfo(
        name="Plex", status="running", image="plex:latest", env={}, device_paths=[]
    )
    finding = audit_plex_container(PLEX_CONTAINER, info, {}, available_hw=[])
    assert finding.status == AuditStatus.INCAPABLE
