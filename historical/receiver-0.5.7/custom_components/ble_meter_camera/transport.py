"""Home Assistant managed connection with strict, fail-closed route checks."""
from __future__ import annotations

import asyncio
from importlib.metadata import version

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth

from .protocol import SERVICE, CONTROL, METADATA, DATA, MANIFEST, HEALTH, BOOT_DIAGNOSTIC


class RouteError(RuntimeError):
    """No image traffic is allowed without verified dedicated XIAO ESP32-C6 route."""


SUPPORTED_PACKAGES = {"habluetooth": "7.1.2", "bleak-esphome": "4.0.0",
                      "bleak-retry-connector": "4.7.1", "bleak": "3.0.2"}
REQUIRED_CHARACTERISTICS = (("control", CONTROL), ("metadata", METADATA), ("data", DATA),
                            ("manifest", MANIFEST), ("health", HEALTH), ("bootdiag", BOOT_DIAGNOSTIC))
CACHE_REFRESH_SECONDS = 5


def check_supported_versions() -> dict[str, str]:
    """Private routing shim is deliberately limited to inspected installed code."""
    installed = {package: version(package) for package in SUPPORTED_PACKAGES}
    for package, expected in SUPPORTED_PACKAGES.items():
        if installed[package] != expected:
            raise RouteError(f"routing shim requires reviewed {package} {expected}; installed {installed[package]}; collection blocked")
    return installed


def require_proxy_route(candidates, expected_source: str):
    """Find only the configured proxy, regardless of stronger alternate adapters."""
    matches = [candidate for candidate in candidates
               if candidate.scanner.source.upper() == expected_source.upper()]
    if len(matches) != 1:
        raise RouteError("configured dedicated XIAO ESP32-C6 has no unique connectable camera route")
    candidate = matches[0]
    details = candidate.ble_device.details
    if (not isinstance(details, dict)
            or str(details.get("source", "")).upper() != expected_source.upper()):
        raise RouteError("dedicated XIAO ESP32-C6 BLEDevice source does not match scanner")
    return candidate


def select_managed_backend(client, manager, candidates, expected_source: str):
    """Version-specific local selection hook; all connection management stays HA's.

    No public preferred-source argument exists in the inspected HA implementation.
    Only this client's selection is overridden. Scanner registration, connection
    slot accounting, connecting state, tracking, and teardown remain in HA's
    wrapper and its ESPHome backend; no global Bluetooth state is changed.
    """
    candidate = require_proxy_route(candidates, expected_source)
    if manager.async_scanner_by_source(candidate.scanner.source) is not candidate.scanner:
        raise RouteError("dedicated XIAO ESP32-C6 scanner registration changed")
    select = getattr(client, "_async_get_backend_for_ble_device", None)
    if select is None:
        raise RouteError("installed managed wrapper has no reviewed routing hook")
    backend = select(manager, candidate.scanner, candidate.ble_device)
    if backend is None:
        raise RouteError("dedicated XIAO ESP32-C6 has no available connection slot")
    if str(backend.source).upper() != expected_source.upper():
        raise RouteError("managed backend selection did not preserve dedicated XIAO ESP32-C6 source")
    return backend


def verify_connected_route(client, expected_source: str) -> str:
    """Version-gated evidence, checked before issuing any capture command.

    HA currently has no documented source-pinning argument. A version-specific
    client-local routing hook selects dedicated XIAO ESP32-C6. These backend evidence
    fields are deliberately read only; if the installed wrapper changes, block.
    Never mutate global scanner/adapter state or bypass HA connection management.
    """
    backend = getattr(client, "_backend", None)
    actual = getattr(backend, "_source", None)
    module = type(backend).__module__ if backend is not None else ""
    if not module.startswith("bleak_esphome.") or not isinstance(actual, str):
        raise RouteError("installed HA backend cannot provide ESPHome route evidence")
    scanner = getattr(client, "_connected_scanner", None)
    if scanner is not None and str(scanner.source).upper() != expected_source.upper():
        raise RouteError("managed scanner route changed")
    if actual.upper() != expected_source.upper():
        raise RouteError("actual connected route differs from dedicated XIAO ESP32-C6; image traffic blocked")
    return actual


async def pair_selected_proxy(client, expected_source: str) -> None:
    """Pair only the verified C6 route; encrypted-only camera GATT is enforced."""
    verify_connected_route(client, expected_source)
    async with asyncio.timeout(15):
        paired = await client.pair()
    # bleak-esphome4.0.0 returns None on success and raises on `paired=false`.
    if paired is False:
        raise RouteError("dedicated proxy BLE pairing was rejected")
    verify_connected_route(client, expected_source)


def confirmed_stale_cleanup(client, scanner, expected_source: str) -> dict | None:
    """Read-only proof that a previous timed-out disconnect left no owned link.

    Only exact reviewed versions use this path. A false client flag alone does
    not prove release: current registered proxy slot telemetry must also agree.
    """
    if client.is_connected is not False or getattr(client, "_connected_scanner", ...) is not None:
        return None
    try:
        verify_connected_route(client, expected_source)
    except RouteError:
        return None
    return current_proxy_free_proof(scanner, expected_source)


def current_proxy_free_proof(scanner, expected_source: str) -> dict | None:
    """Authoritative current source-specific free-slot evidence, no network writes."""
    if (scanner is None or str(scanner.source).upper() != expected_source.upper()
            or scanner.connectable is not True or scanner.scanning is not True
            or scanner.connector is None or not scanner.connector.can_connect()):
        return None
    allocations = scanner.get_allocations()
    if (allocations is None or str(allocations.adapter).upper() != expected_source.upper()
            or type(allocations.slots) is not int or allocations.slots <= 0
            or type(allocations.free) is not int or allocations.free != allocations.slots
            or not isinstance(allocations.allocated, (list, tuple, set)) or allocations.allocated):
        return None
    return {"method": "disconnected_untracked_and_current_proxy_all_slots_free",
            "source": expected_source, "slots": allocations.slots, "free": allocations.free}


class ManagedTransport:
    def __init__(self, hass, address: str, source: str) -> None:
        self.hass = hass
        self.address = address
        self.source = source
        self.client = None
        self.dependencies: dict[str, str] = {}
        self.pairing_completed = False
        self.characteristic_handles: dict[str, int] = {}
        self._close_attempted = False
        self.last_cleanup_proof: dict = {}
        self.mtu_size: int | None = None
        self.connect_subphase = "idle"
        self.cache_refresh_attempted = False

    def connect_diagnostics(self) -> dict:
        """Read public cached properties only; never request services or GATT."""
        client = self.client
        snapshot = {"subphase": self.connect_subphase, "client_exists": client is not None,
                    "client_connected": None if client is not None else False, "cached_services_available": False}
        if client is None:
            return snapshot
        try:
            snapshot["client_connected"] = bool(client.is_connected)
        except Exception as err:
            snapshot["connected_property_error_type"] = type(err).__name__
        try:
            services = client.services
            snapshot["cached_services_available"] = services is not None
            snapshot["cached_expected_characteristics"] = {
                name: services.get_characteristic(uuid) is not None
                for name, uuid in REQUIRED_CHARACTERISTICS
            } if services is not None else {}
        except Exception as err:
            snapshot["services_property_error_type"] = type(err).__name__
        return snapshot

    async def connect(self) -> str:
        if self.client is not None:
            raise RouteError("retained BLE client must be confirmed closed before connecting")
        self.pairing_completed = False
        self.cache_refresh_attempted = False
        self._close_attempted = False
        # These fields describe this attempt, not a previous successful link.
        self.characteristic_handles = {}
        self.mtu_size = None
        self.connect_subphase = "dependency_check"
        self.dependencies = await self.hass.async_add_executor_job(check_supported_versions)

        def candidates():
            return bluetooth.async_scanner_devices_by_address(self.hass, self.address, connectable=True)

        self.connect_subphase = "route_discovery"
        candidate = require_proxy_route(candidates(), self.source)
        owner = self

        class TrackedClient(BleakClientWithServiceCache):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                # Retain partial clients so cancellation/failure cleanup can close them.
                owner.client = self

            def _async_get_best_available_backend_and_device(self, manager):
                return select_managed_backend(self, manager, candidates(), owner.source)

            async def connect(self, **kwargs):
                owner.connect_subphase = "managed_connect_and_services"
                await super().connect(**kwargs)
                owner.connect_subphase = "services_ready_route_check"
                verify_connected_route(self, owner.source)

        # Firmware upgrades can leave the ESPHome proxy's per-device GATT cache
        # describing the old service. Refresh only this managed, pinned client,
        # before CONFIG/CAPTURE, at most once within the caller's connect budget.
        for attempt in range(2):
            candidate = require_proxy_route(candidates(), self.source)
            self.connect_subphase = "establish_connection" if not attempt else "uncached_service_reconnect"
            self.client = await establish_connection(
                TrackedClient, candidate.ble_device, "BLE meter camera",
                max_attempts=1, timeout=15, services=[SERVICE],
                use_services_cache=not attempt,
            )
            verify_connected_route(self.client, self.source)
            self.connect_subphase = "pair_request"
            await pair_selected_proxy(self.client, self.source)
            self.pairing_completed = True
            self.connect_subphase = "mtu_and_characteristics"
            negotiated_mtu = self.client.mtu_size
            if type(negotiated_mtu) is not int or not 23 <= negotiated_mtu <= 517:
                raise RouteError("managed client did not report a valid ATT MTU")
            self.mtu_size = negotiated_mtu
            self.characteristic_handles = {}
            for name, uuid in REQUIRED_CHARACTERISTICS:
                if characteristic := self.client.services.get_characteristic(uuid):
                    self.characteristic_handles[name] = characteristic.handle
            missing = [name for name, _ in REQUIRED_CHARACTERISTICS if name not in self.characteristic_handles]
            source = verify_connected_route(self.client, self.source)
            if not missing:
                self.connect_subphase = "ready"
                return source
            if attempt:
                raise RouteError("camera required characteristics still missing after one cache refresh: " + ", ".join(missing))
            self.cache_refresh_attempted = True
            self.connect_subphase = "connected_service_cache_clear"
            if self.client.is_connected is not True:
                raise RouteError("service cache refresh requires the owned connected camera client")
            # bleak-esphome4.0.0 clears its in-memory services/MTU, then asks the
            # selected proxy to clear this camera's cache while the link is up.
            # A disconnected API client cannot perform that operation.
            async with asyncio.timeout(CACHE_REFRESH_SECONDS):
                cleared = await self.client.clear_cache()
            verify_connected_route(self.client, self.source)
            if cleared is not True:
                raise RouteError("dedicated proxy did not confirm camera service cache refresh")
            self.connect_subphase = "service_cache_disconnect"
            async with asyncio.timeout(CACHE_REFRESH_SECONDS):
                await self.close()
            self.pairing_completed = False
            self.mtu_size = None
            self.characteristic_handles = {}
            await asyncio.sleep(0)
        raise RouteError("camera service discovery did not complete")

    async def write(self, characteristic: str, value: bytes) -> None:
        verify_connected_route(self.client, self.source)
        await self.client.write_gatt_char(characteristic, value, response=True)

    async def read(self, characteristic: str) -> bytes:
        verify_connected_route(self.client, self.source)
        return bytes(await self.client.read_gatt_char(characteristic))

    @property
    def retained_client_connected(self) -> bool | None:
        if self.client is None:
            return False
        try:
            return bool(self.client.is_connected)
        except Exception:
            return None

    async def recover_close(self) -> None:
        """A restarted runtime still requires current proxy proof before resuming."""
        if self.client is not None:
            await self.close()
            return
        await self.hass.async_add_executor_job(check_supported_versions)
        scanner = bluetooth.async_scanner_by_source(self.hass, self.source)
        proof = current_proxy_free_proof(scanner, self.source)
        if not proof:
            raise RouteError("configured proxy has not confirmed all BLE slots free")
        self.last_cleanup_proof = {**proof, "method": "fresh_runtime_no_client_and_current_proxy_all_slots_free"}

    async def close(self) -> None:
        if self.client is not None:
            client = self.client
            if self._close_attempted:
                await self.hass.async_add_executor_job(check_supported_versions)
                scanner = bluetooth.async_scanner_by_source(self.hass, self.source)
                proof = confirmed_stale_cleanup(client, scanner, self.source)
                if proof:
                    # The library already untracked its down backend. Drop only
                    # this transport reference; never mutate HA/global BLE state.
                    self.last_cleanup_proof = proof
                    self.client = None
                    self._close_attempted = False
                    return
                if client.is_connected is False:
                    # The old API client can never confirm another disconnect
                    # after proxy restart. Wait for current slot telemetry;
                    # resending to that stale client adds no release evidence.
                    raise RouteError("disconnected client awaits current dedicated proxy free-slot proof")
            self._close_attempted = True
            await client.disconnect()
            if self.client is client:
                self.client = None
                self._close_attempted = False
                self.last_cleanup_proof = {"method": "managed_disconnect_completed", "source": self.source}
