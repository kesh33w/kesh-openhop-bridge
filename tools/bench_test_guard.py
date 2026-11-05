"""Restrict host test runs to loopback sockets and simulated serial devices."""

import ipaddress
import socket

import serial

_bind = socket.socket.bind
_connect_ex = socket.socket.connect_ex
_connect = socket.socket.connect
_serial_open = serial.Serial.open


def _local(address):
    if not isinstance(address, tuple):
        return
    host = address[0]
    if host == "localhost":
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise PermissionError("Bench tests permit only loopback network addresses")


def _bind_local(self, address):
    _local(address)
    return _bind(self, address)


def _connect_local(self, address):
    _local(address)
    return _connect(self, address)


def _connect_ex_local(self, address):
    _local(address)
    return _connect_ex(self, address)


def _open_simulated(self):
    if self.port and str(self.port).startswith(
        ("/dev/ttyACM", "/dev/ttyUSB", "/dev/serial")
    ):
        raise PermissionError("Bench tests must not open attached radios")
    return _serial_open(self)


def pytest_configure(config):
    socket.socket.bind = _bind_local
    socket.socket.connect = _connect_local
    socket.socket.connect_ex = _connect_ex_local
    serial.Serial.open = _open_simulated
