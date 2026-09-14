"""单机运行数据仓库。

SQLite events 表保存 session/telemetry/decision/calibration/model/fault 等事件。
WAL 便于读写共存，FULL 同步提高落盘可靠性。每次 put 独立提交。
它不代替命令预写审计，也没有实现长期留存/压缩/磁盘容量管理或云同步。"""
import json
import sqlite3
from pathlib import Path


class Store:
    """单线程 SQLite 存储句柄。事件格式采用 JSON，所有写入用参数绑定，禁止 NaN/Inf。"""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS event_kind_time ON events(kind, at)")
        self.db.commit()

    def put(self, at, kind, payload):
        """提交一条有时间、类型和 JSON 载荷的事件；返回时事务已提交。"""
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.db:
            self.db.execute("INSERT INTO events(at,kind,payload) VALUES(?,?,?)", (at, kind, data))

    def latest(self, kind):
        """按写入序号读取某类最新事件，用于获取会话或模型；无记录返回 None。"""
        row = self.db.execute("SELECT payload FROM events WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, kind):
        """按插入顺序读取某类全部事件；当前用于小规模离线报告，长周期应改为分页。"""
        return [{"at": r[0], **json.loads(r[1])} for r in self.db.execute(
            "SELECT at,payload FROM events WHERE kind=? ORDER BY id", (kind,))]

    def close(self):
        """关闭数据库连接。"""
        self.db.close()
