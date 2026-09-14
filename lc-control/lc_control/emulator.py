"""仅监听本机回环地址的 Modbus TCP 设备夹具。

用独立线程推进合成热工和设备看门狗，客户端停止轮询也会触发超时接管。
寄存器是虚构测试地址；故障开关用于模拟超时、错误响应和写后丢回包。
该服务用于验证真实 TCP 报文链路，不是生产设备代理或标准完整性认证服务器。"""
import socketserver
import struct
import threading
import time
from .modbus import decode, encode, width
from .plant import ThermalPlant


class Emulator:
    """合成设备服务及独立物理/看门狗线程，供协议集成测试使用。"""
    def __init__(self, scene, port=None):
        domain = scene["control_domains"][0]
        self.profile = scene["devices"][domain["cdu_id"]]
        self.plant = ThermalPlant(domain["cdu_id"], self.profile, domain["owner"])
        self.owner = domain["owner"]
        self.registers = {}
        self.lock = threading.RLock()
        self.writes = []
        self.drop_after_write = False
        self.wrong_transaction = False
        self.force_exception = False
        self.delay_s = 0
        self.last_heartbeat = time.monotonic()
        self.previous_heartbeat = None
        self.sample_counter = 0
        self.freeze_sample_counter = False
        self.stop = threading.Event()
        self.refresh()
        outer = self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(2)
                def receive(count):
                    data = b""
                    while len(data) < count:
                        chunk = self.request.recv(count - len(data))
                        if not chunk:
                            raise ConnectionError()
                        data += chunk
                    return data
                try:
                    header = receive(7)
                    tx, protocol, size, unit = struct.unpack(">HHHB", header)
                    if protocol or not 2 <= size <= 254:
                        return
                    pdu = receive(size - 1)
                    if outer.delay_s:
                        time.sleep(outer.delay_s)
                    reply = outer.respond(pdu)
                    if outer.drop_after_write and pdu[0] in (6, 16):
                        return
                    if outer.wrong_transaction:
                        tx = (tx + 1) % 65536
                    self.request.sendall(struct.pack(">HHHB", tx, 0, len(reply) + 1, unit) + reply)
                except (OSError, ConnectionError, ValueError, struct.error):
                    return
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        self.server = Server(("127.0.0.1", port if port is not None else self.profile["connection"]["port"]), Handler)
        self.port = self.server.server_address[1]

    def put(self, point, value):
        """把工程量按虚构点表编码到内部测试寄存器。"""
        data = encode(value, point)
        for i in range(width(point)):
            self.registers[point["address"] + i] = data[2 * i:2 * i + 2]

    def refresh(self):
        """同步合成对象的观测、目标与状态到寄存器，不能用于真实设备。"""
        points = self.profile["points"]
        for name, p in points["observations"].items():
            self.put(p, self.plant.observations[name]["value"])
        for name, p in points["setpoints"].items():
            self.put(p, self.plant.setpoints[name])
        for name, p in points["status"].items():
            value = self.sample_counter if name == "sample_counter" else (1 if self.plant.owner == self.owner else 0) if name == "owner" else (
                1 if self.plant.operating_mode != "local_auto" else 0) if name == "mode" else int(bool(self.plant.alarms))
            self.put(p, value)
        for p in points["commands"].values():
            for i in range(width(p)):
                self.registers.setdefault(p["address"] + i, b"\0\0")

    def respond(self, pdu):
        """解析有限功能码并返回 TCP PDU；故意保留故障注入开关用于验证客户端失败路径。"""
        with self.lock:
            fc = pdu[0]
            if self.force_exception:
                return bytes((fc | 0x80, 4))
            try:
                address, count = struct.unpack(">HH", pdu[1:5])
                if fc in (3, 4):
                    if len(pdu) != 5 or not 1 <= count <= 125:
                        raise ValueError()
                    data = b"".join(self.registers[address + i] for i in range(count))
                    return bytes((fc, len(data))) + data
                if fc in (1, 2) and count == 1 and len(pdu) == 5:
                    return bytes((fc, 1, int(bool(int.from_bytes(self.registers[address], "big")))))
                if fc == 6:
                    data, count = pdu[3:5], 1
                elif fc == 16 and len(pdu) >= 6 and pdu[5] == count * 2 and len(pdu) == 6 + count * 2:
                    data = pdu[6:]
                else:
                    return bytes((fc | 0x80, 1))
                matched = [(group, name, p) for group in ("setpoints", "commands")
                           for name, p in self.profile["points"][group].items()
                           if p.get("write_address", p["address"]) == address and width(p) == count and p["write_function"] == fc]
                if len(matched) != 1:
                    raise KeyError()
                group, name, point = matched[0]
                value = decode(data, point)
                if group == "setpoints":
                    cap = self.profile["controls"][name]
                    if self.plant.owner != self.owner or not cap["minimum"] <= value <= cap["maximum"]:
                        raise ValueError()
                    self.plant.setpoints[name] = value
                elif name == "release":
                    if value == point["value"]:
                        self.plant.owner, self.plant.operating_mode = None, "local_auto"
                elif name == "heartbeat" and value != self.previous_heartbeat:
                    self.last_heartbeat, self.previous_heartbeat = time.monotonic(), value
                self.writes.append((name, value))
                self.put(point, value)
                self.refresh()
                return pdu[:5]
            except KeyError:
                return bytes((fc | 0x80, 2))
            except (ValueError, struct.error):
                return bytes((fc | 0x80, 3))

    def physics(self):
        """后台持续推进合成热工并检查失联时间，独立于客户端是否读取数据。"""
        previous = time.monotonic()
        while not self.stop.wait(0.1):
            now = time.monotonic()
            with self.lock:
                if now - self.last_heartbeat > self.profile["commissioning"]["watchdog_timeout_s"]:
                    self.plant.owner, self.plant.operating_mode = None, "local_auto"
                load = 75000 + 15000 * (int(self.plant.elapsed // 60) % 2)
                self.plant.advance(now - previous, load)
                if not self.freeze_sample_counter:
                    self.sample_counter = (self.sample_counter + 1) % 65536
                self.refresh()
            previous = now

    def start(self):
        """启动临时 TCP 服务和物理线程，返回本夹具对象。"""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.physics_thread = threading.Thread(target=self.physics, daemon=True)
        self.thread.start()
        self.physics_thread.start()
        return self

    def close(self):
        """停止所有测试线程并关闭监听端口。"""
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.physics_thread.join(timeout=2)
