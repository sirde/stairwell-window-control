#!/usr/bin/env python3
# -*- coding:utf-8 -*-
import socket
import threading
import time

import config









class ModbusTCPClient:
    def __init__(self, host: str, port: int = 502):
        self.host = host
        self.port = port
        self.socket = None
        self.lock = threading.Lock()
        self.release_timer = None

    def connect(self):
        self.socket = socket.socket()
        self.socket.settimeout(3)
        self.socket.connect((self.host, self.port))
        print(f"Connected to {self.host}:{self.port}")

    def close(self):
        if self.socket:
            self.socket.close()
            print("Connection closed")

    def _release_all(self):
        time.sleep(10)  # wait before releasing
        with self.lock:
            try:
                self.connect()
                self.all_relay_off()
                self.close()
            except Exception as e:
                print(f"Release error: {e}")
            finally:
                self.release_timer = None

    def _schedule_release(self):
        if self.release_timer:
            self.release_timer.cancel()  # reset timer if already running
        self.release_timer = threading.Timer(10.0, self._release_all)
        self.release_timer.start()

    def send_command(self, command: list) -> bytes:
        if not self.socket:
            raise Exception("Socket not connected.")
        self.socket.send(bytearray(command))
        time.sleep(0.2)
        response = self.socket.recv(1024)
        return response

    def write_coil(self, channel: int, value: int):
        """Sends a Write Coil command (0x05) to the device."""
        print("Write coil %s" % channel)
        cmd = [0] * 12
        cmd[5] = 0x06  # Byte length
        cmd[6] = 0x01  # Device address
        cmd[7] = 0x05  # Command: Write single coil
        cmd[8] = 0x00
        cmd[9] = channel
        cmd[10] = 0xFF if value else 0x00
        cmd[11] = 0x00
        resp = self.send_command(cmd)
        print(f"[WRITE COIL ch={channel} val={value}] → [{', '.join(hex(x) for x in resp)}]")

        with self.lock:
            self._schedule_release()

    def all_relay_off(self):
        """Sends a Write Coil command (0x05) to the device."""
        print("Release all relays")
        cmd = [0] * 12
        cmd[5] = 0x06  # Byte length
        cmd[6] = 0x01  # Device address
        cmd[7] = 0x05  # Command: Write single coil
        cmd[8] = 0x00
        cmd[9] = 0xFF
        cmd[10] = 0x00 # Relay off
        cmd[11] = 0x00
        resp = self.send_command(cmd)
        print(f"[RELEASE ALL COILS] → [{', '.join(hex(x) for x in resp)}]")

    def read_inputs(self):
        """Sends a Read Discrete Inputs command (0x02)."""
        cmd = [0] * 12
        cmd[5] = 0x06
        cmd[6] = 0x01
        cmd[7] = 0x02
        cmd[8] = 0x00
        cmd[9] = 0x00
        cmd[10] = 0x00
        cmd[11] = 0x08  # Read 8 bits
        resp = self.send_command(cmd)
        if len(resp) > 9:
            print("Input status:", hex(resp[9]))
        else:
            print("Unexpected response:", resp)

# --- TCP placeholder command handler ---
def send_window_command(building, window_id, action):
    # This would send a TCP command in a real app
    print(f"[{building.upper()}] Window {window_id}: {action.upper()}")



    try:

        ip_address = config.module_address[building]
        tcp_client = ModbusTCPClient(ip_address)

        tcp_client.connect()

        relay_number_open = config.BUILDINGS[building].index(window_id) * 2
        relay_number_close =  relay_number_open + 1
        if action == "close":
            tcp_client.write_coil(relay_number_open, 0)
            tcp_client.write_coil(relay_number_close, 1)
        else:
            tcp_client.write_coil(relay_number_open, 1)
            tcp_client.write_coil(relay_number_close, 0)

        tcp_client.close()
    except Exception as e:
        print(f"error: {e}")
        return False, f"Failed to send tcp command: {e}"

    return True, "Command sent"

if __name__ == "__main__":
    client = ModbusTCPClient("192.168.2.200")

    try:
        client.connect()

        # Turn ON channels 0-7
        for i in range(2):
            client.write_coil(i, 1)

        # Turn OFF channels 0-7
        for i in range(2):
            client.write_coil(i, 0)

        # Read input status
        client.read_inputs()

    finally:
        client.close()