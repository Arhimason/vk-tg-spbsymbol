import sqlite3
from pathlib import Path

from vk_tg_bridge.constants import CURSOR_KEY
from vk_tg_bridge.models import MessageLink, PeerLink, VkCredentials


class Store:
    def __init__(self, path: str) -> None:
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.path = database
        self._conn = sqlite3.connect(database)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def read_credentials(self) -> VkCredentials | None:
        row = self._conn.execute(
            """
            SELECT access_token, refresh_token, exchange_token, device_id, client_id, expires_at
            FROM vk_token WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return None
        return VkCredentials(
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            exchange_token=row["exchange_token"],
            device_id=row["device_id"],
            client_id=row["client_id"],
            expires_at=row["expires_at"],
        )

    def save_credentials(self, credentials: VkCredentials) -> None:
        self._conn.execute(
            """
            INSERT INTO vk_token (
                id, access_token, refresh_token, exchange_token,
                device_id, client_id, expires_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                exchange_token = excluded.exchange_token,
                device_id = excluded.device_id,
                client_id = excluded.client_id,
                expires_at = excluded.expires_at
            """,
            (
                credentials.access_token,
                credentials.refresh_token,
                credentials.exchange_token,
                credentials.device_id,
                credentials.client_id,
                credentials.expires_at,
            ),
        )
        self._conn.commit()

    def peer_by_vk(self, peer_id: int) -> PeerLink | None:
        row = self._conn.execute(
            """
            SELECT vk_peer_id, title, telegram_chat_id, bot_invited
            FROM peers WHERE vk_peer_id = ?
            """,
            (peer_id,),
        ).fetchone()
        return _peer(row)

    def peers(self) -> list[PeerLink]:
        rows = self._conn.execute(
            """
            SELECT vk_peer_id, title, telegram_chat_id, bot_invited
            FROM peers ORDER BY vk_peer_id
            """
        ).fetchall()
        return [link for row in rows if (link := _peer(row)) is not None]

    def peer_by_telegram(self, chat_id: int) -> PeerLink | None:
        row = self._conn.execute(
            """
            SELECT vk_peer_id, title, telegram_chat_id, bot_invited
            FROM peers WHERE telegram_chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        return _peer(row)

    def save_peer(
        self,
        *,
        vk_peer_id: int,
        title: str,
        telegram_chat_id: int,
        bot_invited: bool,
    ) -> PeerLink:
        self._conn.execute(
            """
            INSERT INTO peers (vk_peer_id, title, telegram_chat_id, bot_invited)
            VALUES (?, ?, ?, ?)
            """,
            (vk_peer_id, title, telegram_chat_id, int(bot_invited)),
        )
        self._conn.commit()
        return PeerLink(
            vk_peer_id=vk_peer_id,
            title=title,
            telegram_chat_id=telegram_chat_id,
            bot_invited=bot_invited,
        )

    def rename_peer(self, peer_id: int, title: str) -> None:
        self._conn.execute(
            "UPDATE peers SET title = ? WHERE vk_peer_id = ?",
            (title, peer_id),
        )
        self._conn.commit()

    def mark_bot_invited(self, peer_id: int) -> None:
        self._conn.execute(
            "UPDATE peers SET bot_invited = 1 WHERE vk_peer_id = ?",
            (peer_id,),
        )
        self._conn.commit()

    def is_seen(self, message_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_messages WHERE vk_message_id = ?",
            (message_id,),
        ).fetchone()
        return row is not None

    def save_message_link(
        self,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        vk_peer_id: int,
        vk_message_id: int,
        vk_conversation_message_id: int,
        outgoing: bool = False,
        body: str = "",
    ) -> MessageLink:
        self._conn.execute(
            """
            INSERT INTO message_links (
                telegram_chat_id, telegram_message_id, vk_peer_id,
                vk_message_id, vk_conversation_message_id, outgoing, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_chat_id, telegram_message_id) DO UPDATE SET
                vk_peer_id = excluded.vk_peer_id,
                vk_message_id = excluded.vk_message_id,
                vk_conversation_message_id = excluded.vk_conversation_message_id,
                outgoing = excluded.outgoing,
                body = CASE
                    WHEN excluded.body != '' THEN excluded.body
                    ELSE message_links.body
                END
            """,
            (
                telegram_chat_id,
                telegram_message_id,
                vk_peer_id,
                vk_message_id,
                vk_conversation_message_id,
                int(outgoing),
                body,
            ),
        )
        self._conn.commit()
        return MessageLink(
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            vk_peer_id=vk_peer_id,
            vk_message_id=vk_message_id,
            vk_conversation_message_id=vk_conversation_message_id,
            outgoing=outgoing,
            body=body,
        )

    def link_by_telegram(self, *, chat_id: int, message_id: int) -> MessageLink | None:
        row = self._conn.execute(
            """
            SELECT telegram_chat_id, telegram_message_id, vk_peer_id,
                   vk_message_id, vk_conversation_message_id, outgoing, body
            FROM message_links
            WHERE telegram_chat_id = ? AND telegram_message_id = ?
            """,
            (chat_id, message_id),
        ).fetchone()
        return _message_link(row)

    def link_by_vk(self, *, peer_id: int, conversation_message_id: int) -> MessageLink | None:
        row = self._conn.execute(
            """
            SELECT telegram_chat_id, telegram_message_id, vk_peer_id,
                   vk_message_id, vk_conversation_message_id, outgoing, body
            FROM message_links
            WHERE vk_peer_id = ? AND vk_conversation_message_id = ?
            ORDER BY telegram_message_id
            LIMIT 1
            """,
            (peer_id, conversation_message_id),
        ).fetchone()
        return _message_link(row)

    def outgoing_unread(self, *, peer_id: int, up_to: int) -> list[MessageLink]:
        rows = self._conn.execute(
            """
            SELECT telegram_chat_id, telegram_message_id, vk_peer_id,
                   vk_message_id, vk_conversation_message_id, outgoing, body
            FROM message_links
            WHERE vk_peer_id = ? AND outgoing = 1 AND read_marked = 0
              AND vk_conversation_message_id <= ?
            ORDER BY vk_conversation_message_id, telegram_message_id
            """,
            (peer_id, up_to),
        ).fetchall()
        return [link for row in rows if (link := _message_link(row)) is not None]

    def links_by_vk_message(self, *, peer_id: int, message_id: int) -> list[MessageLink]:
        rows = self._conn.execute(
            """
            SELECT telegram_chat_id, telegram_message_id, vk_peer_id,
                   vk_message_id, vk_conversation_message_id, outgoing, body
            FROM message_links
            WHERE vk_peer_id = ? AND vk_message_id = ?
            ORDER BY telegram_message_id
            """,
            (peer_id, message_id),
        ).fetchall()
        return [link for row in rows if (link := _message_link(row)) is not None]

    def update_body(self, *, chat_id: int, message_id: int, body: str) -> None:
        self._conn.execute(
            """
            UPDATE message_links SET body = ?
            WHERE telegram_chat_id = ? AND telegram_message_id = ?
            """,
            (body, chat_id, message_id),
        )
        self._conn.commit()

    def mark_link_read(self, *, chat_id: int, message_id: int) -> None:
        self._conn.execute(
            """
            UPDATE message_links SET read_marked = 1
            WHERE telegram_chat_id = ? AND telegram_message_id = ?
            """,
            (chat_id, message_id),
        )
        self._conn.commit()

    def mark_seen(self, message_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_messages (vk_message_id) VALUES (?)",
            (message_id,),
        )
        self._conn.commit()

    def read_runtime(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM runtime WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def read_runtime_int(self, key: str) -> int | None:
        value = self.read_runtime(key)
        if value is None:
            return None
        return int(value)

    def save_runtime(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO runtime (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._conn.commit()

    def read_cursor(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM runtime WHERE key = ?",
            (CURSOR_KEY,),
        ).fetchone()
        if row is None:
            return None
        return int(row["value"])

    def save_cursor(self, ts: int) -> None:
        self._conn.execute(
            """
            INSERT INTO runtime (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (CURSOR_KEY, str(ts)),
        )
        self._conn.commit()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS peers (
                vk_peer_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                telegram_chat_id INTEGER NOT NULL UNIQUE,
                bot_invited INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_messages (
                vk_message_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS message_links (
                telegram_chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                vk_peer_id INTEGER NOT NULL,
                vk_message_id INTEGER NOT NULL,
                vk_conversation_message_id INTEGER NOT NULL,
                outgoing INTEGER NOT NULL DEFAULT 0,
                read_marked INTEGER NOT NULL DEFAULT 0,
                body TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (telegram_chat_id, telegram_message_id)
            );
            CREATE INDEX IF NOT EXISTS message_links_vk
                ON message_links (vk_peer_id, vk_conversation_message_id);
            CREATE TABLE IF NOT EXISTS runtime (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vk_token (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL DEFAULT '',
                expires_at REAL
            );
            """
        )
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(vk_token)")
        }
        if "exchange_token" not in columns:
            self._conn.execute(
                "ALTER TABLE vk_token ADD COLUMN exchange_token TEXT NOT NULL DEFAULT ''"
            )
        link_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(message_links)")
        }
        if "outgoing" not in link_columns:
            self._conn.execute(
                "ALTER TABLE message_links ADD COLUMN outgoing INTEGER NOT NULL DEFAULT 0"
            )
        if "read_marked" not in link_columns:
            self._conn.execute(
                "ALTER TABLE message_links ADD COLUMN read_marked INTEGER NOT NULL DEFAULT 0"
            )
        if "body" not in link_columns:
            self._conn.execute(
                "ALTER TABLE message_links ADD COLUMN body TEXT NOT NULL DEFAULT ''"
            )
        self._conn.commit()


def _message_link(row: sqlite3.Row | None) -> MessageLink | None:
    if row is None:
        return None
    return MessageLink(
        telegram_chat_id=row["telegram_chat_id"],
        telegram_message_id=row["telegram_message_id"],
        vk_peer_id=row["vk_peer_id"],
        vk_message_id=row["vk_message_id"],
        vk_conversation_message_id=row["vk_conversation_message_id"],
        outgoing=bool(row["outgoing"]) if "outgoing" in row.keys() else False,
        body=row["body"] if "body" in row.keys() else "",
    )


def _peer(row: sqlite3.Row | None) -> PeerLink | None:
    if row is None:
        return None
    return PeerLink(
        vk_peer_id=row["vk_peer_id"],
        title=row["title"],
        telegram_chat_id=row["telegram_chat_id"],
        bot_invited=bool(row["bot_invited"]),
    )
