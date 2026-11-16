# -*- coding: utf-8 -*-
"""Companion Lite · 存储层 v2：多聊天 + 删除/编辑 + FTS 同步"""
import os
import re
import sqlite3
import threading
from datetime import datetime

TS_FMT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  speaker TEXT NOT NULL,
  text TEXT NOT NULL,
  chat_id TEXT DEFAULT 'main',
  UNIQUE(ts, speaker, text)
);
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY,
  title TEXT,
  created TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
  USING fts5(text, content='messages', content_rowid='id', tokenize=trigram);
CREATE TABLE IF NOT EXISTS gram_df (gram TEXT PRIMARY KEY, df INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS diary (day TEXT PRIMARY KEY, text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS timeline (id INTEGER PRIMARY KEY, ts TEXT, note TEXT);
"""


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "memory.db")
        self.con = sqlite3.connect(self.db_path, check_same_thread=False)
        self.lock = threading.RLock()
        try:
            self.con.execute("PRAGMA temp_store=MEMORY")
        except Exception:
            pass
        self.con.executescript(SCHEMA)
        self.con.commit()
        self._migrate()

    def _migrate(self):
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(messages)")]
        if "chat_id" not in cols:
            self.con.execute("ALTER TABLE messages ADD COLUMN chat_id TEXT DEFAULT 'main'")
            self.con.commit()
        self.con.executescript("""
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
          INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
        END;
        """)
        self.con.commit()
        if not self.current_chat():
            self.con.execute(
                "INSERT OR IGNORE INTO chats(id,title,created) VALUES('main','主聊天',?)",
                (datetime.now().strftime(TS_FMT),))
            self.con.commit()
            self.set_chat("main")

    # ---------- 聊天 ----------
    def current_chat(self):
        r = self.con.execute("SELECT v FROM state WHERE k='current_chat'").fetchone()
        return r[0] if r else None

    def set_chat(self, cid):
        self.con.execute("INSERT OR REPLACE INTO state(k,v) VALUES('current_chat',?)", (cid,))
        self.con.commit()

    def list_chats(self):
        rows = self.con.execute(
            "SELECT c.id, c.title, COUNT(m.id) FROM chats c "
            "LEFT JOIN messages m ON m.chat_id=c.id GROUP BY c.id ORDER BY c.created"
        ).fetchall()
        return [{"id": r[0], "title": r[1] or r[0], "count": r[2]} for r in rows]

    def new_chat(self, title=None):
        cid = datetime.now().strftime("c%Y%m%d%H%M%S")
        self.con.execute("INSERT INTO chats(id,title,created) VALUES(?,?,?)",
                         (cid, title or f"聊天 {cid[-6:]}", datetime.now().strftime(TS_FMT)))
        self.con.commit()
        self.set_chat(cid)
        return cid

    # ---------- 消息 ----------
    def append(self, speaker, text, ts=None, chat_id=None):
        ts = ts or datetime.now().strftime(TS_FMT)
        cid = chat_id or self.current_chat() or "main"
        cur = self.con.execute(
            "INSERT OR IGNORE INTO messages(ts,speaker,text,chat_id) VALUES(?,?,?,?)",
            (ts, speaker, text.strip(), cid))
        self.con.commit()
        return (cur.lastrowid or 0), cid

    def recent(self, n=40, chat_id=None, before_id=None):
        """最近的 n 条；给了 before_id 就取比它更早的（往前翻页用）"""
        cid = chat_id or self.current_chat() or "main"
        if before_id:
            rows = self.con.execute(
                "SELECT ts,speaker,text,id FROM messages WHERE chat_id=? AND id<? "
                "ORDER BY id DESC LIMIT ?", (cid, int(before_id), n)).fetchall()
        else:
            rows = self.con.execute(
                "SELECT ts,speaker,text,id FROM messages WHERE chat_id=? "
                "ORDER BY ts DESC, id DESC LIMIT ?", (cid, n)).fetchall()
        return list(reversed(rows))

    def total_in_chat(self, chat_id=None):
        cid = chat_id or self.current_chat() or "main"
        return self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (cid,)).fetchone()[0]

    def count(self, chat_id=None):
        cid = chat_id or self.current_chat() or "main"
        return self.con.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (cid,)).fetchone()[0]

    def count_all(self):
        return self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def delete_msg(self, mid):
        self.con.execute("DELETE FROM messages WHERE id=?", (mid,))
        self.con.commit()

    def update_msg(self, mid, text):
        self.con.execute("UPDATE messages SET text=? WHERE id=?", (text, mid))
        self.con.commit()

    def tail(self, chat_id=None):
        cid = chat_id or self.current_chat() or "main"
        return self.con.execute(
            "SELECT id, speaker, text FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT 2",
            (cid,)).fetchall()

    def rebuild_grams(self, max_df=200):
        counts = {}
        for (text,) in self.con.execute("SELECT text FROM messages"):
            seen = set()
            for n in (2, 3):
                for i in range(len(text) - n + 1):
                    w = text[i:i + n]
                    if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9+]+", w):
                        seen.add(w)
            for w in seen:
                counts[w] = counts.get(w, 0) + 1
        self.con.execute("DELETE FROM gram_df")
        self.con.executemany("INSERT OR REPLACE INTO gram_df(gram,df) VALUES(?,?)",
                             [(w, c) for w, c in counts.items() if c <= max_df])
        self.con.commit()
        return len(counts)

    def import_from_st(self, st_db, limit=None):
        src = sqlite3.connect(f"file:{st_db}?mode=ro", uri=True)
        q = "SELECT ts,speaker,text FROM messages ORDER BY ts, id"
        if limit:
            q = (f"SELECT * FROM (SELECT ts,speaker,text FROM messages "
                 f"ORDER BY ts DESC, id DESC LIMIT {int(limit)}) ORDER BY ts, id")
        rows = src.execute(q).fetchall()
        src.close()
        cid = self.current_chat() or "main"
        before = self.count_all()
        self.con.executemany(
            "INSERT OR IGNORE INTO messages(ts,speaker,text,chat_id) VALUES(?,?,?,?)",
            [(t, s, x, cid) for t, s, x in rows])
        self.con.commit()
        added = self.count_all() - before
        self.rebuild_grams()
        return added, self.count_all()

    def close(self):
        try:
            self.con.commit()
        except Exception:
            pass
        try:
            self.con.close()
        except Exception:
            pass

    # ---------- 状态 / 日记 ----------
    def set_state(self, k, v):
        self.con.execute("INSERT OR REPLACE INTO state(k,v) VALUES(?,?)", (k, str(v)))
        self.con.commit()

    def get_state(self, k, default=None):
        r = self.con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def add_diary(self, day, text):
        self.con.execute("INSERT OR REPLACE INTO diary(day,text,created) VALUES(?,?,?)",
                         (day, text, datetime.now().strftime(TS_FMT)))
        self.con.commit()

    def recent_diary(self, n=2):
        return self.con.execute(
            "SELECT day,text FROM diary ORDER BY day DESC LIMIT ?", (n,)).fetchall()
