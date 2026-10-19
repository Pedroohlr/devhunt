from __future__ import annotations

from urllib.parse import urlparse

from .base import Applier


def detect_ats(url: str, known_hosts: dict[str, str]) -> str:
    host = (urlparse(url).hostname or "").lower()
    for k, v in known_hosts.items():
        k = k.lower()
        if host == k or host.endswith("." + k):
            return v
    return "generic"


def get_applier(ats: str) -> Applier:
    from .generic import GenericApplier, GupyApplier, InHireApplier, GreenhouseApplier, LeverApplier, WorkableApplier, RecruteiApplier
    table: dict[str, type[Applier]] = {
        "gupy": GupyApplier,
        "recrutei": RecruteiApplier,
        "inhire": InHireApplier,
        "greenhouse": GreenhouseApplier,
        "lever": LeverApplier,
        "workable": WorkableApplier,
    }
    return table.get(ats, GenericApplier)()
