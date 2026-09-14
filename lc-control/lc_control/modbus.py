"""真实 Modbus TCP 通信与 CDU 点表适配。

分三层：标量编解码 → TCP 功能码交换 → 标准 Snapshot/ControlRequest 映射。
读支持 FC01/02 单个状态位、FC03/04 寄存器；写仅 FC06/16 寄存器。
所有地址为 PDU 零基地址；write_address 可与读取地址不同。
读写都有截止时间；写失败不重试，因为响应丢失不代表设备没有执行。
这里没有任何厂家预置寄存器，也不提供 Modbus 的加密/认证。"""
import math
import ipaddress
import socket
import struct
import time
from dataclasses import replace

from .configuration import finite
from .contracts import Reading, Snapshot, describe_profile

FORMATS = {"uint16": "H", "int16": "h", "uint32": "I", "int32": "i", "float32": "f", "bool": "H"}


def width(point):
    """返回标量占用的 16 位寄存器数量；bool 在单点位读取分支单独解释。"""
    return struct.calcsize(FORMATS[point["dtype"]]) // 2


def reorder(data, point):
    """按设备点表调整寄存器内字节序及 32 位值的字序。
    该变换正反相同；编码/解码都调用，不能由主机端序猜测设备端序。"""
    words = [data[i:i + 2] for i in range(0, len(data), 2)]
    if point.get("byte_order", "big") == "little":
        words = [w[::-1] for w in words]
    if point.get("word_order", "big") == "little":
        words.reverse()
    return b"".join(words)


def decode(data, point):
    """把响应字节换算为内部工程量：value = raw × scale + offset。
    拒绝 NaN/Inf 和厂家声明的无效原始码；单位由点表事先核对。"""
    raw = struct.unpack(">" + FORMATS[point["dtype"]], reorder(data, point))[0]
    if not math.isfinite(raw) or raw in point.get("invalid_raw", []):
        raise ValueError("invalid_register_value")
    value = raw * point.get("scale", 1) + point.get("offset", 0)
    if not math.isfinite(value):
        raise ValueError("invalid_scaled_value")
    return value


def encode(value, point):
    """将工程量反算为设备原始值。整数按最近值量化；网关还会复核量化后的工程边界。"""
    raw = (value - point.get("offset", 0)) / point.get("scale", 1)
    if not math.isfinite(raw):
        raise ValueError("nonfinite_register_write")
    if point["dtype"] != "float32":
        raw = round(raw)
    if raw in point.get("invalid_raw", []):
        raise ValueError("reserved_register_value")
    try:
        return reorder(struct.pack(">" + FORMATS[point["dtype"]], raw), point)
    except (struct.error, OverflowError):
        raise ValueError("register_value_not_representable") from None


def validate_profile(profile):
    """校验协议绑定本身，不发报文。
    检查地址空间、类型、缩放、枚举、写功能和写地址冲突；缺少控制状态只允许监测。"""
    def need(ok, reason):
        if not ok:
            raise ValueError(reason)
    conn = profile["connection"]
    need(isinstance(conn.get("host"), str) and bool(conn["host"]), "modbus_host_required")
    # Literal IP avoids unbounded DNS resolution in a time-limited control cycle.
    ipaddress.ip_address(conn["host"])
    for name, lo, hi in (("port", 1, 65535), ("unit_id", 1, 247)):
        need(type(conn.get(name)) is int and lo <= conn[name] <= hi, "invalid_" + name)
    need(finite(conn.get("timeout_s")) and 0 < conn["timeout_s"] <= 30, "invalid_timeout")
    points = profile["points"]
    need(set(points["setpoints"]) == set(profile["controls"]), "mapped_controls_mismatch")
    need(isinstance(points["status"], dict), "status_mapping_required")
    for group, mappings in points.items():
        for name, point in mappings.items():
            need(point.get("dtype") in FORMATS, "unsupported_register_type")
            need(type(point.get("address")) is int and 0 <= point["address"] <= 65536 - width(point),
                 "explicit_zero_based_address_required")
            need(point.get("read_function") in (1, 2, 3, 4), "unsupported_read_function")
            if point["read_function"] in (1, 2):
                need(point["dtype"] == "bool" and point.get("scale", 1) == 1
                     and point.get("offset", 0) == 0, "bit_point_requires_unscaled_bool")
            else:
                need(point["dtype"] != "bool", "bool_requires_bit_function")
            need(point.get("byte_order", "big") in ("big", "little") and
                 point.get("word_order", "big") in ("big", "little"), "invalid_register_order")
            need(finite(point.get("scale", 1)) and point.get("scale", 1) != 0 and
                 finite(point.get("offset", 0)), "invalid_register_conversion")
            need(isinstance(point.get("unit"), str), "point_unit_required")
            if group == "setpoints" or group == "commands":
                # 当前写入仍限定寄存器；FC05 远程停机不能冒充控制权释放。
                need(point["dtype"] != "bool", "coil_write_not_supported")
                need(point.get("write_function") in (6, 16), "explicit_write_function_required")
                address = point.get("write_address", point["address"])
                need(type(address) is int and 0 <= address <= 65536 - width(point), "invalid_write_address")
                need(point["write_function"] != 6 or width(point) == 1, "fc6_requires_one_register")
                if group == "setpoints":
                    need(point["unit"] == profile["controls"][name]["unit"], "mapped_control_unit_mismatch")
                    tolerance = point.get("tolerance", 0)
                    need(finite(tolerance) and 0 <= tolerance < profile["controls"][name]["max_step"] / 2,
                         "invalid_readback_tolerance")
    for name in ("mode", "owner"):
        if name not in points["status"]:
            continue  # 只读接入允许缺少控制状态，但不能因此宣布自动控制可用。
        need(bool(points["status"][name].get("enum")), "status_enum_required")
        need(points["status"][name]["dtype"] in ("uint16", "int16", "uint32", "int32"),
             "integer_status_required")
    # Catch accidental overlap of command registers before any connection is made.
    occupied = set()
    for p in list(points["setpoints"].values()) + list(points.get("commands", {}).values()):
        address = p.get("write_address", p["address"])
        addresses = set(range(address, address + width(p)))
        need(not occupied.intersection(addresses), "overlapping_write_registers")
        occupied.update(addresses)


class ModbusTCPClient:
    """同步 Modbus TCP 客户端。每次交换新建连接，低依赖但吞吐有限；不做写重试。"""
    def __init__(self, connection):
        self.connection = connection
        self.transaction = 0

    def exchange(self, pdu, deadline=None):
        """发送一个 PDU 并校验响应，将不同 Python 版本的 socket.timeout 统一为 TimeoutError。"""
        try:
            return self._exchange(pdu, deadline)
        except socket.timeout:
            raise TimeoutError("modbus_deadline") from None

    def _exchange(self, pdu, deadline=None):
        """为连接、发送、响应头和所有响应片段设置共同截止时间。
        核对事务号、协议号、Unit ID、长度、功能码和异常码，防止串包误当成功。"""
        conf = self.connection
        # One deadline covers connect, send and ALL receive fragments.
        end = time.monotonic() + conf["timeout_s"]
        if deadline is not None:
            end = min(end, time.monotonic() + deadline - time.time())
        def remaining():
            value = end - time.monotonic()
            if value <= 0:
                raise TimeoutError("modbus_deadline")
            return value
        self.transaction = (self.transaction + 1) % 65536
        header = struct.pack(">HHHB", self.transaction, 0, len(pdu) + 1, conf["unit_id"])
        with socket.create_connection((conf["host"], conf["port"]), remaining()) as sock:
            sock.settimeout(remaining())
            sock.sendall(header + pdu)
            def receive(count):
                data = b""
                while len(data) < count:
                    sock.settimeout(remaining())
                    chunk = sock.recv(count - len(data))
                    if not chunk:
                        raise ConnectionError("modbus_connection_closed")
                    data += chunk
                return data
            tx, protocol, length, unit = struct.unpack(">HHHB", receive(7))
            if (tx != self.transaction or protocol != 0 or unit != conf["unit_id"]
                    or not 2 <= length <= 254):
                raise ValueError("invalid_modbus_header")
            reply = receive(length - 1)
            if reply[0] == pdu[0] | 0x80:
                raise OSError("modbus_exception_%s" % (reply[1] if len(reply) == 2 else "malformed"))
            if reply[0] != pdu[0]:
                raise ValueError("modbus_function_mismatch")
            return reply

    def read(self, point, deadline=None):
        """读取一个已映射标量或单个位。deadline 可限制整次设备扫描的总时间。"""
        count = width(point)
        reply = self.exchange(struct.pack(">BHH", point["read_function"], point["address"], count), deadline)
        if point["read_function"] in (1, 2):
            if len(reply) != 3 or reply[1] != 1:
                raise ValueError("modbus_bit_count_mismatch")
            return int(bool(reply[2] & 1))  # 单点读取：第一个离散输入/线圈位在低位。
        if len(reply) != 2 + count * 2 or reply[1] != count * 2:
            raise ValueError("modbus_byte_count_mismatch")
        return decode(reply[2:], point)

    def write(self, point, value, deadline=None):
        """用 FC06/16 写寄存器并核对响应回显。
        使用独立 write_address（若配置），不因响应丢失自动重发。"""
        data = encode(value, point)
        address = point.get("write_address", point["address"])
        if point["write_function"] == 6:
            pdu = struct.pack(">BH", 6, address) + data
            expected = pdu
        else:
            pdu = struct.pack(">BHHB", 16, address, len(data) // 2, len(data)) + data
            expected = pdu[:5]
        if self.exchange(pdu, deadline) != expected:
            raise ValueError("modbus_write_ack_mismatch")


class ModbusCDUAdapter:
    """把具体点表封装成标准设备接口。硬件来源表示真实网络采集，不保证对端为真实 CDU。"""
    def __init__(self, asset_id, profile, owner):
        validate_profile(profile)
        self.asset_id, self.profile, self.owner = asset_id, profile, owner
        self.client = ModbusTCPClient(profile["connection"])
        self.sequence = 0
        self.last_counter = None
        self.counter_changed_at = None

    def describe(self):
        """仅返回设备声明的可写能力；与模拟器共用元数据构造，不复用模拟测量值。"""
        return describe_profile(self.asset_id, self.profile, "hardware")

    def read(self, now):
        # Acquisition timestamps are NOT PLC sample timestamps. Max age also bounds scan skew.
        """采集观测、目标回读和模式/归属/告警，返回标准快照。
        缺失的控制状态显示 unknown，缺失告警显示 unavailable。可选样本计数器停更会标记 stale_source。
        时间戳是采集开始时间；无计数器时无法仅凭成功收包识别 PLC 数据冻结。"""
        started = time.time()
        scan_deadline = started + self.profile["max_data_age_s"]
        points = self.profile["points"]
        def readings(group):
            result = {}
            for name, point in points[group].items():
                at = time.time()
                result[name] = Reading(self.client.read(point, scan_deadline), point["unit"], at,
                                       provenance="modbus_tcp_acquisition")
            return result
        observations, setpoints = readings("observations"), readings("setpoints")
        status = {k: self.client.read(p, scan_deadline) for k, p in points["status"].items()}
        # 可选设备采样计数器用于发现“通信正常但 PLC 数据不再刷新”。
        # 计数器必须与传感器采样更新关联；普通运行时长/网络请求计数不够。
        if "sample_counter" in status:
            monotonic_now = time.monotonic()
            if status["sample_counter"] != self.last_counter or self.counter_changed_at is None:
                self.last_counter, self.counter_changed_at = status["sample_counter"], monotonic_now
            elif monotonic_now - self.counter_changed_at > self.profile["max_data_age_s"]:
                observations = {k: replace(v, quality="stale_source") for k, v in observations.items()}
        def enum(name):
            if name not in status:
                return "unknown"
            value = status[name]
            return points["status"][name]["enum"].get(str(int(value)), "unknown") if value == int(value) else "unknown"
        alarms = (("device_alarm_unavailable",) if "alarm" not in status else
                  ("device_alarm:%s" % status["alarm"],) if status["alarm"] else ())
        return Snapshot(self.asset_id, started, enum("mode"), enum("owner"), alarms,
                        observations, setpoints)

    def commissioned(self):
        """检查现场操作者声明的验收项、写开关和所需接口是否完整。
        这些布尔字段不能自动证明现场安全；真实验收证据和设备回退逻辑必须存在。"""
        c = self.profile.get("commissioning", {})
        return (self.profile.get("write_enabled") is True
                and bool(self.profile["controls"])
                and {"mode", "owner", "alarm"}.issubset(self.profile["points"]["status"])
                and c.get("point_map_verified") is True
                and c.get("limits_verified") is True
                and c.get("local_fallback_tested") is True
                and c.get("watchdog_tested") is True
                and c.get("exclusive_control_tested") is True
                and bool(c.get("evidence_reference"))
                and {"heartbeat", "release"}.issubset(self.profile["points"].get("commands", {})))

    def write(self, request):
        """网关通过后执行底层写入；再次检查验收开关和命令截止时间。禁止绕过 ControlService 直接作为产品控制 API。"""
        if not self.commissioned():
            raise PermissionError("device_not_commissioned")
        self.client.write(self.profile["points"]["setpoints"][request.quantity],
                          request.value, request.expires_at)

    def tolerance(self, quantity):
        """返回该点的设定值回读误差容限，通常对应寄存器量化分辨率。"""
        return self.profile["points"]["setpoints"][quantity].get("tolerance", 1e-8)

    def normalized_value(self, quantity, value):
        """预先执行一次编解码，得到实际能写入设备的量化值，便于网关检查。"""
        p = self.profile["points"]["setpoints"][quantity]
        return decode(encode(value, p), p)

    def feed_watchdog(self):
        """向设备递增心跳序号，表明控制程序仍在正常检查工况。
        设备必须自行实现超时本地接管；本函数不会替设备建立看门狗。"""
        if not self.commissioned():
            raise PermissionError("device_not_commissioned")
        self.sequence = (self.sequence + 1) % 65536
        self.client.write(self.profile["points"]["commands"]["heartbeat"], self.sequence)

    def release_to_local(self, owner):
        """仅当控制权仍属本程序时请求本地接管，并读取模式和归属确认。
        他方已经接管时不抢回控制；这里不是紧急停机/停泵操作。"""
        if not self.commissioned():
            return False
        snapshot = self.read(time.time())
        if snapshot.owner != owner:
            return snapshot.operating_mode == "local_auto" and snapshot.owner == "local"
        p = self.profile["points"]["commands"]["release"]
        self.client.write(p, p["value"])
        after = self.read(time.time())
        return after.operating_mode == "local_auto" and after.owner == "local"
