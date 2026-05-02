#!/usr/bin/env python3
"""Bidirectional WM1302 Semtech UDP <-> MeshCore KISS bridge.

This bridge implements:
1) Semtech UDP network server endpoint for sx1302 packet forwarder.
2) KISS TNC TCP endpoint compatible with MeshCore KISS modem protocol.

RX path: WM1302 uplink (PUSH_DATA/rxpk) -> KISS DATA frame (+ optional RX_META).
TX path: KISS DATA frame from host -> Semtech PULL_RESP/txpk to packet forwarder.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import random
import socket
import socketserver
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Optional, Tuple, cast

if TYPE_CHECKING:
    from typing import Type

# Semtech UDP protocol identifiers.
PROTOCOL_VERSION = 0x02
PUSH_DATA = 0x00
PUSH_ACK = 0x01
PULL_DATA = 0x02
PULL_RESP = 0x03
PULL_ACK = 0x04
TX_ACK = 0x05

# KISS framing and commands.
KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD

KISS_CMD_DATA = 0x00
KISS_CMD_TXDELAY = 0x01
KISS_CMD_PERSISTENCE = 0x02
KISS_CMD_SLOT_TIME = 0x03
KISS_CMD_TXTAIL = 0x04
KISS_CMD_FULL_DUPLEX = 0x05
KISS_CMD_SETHARDWARE = 0x06

# MeshCore KISS SetHardware commands/responses.
HW_CMD_SET_RADIO = 0x09
HW_CMD_SET_TX_POWER = 0x0A
HW_CMD_GET_RADIO = 0x0B
HW_CMD_GET_TX_POWER = 0x0C
HW_CMD_GET_VERSION = 0x11
HW_CMD_PING = 0x17
HW_CMD_SET_SIGNAL_REPORT = 0x19
HW_CMD_GET_SIGNAL_REPORT = 0x1A

HW_RESP_OK = 0xF0
HW_RESP_ERROR = 0xF1
HW_RESP_TX_DONE = 0xF8
HW_RESP_RX_META = 0xF9

HW_ERR_INVALID_LENGTH = 0x01
HW_ERR_UNKNOWN_CMD = 0x05


@dataclass
class BridgeConfig:
    semtech_listen_host: str
    semtech_listen_port: int
    kiss_listen_host: str
    kiss_listen_port: int
    tx_immediate: bool
    tx_freq_hz: int
    tx_bw_hz: int
    tx_sf: int
    tx_cr: int
    tx_power_dbm: int
    auto_repeat_enabled: bool
    auto_repeat_min_delay_ms: int
    auto_repeat_max_delay_ms: int
    auto_repeat_holdoff_seconds: int
    auto_repeat_require_crc_ok: bool
    auto_repeat_min_rssi: int
    auto_repeat_min_snr_x4: int
    beacon_enabled: bool
    beacon_interval_seconds: int
    beacon_message: str
    log_level: str


class RuntimeState:
    def __init__(self, config: BridgeConfig) -> None:
        self.lock = threading.Lock()
        self.last_pull_addr: Optional[Tuple[str, int]] = None
        self.last_gateway_eui: Optional[str] = None
        self.kiss_client: Optional[socket.socket] = None
        self.signal_report_enabled: bool = True

        self.freq_hz = config.tx_freq_hz
        self.bw_hz = config.tx_bw_hz
        self.sf = config.tx_sf
        self.cr = config.tx_cr
        self.tx_power = config.tx_power_dbm

        # KISS protocol parameters (per KA9Q/K3MC spec)
        self.txdelay_10ms_units = 50  # Default: 500ms
        self.persistence = 63  # Default: 0-255, all values have equal probability
        self.slot_time_10ms_units = 10  # Default: 100ms
        self.txtail_10ms_units = 0  # Default: 0ms
        self.full_duplex = False  # Default: half-duplex

        self.auto_repeat_enabled = config.auto_repeat_enabled
        self.auto_repeat_min_delay_ms = config.auto_repeat_min_delay_ms
        self.auto_repeat_max_delay_ms = config.auto_repeat_max_delay_ms
        self.auto_repeat_holdoff_seconds = config.auto_repeat_holdoff_seconds
        self.auto_repeat_require_crc_ok = config.auto_repeat_require_crc_ok
        self.auto_repeat_min_rssi = config.auto_repeat_min_rssi
        self.auto_repeat_min_snr_x4 = config.auto_repeat_min_snr_x4

        self.beacon_enabled = config.beacon_enabled
        self.beacon_interval_seconds = config.beacon_interval_seconds
        self.beacon_message = config.beacon_message

        self.repeat_seen: Dict[str, float] = {}


class AutoRepeater:
    def __init__(self, state: RuntimeState) -> None:
        self.state = state

    def should_repeat(self, raw_payload: bytes, rxpk: Dict[str, object]) -> bool:
        with self.state.lock:
            if not self.state.auto_repeat_enabled:
                return False
            holdoff = self.state.auto_repeat_holdoff_seconds
            require_crc_ok = self.state.auto_repeat_require_crc_ok
            min_rssi = self.state.auto_repeat_min_rssi
            min_snr_x4 = self.state.auto_repeat_min_snr_x4

        if require_crc_ok:
            stat = rxpk.get("stat")
            if isinstance(stat, (int, float)) and int(stat) != 1:
                return False

        rssi_val = rxpk.get("rssi")
        if isinstance(rssi_val, (int, float)) and int(rssi_val) < min_rssi:
            return False

        snr_val = rxpk.get("lsnr")
        if isinstance(snr_val, (int, float)) and clamp_int8(float(snr_val) * 4.0) < min_snr_x4:
            return False

        key = hashlib.blake2s(raw_payload, digest_size=16).hexdigest()
        now = time.time()

        with self.state.lock:
            last = self.state.repeat_seen.get(key)
            if last is not None and (now - last) < holdoff:
                return False
            self.state.repeat_seen[key] = now

            # Cleanup old dedup entries to bound memory.
            stale = [k for k, t in self.state.repeat_seen.items() if (now - t) > (holdoff * 4)]
            for k in stale:
                del self.state.repeat_seen[k]

        return True

    def repeat_delay_seconds(self) -> float:
        with self.state.lock:
            min_ms = self.state.auto_repeat_min_delay_ms
            max_ms = self.state.auto_repeat_max_delay_ms
        if max_ms < min_ms:
            max_ms = min_ms
        if max_ms == min_ms:
            return float(min_ms) / 1000.0
        return float(random.randint(min_ms, max_ms)) / 1000.0


def hz_to_semtech_datr(sf: int, bw_hz: int) -> str:
    if bw_hz == 125000:
        bw = "BW125"
    elif bw_hz == 250000:
        bw = "BW250"
    elif bw_hz == 500000:
        bw = "BW500"
    else:
        # Packet forwarder expects BW token values, fallback to nearest standard.
        bw = "BW125"
    return f"SF{sf}{bw}"


def coding_rate_token(cr: int) -> str:
    if cr < 5:
        cr = 5
    if cr > 8:
        cr = 8
    return f"4/{cr}"


def clamp_int8(value: float) -> int:
    iv = int(round(value))
    if iv < -128:
        return -128
    if iv > 127:
        return 127
    return iv


def kiss_escape(payload: bytes) -> bytes:
    out = bytearray()
    for b in payload:
        if b == KISS_FEND:
            out.extend((KISS_FESC, KISS_TFEND))
        elif b == KISS_FESC:
            out.extend((KISS_FESC, KISS_TFESC))
        else:
            out.append(b)
    return bytes(out)


def build_kiss_frame(type_byte: int, data: bytes) -> bytes:
    body = bytes([type_byte]) + data
    return bytes([KISS_FEND]) + kiss_escape(body) + bytes([KISS_FEND])


def send_kiss_frame(state: RuntimeState, type_byte: int, data: bytes) -> None:
    frame = build_kiss_frame(type_byte, data)
    with state.lock:
        sock = state.kiss_client
    if sock is None:
        return
    try:
        sock.sendall(frame)
    except OSError:
        logging.warning("KISS client disconnected")
        with state.lock:
            if state.kiss_client is sock:
                state.kiss_client = None


class SemtechUDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = self.request[0]
        sock: socket.socket = self.request[1]

        if len(data) < 4:
            return

        version = data[0]
        token = data[1:3]
        ident = data[3]

        if version != PROTOCOL_VERSION:
            return

        if ident == PUSH_DATA:
            self._on_push_data(sock, token, data)
        elif ident == PULL_DATA:
            self._on_pull_data(sock, token, data)
        elif ident == TX_ACK:
            self._on_tx_ack(data)

    def _on_push_data(self, sock: socket.socket, token: bytes, packet: bytes) -> None:
        server = cast("SemtechUDPServer", self.server)
        if len(packet) < 12:
            return

        gateway_eui = packet[4:12].hex()
        payload = packet[12:]

        ack = bytes([PROTOCOL_VERSION]) + token + bytes([PUSH_ACK])
        sock.sendto(ack, self.client_address)

        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        with server.state.lock:
            server.state.last_gateway_eui = gateway_eui

        rxpk = body.get("rxpk", [])
        if not isinstance(rxpk, list):
            return

        for packet_obj in rxpk:
            if not isinstance(packet_obj, dict):
                continue

            b64_data = packet_obj.get("data")
            if not isinstance(b64_data, str):
                continue

            try:
                raw = base64.b64decode(b64_data, validate=True)
            except Exception:
                continue

            # Push raw packet into KISS Data frame.
            send_kiss_frame(server.state, KISS_CMD_DATA, raw)

            # Optional MeshCore-style metadata notification.
            with server.state.lock:
                signal_report = server.state.signal_report_enabled
            if signal_report:
                snr = clamp_int8(float(packet_obj.get("lsnr", 0.0)) * 4.0)
                rssi = clamp_int8(float(packet_obj.get("rssi", 0.0)))
                meta = struct.pack("bb", snr, rssi)
                send_kiss_frame(server.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_RX_META]) + meta)

            logging.info(
                "RX <- WM1302 gw=%s len=%d rssi=%s snr=%s",
                gateway_eui,
                len(raw),
                packet_obj.get("rssi"),
                packet_obj.get("lsnr"),
            )

            server.maybe_repeat(raw, packet_obj)

    def _on_pull_data(self, sock: socket.socket, token: bytes, packet: bytes) -> None:
        server = cast("SemtechUDPServer", self.server)
        ack = bytes([PROTOCOL_VERSION]) + token + bytes([PULL_ACK])
        sock.sendto(ack, self.client_address)

        gateway_eui = None
        if len(packet) >= 12:
            gateway_eui = packet[4:12].hex()

        with server.state.lock:
            server.state.last_pull_addr = self.client_address
            if gateway_eui:
                server.state.last_gateway_eui = gateway_eui

    def _on_tx_ack(self, packet: bytes) -> None:
        payload = packet[4:]
        if payload:
            try:
                body = json.loads(payload.decode("utf-8"))
                logging.info("TX_ACK <- WM1302 %s", body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                logging.info("TX_ACK <- WM1302")
        else:
            logging.info("TX_ACK <- WM1302")


class SemtechUDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True

    def __init__(self, addr: Tuple[str, int], state: RuntimeState):
        super().__init__(addr, SemtechUDPHandler)
        self.state = state
        self.repeater = AutoRepeater(state)

    def send_txpk(self, payload: bytes, immediate: bool = False) -> bool:
        with self.state.lock:
            addr = self.state.last_pull_addr
            freq_hz = self.state.freq_hz
            bw_hz = self.state.bw_hz
            sf = self.state.sf
            cr = self.state.cr
            power = self.state.tx_power

        if addr is None:
            logging.warning("No active packet forwarder PULL_DATA session, TX dropped")
            return False

        txpk = {
            "imme": immediate,
            "freq": freq_hz / 1_000_000.0,
            "rfch": 0,
            "powe": int(power),
            "modu": "LORA",
            "datr": hz_to_semtech_datr(sf, bw_hz),
            "codr": coding_rate_token(cr),
            "ipol": True,
            "size": len(payload),
            "data": base64.b64encode(payload).decode("ascii"),
        }
        body = json.dumps({"txpk": txpk}, separators=(",", ":")).encode("utf-8")
        token = random.randint(0, 65535)
        header = bytes([PROTOCOL_VERSION]) + struct.pack(">H", token) + bytes([PULL_RESP])

        try:
            self.socket.sendto(header + body, addr)
            logging.info(
                "TX -> WM1302 len=%d freq=%s sf=%s bw=%s cr=%s pwr=%s imme=%s",
                len(payload),
                freq_hz,
                sf,
                bw_hz,
                cr,
                power,
                immediate,
            )
            return True
        except OSError as exc:
            logging.warning("Failed to send PULL_RESP: %s", exc)
            return False

    def maybe_repeat(self, payload: bytes, rxpk: Dict[str, object]) -> None:
        if not self.repeater.should_repeat(payload, rxpk):
            return

        delay_seconds = self.repeater.repeat_delay_seconds()

        def _repeat_worker() -> None:
            time.sleep(delay_seconds)
            ok = self.send_txpk(payload, immediate=True)
            logging.info(
                "AUTO_REPEAT %s len=%d delay_ms=%d",
                "sent" if ok else "dropped",
                len(payload),
                int(delay_seconds * 1000),
            )

        threading.Thread(target=_repeat_worker, name="auto-repeat", daemon=True).start()

    def start_beacon(self) -> None:
        """Start periodic beacon transmission in background thread."""
        with self.state.lock:
            if not self.state.beacon_enabled:
                return
            interval_seconds = self.state.beacon_interval_seconds
            message = self.state.beacon_message

        def _beacon_worker() -> None:
            try:
                payload = message.encode("utf-8")
                while True:
                    time.sleep(interval_seconds)
                    ok = self.send_txpk(payload, immediate=True)
                    logging.info(
                        "BEACON %s len=%d interval=%ds",
                        "sent" if ok else "dropped",
                        len(payload),
                        interval_seconds,
                    )
            except Exception as e:
                logging.error("Beacon thread error: %s", e)

        threading.Thread(target=_beacon_worker, name="beacon", daemon=True).start()


class KISSTCPServer:
    def __init__(self, state: RuntimeState, semtech_server: SemtechUDPServer, host: str, port: int) -> None:
        self.state = state
        self.semtech_server = semtech_server
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None

    def serve_forever(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(1)
        logging.info("KISS endpoint listening on %s:%d", self.host, self.port)

        while True:
            conn, addr = self._sock.accept()
            logging.info("KISS client connected from %s:%d", addr[0], addr[1])

            with self.state.lock:
                old = self.state.kiss_client
                self.state.kiss_client = conn
            if old is not None and old is not conn:
                try:
                    old.close()
                except OSError:
                    pass

            try:
                self._serve_client(conn)
            finally:
                with self.state.lock:
                    if self.state.kiss_client is conn:
                        self.state.kiss_client = None
                try:
                    conn.close()
                except OSError:
                    pass
                logging.info("KISS client disconnected")

    def _serve_client(self, conn: socket.socket) -> None:
        buf = bytearray()
        escaped = False
        in_frame = False

        while True:
            data = conn.recv(4096)
            if not data:
                return
            for b in data:
                if b == KISS_FEND:
                    if in_frame and buf:
                        self._handle_kiss_frame(bytes(buf))
                    buf.clear()
                    escaped = False
                    in_frame = True
                    continue

                if not in_frame:
                    continue

                if b == KISS_FESC:
                    escaped = True
                    continue

                if escaped:
                    escaped = False
                    if b == KISS_TFEND:
                        b = KISS_FEND
                    elif b == KISS_TFESC:
                        b = KISS_FESC
                    else:
                        continue

                if len(buf) < 512:
                    buf.append(b)
                else:
                    buf.clear()
                    in_frame = False
                    escaped = False

    def _handle_kiss_frame(self, frame: bytes) -> None:
        if not frame:
            return

        type_byte = frame[0]
        cmd = type_byte & 0x0F
        port = (type_byte >> 4) & 0x0F
        payload = frame[1:]

        if port != 0:
            return

        if cmd == KISS_CMD_DATA:
            # Queue transmission with CSMA/TXDELAY
            self._queue_transmission(payload)
            return

        if cmd == KISS_CMD_TXDELAY:
            if len(payload) >= 1:
                with self.state.lock:
                    self.state.txdelay_10ms_units = payload[0]
            return

        if cmd == KISS_CMD_PERSISTENCE:
            if len(payload) >= 1:
                with self.state.lock:
                    self.state.persistence = payload[0]
            return

        if cmd == KISS_CMD_SLOT_TIME:
            if len(payload) >= 1:
                with self.state.lock:
                    self.state.slot_time_10ms_units = payload[0]
            return

        if cmd == KISS_CMD_TXTAIL:
            if len(payload) >= 1:
                with self.state.lock:
                    self.state.txtail_10ms_units = payload[0]
            return

        if cmd == KISS_CMD_FULL_DUPLEX:
            if len(payload) >= 1:
                with self.state.lock:
                    self.state.full_duplex = payload[0] != 0
            return

        if cmd == KISS_CMD_SETHARDWARE:
            self._handle_sethardware(payload)

    def _queue_transmission(self, payload: bytes) -> None:
        """Queue transmission with CSMA/TXDELAY handling in background thread."""
        def _tx_worker() -> None:
            with self.state.lock:
                txdelay_ms = self.state.txdelay_10ms_units * 10
                persistence = self.state.persistence
                slot_time_ms = self.state.slot_time_10ms_units * 10
                full_duplex = self.state.full_duplex
                txtail_ms = self.state.txtail_10ms_units * 10

            # For now, use immediate transmission for full-duplex, and delayed for half-duplex
            # Full CSMA/carrier sensing would require RX status from the modem
            if full_duplex:
                # Full duplex: wait TXDELAY then send immediately
                if txdelay_ms > 0:
                    time.sleep(txdelay_ms / 1000.0)
                ok = self.semtech_server.send_txpk(payload, immediate=True)
            else:
                # Half duplex: implement p-persistent CSMA
                # Note: actual carrier sensing would require modem feedback
                # For now, use random backoff with persistence probability
                attempt = 0
                max_attempts = 10
                
                while attempt < max_attempts:
                    # Wait TXDELAY
                    if txdelay_ms > 0:
                        time.sleep(txdelay_ms / 1000.0)
                    
                    # Check persistence: random value 0-255 vs P
                    random_val = random.randint(0, 255)
                    if random_val > persistence:
                        # Don't transmit yet, wait slot time and retry
                        if slot_time_ms > 0:
                            time.sleep(slot_time_ms / 1000.0)
                        attempt += 1
                        continue
                    
                    # Persistence check passed, transmit
                    ok = self.semtech_server.send_txpk(payload, immediate=True)
                    break
                else:
                    # Max attempts exceeded
                    ok = False
                    logging.warning("CSMA max attempts exceeded, TX dropped")

            # Send TxDone response
            send_kiss_frame(
                self.state,
                KISS_CMD_SETHARDWARE,
                bytes([HW_RESP_TX_DONE, 0x01 if ok else 0x00]),
            )

        # Run in background thread to not block frame parsing
        threading.Thread(target=_tx_worker, name="kiss-tx", daemon=True).start()

    def _handle_sethardware(self, payload: bytes) -> None:
        if not payload:
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_ERROR, HW_ERR_INVALID_LENGTH]))
            return

        sub = payload[0]
        data = payload[1:]

        if sub == HW_CMD_SET_RADIO:
            if len(data) < 10:
                send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_ERROR, HW_ERR_INVALID_LENGTH]))
                return
            freq_hz = int.from_bytes(data[0:4], "little", signed=False)
            bw_hz = int.from_bytes(data[4:8], "little", signed=False)
            sf = data[8]
            cr = data[9]
            with self.state.lock:
                self.state.freq_hz = freq_hz
                self.state.bw_hz = bw_hz
                self.state.sf = sf
                self.state.cr = cr
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_OK]))
            return

        if sub == HW_CMD_SET_TX_POWER:
            if len(data) < 1:
                send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_ERROR, HW_ERR_INVALID_LENGTH]))
                return
            with self.state.lock:
                self.state.tx_power = data[0]
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_OK]))
            return

        if sub == HW_CMD_GET_RADIO:
            with self.state.lock:
                freq_hz = self.state.freq_hz
                bw_hz = self.state.bw_hz
                sf = self.state.sf
                cr = self.state.cr
            out = (
                bytes([HW_CMD_GET_RADIO | 0x80])
                + freq_hz.to_bytes(4, "little", signed=False)
                + bw_hz.to_bytes(4, "little", signed=False)
                + bytes([sf, cr])
            )
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, out)
            return

        if sub == HW_CMD_GET_TX_POWER:
            with self.state.lock:
                tx_power = self.state.tx_power
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_CMD_GET_TX_POWER | 0x80, tx_power]))
            return

        if sub == HW_CMD_GET_VERSION:
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_CMD_GET_VERSION | 0x80, 0x01, 0x00]))
            return

        if sub == HW_CMD_PING:
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_CMD_PING | 0x80]))
            return

        if sub == HW_CMD_SET_SIGNAL_REPORT:
            if len(data) < 1:
                send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_ERROR, HW_ERR_INVALID_LENGTH]))
                return
            with self.state.lock:
                self.state.signal_report_enabled = data[0] != 0
                enabled = self.state.signal_report_enabled
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_CMD_GET_SIGNAL_REPORT | 0x80, 0x01 if enabled else 0x00]))
            return

        if sub == HW_CMD_GET_SIGNAL_REPORT:
            with self.state.lock:
                enabled = self.state.signal_report_enabled
            send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_CMD_GET_SIGNAL_REPORT | 0x80, 0x01 if enabled else 0x00]))
            return

        send_kiss_frame(self.state, KISS_CMD_SETHARDWARE, bytes([HW_RESP_ERROR, HW_ERR_UNKNOWN_CMD]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WM1302 Semtech UDP <-> MeshCore KISS bridge")
    parser.add_argument("--semtech-listen-host", default=os.getenv("SEMTECH_LISTEN_HOST", "0.0.0.0"))
    parser.add_argument("--semtech-listen-port", type=int, default=int(os.getenv("SEMTECH_LISTEN_PORT", "1700")))
    parser.add_argument("--kiss-listen-host", default=os.getenv("KISS_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument("--kiss-listen-port", type=int, default=int(os.getenv("KISS_LISTEN_PORT", "8001")))

    parser.add_argument("--tx-freq-hz", type=int, default=int(os.getenv("TX_FREQ_HZ", "868100000")))
    parser.add_argument("--tx-bw-hz", type=int, default=int(os.getenv("TX_BW_HZ", "125000")))
    parser.add_argument("--tx-sf", type=int, default=int(os.getenv("TX_SF", "10")))
    parser.add_argument("--tx-cr", type=int, default=int(os.getenv("TX_CR", "5")))
    parser.add_argument("--tx-power-dbm", type=int, default=int(os.getenv("TX_POWER_DBM", "14")))

    parser.add_argument(
        "--auto-repeat-enabled",
        type=int,
        choices=[0, 1],
        default=int(os.getenv("AUTO_REPEAT_ENABLED", "1")),
    )
    parser.add_argument(
        "--auto-repeat-min-delay-ms",
        type=int,
        default=int(os.getenv("AUTO_REPEAT_MIN_DELAY_MS", "250")),
    )
    parser.add_argument(
        "--auto-repeat-max-delay-ms",
        type=int,
        default=int(os.getenv("AUTO_REPEAT_MAX_DELAY_MS", "1250")),
    )
    parser.add_argument(
        "--auto-repeat-holdoff-seconds",
        type=int,
        default=int(os.getenv("AUTO_REPEAT_HOLDOFF_SECONDS", "60")),
    )
    parser.add_argument(
        "--auto-repeat-require-crc-ok",
        type=int,
        choices=[0, 1],
        default=int(os.getenv("AUTO_REPEAT_REQUIRE_CRC_OK", "1")),
    )
    parser.add_argument(
        "--auto-repeat-min-rssi",
        type=int,
        default=int(os.getenv("AUTO_REPEAT_MIN_RSSI", "-127")),
    )
    parser.add_argument(
        "--auto-repeat-min-snr-x4",
        type=int,
        default=int(os.getenv("AUTO_REPEAT_MIN_SNR_X4", "-128")),
    )

    parser.add_argument(
        "--beacon-enabled",
        type=int,
        choices=[0, 1],
        default=int(os.getenv("BEACON_ENABLED", "0")),
    )
    parser.add_argument(
        "--beacon-interval-seconds",
        type=int,
        default=int(os.getenv("BEACON_INTERVAL_SECONDS", "60")),
    )
    parser.add_argument(
        "--beacon-message",
        default=os.getenv("BEACON_MESSAGE", "MeshCore Beacon"),
    )

    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = BridgeConfig(
        semtech_listen_host=args.semtech_listen_host,
        semtech_listen_port=args.semtech_listen_port,
        kiss_listen_host=args.kiss_listen_host,
        kiss_listen_port=args.kiss_listen_port,
        tx_immediate=True,
        tx_freq_hz=args.tx_freq_hz,
        tx_bw_hz=args.tx_bw_hz,
        tx_sf=args.tx_sf,
        tx_cr=args.tx_cr,
        tx_power_dbm=args.tx_power_dbm,
        auto_repeat_enabled=args.auto_repeat_enabled == 1,
        auto_repeat_min_delay_ms=args.auto_repeat_min_delay_ms,
        auto_repeat_max_delay_ms=args.auto_repeat_max_delay_ms,
        auto_repeat_holdoff_seconds=args.auto_repeat_holdoff_seconds,
        auto_repeat_require_crc_ok=args.auto_repeat_require_crc_ok == 1,
        auto_repeat_min_rssi=args.auto_repeat_min_rssi,
        auto_repeat_min_snr_x4=args.auto_repeat_min_snr_x4,
        beacon_enabled=args.beacon_enabled == 1,
        beacon_interval_seconds=args.beacon_interval_seconds,
        beacon_message=args.beacon_message,
        log_level=args.log_level,
    )

    state = RuntimeState(config)
    semtech_server = SemtechUDPServer((config.semtech_listen_host, config.semtech_listen_port), state)
    kiss_server = KISSTCPServer(state, semtech_server, config.kiss_listen_host, config.kiss_listen_port)

    logging.info(
        "Starting bridge: Semtech %s:%d <-> KISS %s:%d",
        config.semtech_listen_host,
        config.semtech_listen_port,
        config.kiss_listen_host,
        config.kiss_listen_port,
    )

    semtech_thread = threading.Thread(target=semtech_server.serve_forever, name="semtech-udp", daemon=True)
    semtech_thread.start()

    semtech_server.start_beacon()

    try:
        kiss_server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down")
    finally:
        semtech_server.shutdown()
        semtech_server.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
