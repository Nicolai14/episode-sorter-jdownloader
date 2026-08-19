"""MyJDownloader connection. Optional: the folder watcher works without it."""
from __future__ import annotations

import threading
from dataclasses import dataclass, asdict
from typing import Any

from .. import config

try:  # myjdapi is optional so the container starts without credentials
    import myjdapi  # type: ignore
except Exception:  # noqa: BLE001
    myjdapi = None  # type: ignore

QUERY = {
    "bytesLoaded": True,
    "bytesTotal": True,
    "childCount": True,
    "comment": True,
    "enabled": True,
    "eta": True,
    "finished": True,
    "hosts": True,
    "running": True,
    "saveTo": True,
    "speed": True,
    "status": True,
    "maxResults": -1,
    "startAt": 0,
}

EXTRACT_WORDS = ("extract", "entpack", "archiv", "unpack")
FAIL_WORDS = ("error", "fehler", "failed", "abort", "crc", "invalid")


@dataclass
class Package:
    uuid: str
    name: str
    save_to: str | None
    state: str | None
    status_text: str | None
    bytes_total: int
    bytes_loaded: int
    speed: int
    eta: int | None
    finished: bool
    extracting: bool
    failed: bool
    source: str  # downloads | linkgrabber

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JDClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._api: Any = None
        self._device: Any = None
        self.last_error: str | None = None
        self.device_name: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(config.get("jd_enabled")) and bool(config.get("jd_email")) and bool(config.get("jd_password"))

    @property
    def connected(self) -> bool:
        return self._device is not None

    def disconnect(self) -> None:
        with self._lock:
            try:
                if self._api is not None:
                    self._api.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._api = None
            self._device = None

    def connect(self) -> bool:
        if not self.enabled:
            self.last_error = "JDownloader connection is disabled in the settings"
            return False
        if myjdapi is None:
            self.last_error = "myjdapi is not installed"
            return False
        with self._lock:
            try:
                api = myjdapi.Myjdapi()
                api.set_app_key("episode-sorter")
                api.connect(str(config.get("jd_email")), str(config.get("jd_password")))
                api.update_devices()
                devices = api.list_devices()
                if not devices:
                    self.last_error = "no JDownloader device is registered on this account"
                    return False
                wanted = str(config.get("jd_device") or "").strip()
                name = wanted or devices[0]["name"]
                self._device = api.get_device(name)
                self._api = api
                self.device_name = name
                self.last_error = None
                return True
            except Exception as exc:  # noqa: BLE001
                self._api = None
                self._device = None
                self.last_error = str(exc)
                return False

    def _ensure(self) -> bool:
        if self._device is not None:
            return True
        return self.connect()

    def devices(self) -> list[str]:
        if myjdapi is None:
            return []
        try:
            api = myjdapi.Myjdapi()
            api.set_app_key("episode-sorter")
            api.connect(str(config.get("jd_email")), str(config.get("jd_password")))
            api.update_devices()
            return [device["name"] for device in api.list_devices()]
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return []

    def packages(self) -> list[Package]:
        if not self._ensure():
            return []
        collected: list[Package] = []
        with self._lock:
            device = self._device
            try:
                downloads = device.downloads.query_packages([dict(QUERY)]) or []
                grabber = device.linkgrabber.query_packages([dict(QUERY)]) or []
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self._device = None
                return []
        for source, rows in (("downloads", downloads), ("linkgrabber", grabber)):
            for row in rows:
                collected.append(_to_package(row, source))
        self.last_error = None
        return collected

    def finished_folders(self) -> list[str]:
        """Folders of packages that finished downloading and are no longer extracting."""
        folders: list[str] = []
        for package in self.packages():
            if package.source != "downloads":
                continue
            if package.finished and not package.extracting and package.save_to:
                folders.append(package.save_to)
        return folders


def host_path(save_to: str | None) -> str | None:
    """JDownloader reports its own container path. Map it onto the watched folder."""
    if not save_to:
        return save_to
    prefix = str(config.get("jd_path_prefix") or "").rstrip("/")
    target = str(config.get("download_dir") or "").rstrip("/")
    if prefix and target and (save_to == prefix or save_to.startswith(prefix + "/")):
        return target + save_to[len(prefix):]
    return save_to


def _to_package(row: dict[str, Any], source: str) -> Package:
    status = str(row.get("status") or "")
    state = str(row.get("statusIconKey") or row.get("state") or "")
    haystack = f"{status} {state}".lower()
    total = int(row.get("bytesTotal") or 0)
    loaded = int(row.get("bytesLoaded") or 0)
    finished = bool(row.get("finished")) or (total > 0 and loaded >= total)
    return Package(
        uuid=str(row.get("uuid")),
        name=str(row.get("name") or "unnamed package"),
        save_to=host_path(row.get("saveTo")),
        state=state or None,
        status_text=status or None,
        bytes_total=total,
        bytes_loaded=loaded,
        speed=int(row.get("speed") or 0),
        eta=row.get("eta"),
        finished=finished and source == "downloads",
        extracting=any(word in haystack for word in EXTRACT_WORDS),
        failed=any(word in haystack for word in FAIL_WORDS),
        source=source,
    )


client = JDClient()
