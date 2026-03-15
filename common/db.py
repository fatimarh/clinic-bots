
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Optional

from .logging_config import setup_logger

# --- Paths & logger ---
ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "clinic.db"

log = setup_logger("db", ROOT / "logs" / "db.log")

# --- Low-level helpers ---

def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply SQLite pragmas for WAL, FK and performance/consistency trade-offs."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")


def get_conn(timeout_sec: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def _table_info(conn: sqlite3.Connection, table: str):
    cur = conn.execute(f"PRAGMA table_info({table})")
    # columns: cid, name, type, notnull, dflt_value, pk
    return [tuple(r) for r in cur.fetchall()]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(name == column for _, name, *_ in _table_info(conn, table))


def _col_notnull(conn: sqlite3.Connection, table: str, column: str) -> bool:
    for _, name, _type, notnull, *_ in _table_info(conn, table):
        if name == column:
            return bool(notnull)
    return False


# --- Migrations ---

def migrate() -> None:
    """Create/upgrade schema to the latest version (idempotent)."""
    sql = """
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS doctors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_user_id INTEGER UNIQUE,
        full_name  TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- initially (legacy) patients had NOT NULL birth_year; we will relax it below if needed
    CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name  TEXT NOT NULL,
        birth_year INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
        patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        settled    INTEGER DEFAULT 0,
        settled_at TIMESTAMP NULL
    );

    CREATE INDEX IF NOT EXISTS idx_referrals_doctor_created ON referrals(doctor_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_referrals_settled        ON referrals(settled, settled_at);

    CREATE TABLE IF NOT EXISTS admin_auth (
        chat_id       INTEGER PRIMARY KEY,
        authorized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    with get_conn() as conn:
        conn.executescript(sql)

        # Ensure patients has birth_date and birth_year is nullable
        # 1) Add birth_date if missing
        if not _column_exists(conn, "patients", "birth_date"):
            conn.execute("ALTER TABLE patients ADD COLUMN birth_date TEXT")  # ISO YYYY-MM-DD (nullable)

        # 2) If birth_year is NOT NULL, recreate table with birth_year NULLABLE
        try:
            by_notnull = _col_notnull(conn, "patients", "birth_year")
        except Exception:
            by_notnull = False

        if by_notnull:
            log.info("Rebuilding patients to relax birth_year NOT NULL -> NULL and keep birth_date")
            conn.execute("BEGIN IMMEDIATE")
            # create new table with desired schema
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS patients_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name  TEXT NOT NULL,
                    birth_year INTEGER,
                    birth_date TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            # copy old data (birth_date unknown -> NULL)
            conn.execute(
                """
                INSERT INTO patients_new(id, full_name, birth_year, birth_date, created_at, updated_at)
                SELECT id, full_name, birth_year, NULL, created_at, updated_at FROM patients
                """
            )
            conn.execute("DROP TABLE patients")
            conn.execute("ALTER TABLE patients_new RENAME TO patients")
            conn.commit()

        # 3) Helpful index for lookups by (full_name, birth_date)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_patients_fullname_birthdate ON patients(full_name, birth_date)"
        )

    log.info("Migrations applied (SQLite WAL mode, birth_date supported)")


# --- Admin auth persistence ---

def admin_is_authorized(chat_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM admin_auth WHERE chat_id = ? LIMIT 1",
            (chat_id,),
        ).fetchone()
        return row is not None


def admin_authorize(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO admin_auth(chat_id) VALUES (?)",
            (chat_id,),
        )


def admin_unauthorize(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM admin_auth WHERE chat_id = ?",
            (chat_id,),
        )


# --- Doctors CRUD ---

def get_doctor_by_tg(tg_user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, tg_user_id, full_name, created_at, updated_at FROM doctors WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()
        return dict(row) if row else None


def upsert_doctor(tg_user_id: int, full_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO doctors(tg_user_id, full_name)
            VALUES (?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                full_name = excluded.full_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (tg_user_id, full_name),
        )


def update_doctor_name(tg_user_id: int, full_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE doctors
               SET full_name = ?, updated_at = CURRENT_TIMESTAMP
             WHERE tg_user_id = ?
            """,
            (full_name, tg_user_id),
        )


def get_doctor_id_by_tg(tg_user_id: int) -> Optional[int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM doctors WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()
        return int(row["id"]) if row else None


# --- Patients & Referrals ---

def get_or_create_patient(full_name: str, birth_date_iso: str) -> int:
    """Find patient by (full_name, birth_date). Create if not exists. Return patient id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM patients WHERE full_name = ? AND birth_date = ? LIMIT 1",
            (full_name, birth_date_iso),
        ).fetchone()
        if row:
            return int(row["id"])
        # also fill legacy birth_year for compatibility (year from ISO date)
        try:
            year_val = int(birth_date_iso[:4])
        except Exception:
            year_val = None
        cur = conn.execute(
            "INSERT INTO patients(full_name, birth_date, birth_year) VALUES(?, ?, ?)",
            (full_name, birth_date_iso, year_val),
        )
        return int(cur.lastrowid)


def add_referral(doctor_id: int, patient_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO referrals(doctor_id, patient_id) VALUES(?, ?)",
            (doctor_id, patient_id),
        )
        return int(cur.lastrowid)


def list_referrals_by_doctor(
    doctor_id: int,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> list[dict]:
    base_sql = """
    SELECT r.id           AS referral_id,
           p.full_name    AS patient_full_name,
           p.birth_date   AS patient_birth_date,
           r.created_at   AS created_at,
           r.settled      AS settled,
           r.settled_at   AS settled_at
      FROM referrals r
      JOIN patients p ON p.id = r.patient_id
     WHERE r.doctor_id = ?
    """
    args: list[object] = [doctor_id]

    if year is not None and month is not None:
        base_sql += " AND strftime('%Y', r.created_at) = ? AND strftime('%m', r.created_at) = ?"
        args += [f"{year:04d}", f"{month:02d}"]
    elif year is not None:
        base_sql += " AND strftime('%Y', r.created_at) = ?"
        args += [f"{year:04d}"]

    base_sql += " ORDER BY r.created_at ASC, r.id ASC"

    with get_conn() as conn:
        rows = conn.execute(base_sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]


def list_years_with_referrals(doctor_id: int) -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT strftime('%Y', created_at) AS y FROM referrals WHERE doctor_id = ? ORDER BY y ASC",
            (doctor_id,),
        ).fetchall()
        return [int(r["y"]) for r in rows if r["y"] is not None]


def list_months_for_year(doctor_id: int, year: int) -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT strftime('%m', created_at) AS m
              FROM referrals
             WHERE doctor_id = ? AND strftime('%Y', created_at) = ?
             ORDER BY m ASC
            """,
            (doctor_id, f"{year:04d}"),
        ).fetchall()
        return [int(r["m"]) for r in rows if r["m"] is not None]

def delete_referrals(doctor_id: int, referral_ids: list[int]) -> int:
    """
    Удаляет направления конкретного врача по списку id.
    Возвращает количество удалённых строк.
    """
    if not referral_ids:
        return 0
    placeholders = ",".join(["?"] * len(referral_ids))
    sql = f"DELETE FROM referrals WHERE doctor_id = ? AND id IN ({placeholders})"
    with get_conn() as conn:
        cur = conn.execute(sql, (doctor_id, *referral_ids))
        return cur.rowcount

        # ===== ADMIN QUERIES =====

def list_doctors_with_counts() -> list[dict]:
    """
    Возвращает список врачей с количеством направлений.
    Порядок — по дате создания (стабильный).
    """
    sql = """
    SELECT d.id            AS doctor_id,
           d.full_name     AS full_name,
           d.tg_user_id    AS tg_user_id,
           COUNT(r.id)     AS referrals_count
      FROM doctors d
 LEFT JOIN referrals r ON r.doctor_id = d.id
  GROUP BY d.id
  ORDER BY d.created_at ASC, d.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def search_doctors_prefix(q: str) -> list[dict]:
    """
    Поиск по префиксу любой части ФИО (начало строки или после пробела).
    Пример: 'Ив' найдёт 'Иванов Пётр' и 'Пётр Иванов' (по второй части).
    """
    q = (q or "").strip()
    if not q:
        return []
    like1 = f"{q}%"      # начало строки
    like2 = f"% {q}%"    # после пробела
    sql = """
    SELECT d.id         AS doctor_id,
           d.full_name  AS full_name,
           d.tg_user_id AS tg_user_id,
           COUNT(r.id)  AS referrals_count
      FROM doctors d
 LEFT JOIN referrals r ON r.doctor_id = d.id
     WHERE d.full_name LIKE ? ESCAPE '\\'
        OR d.full_name LIKE ? ESCAPE '\\'
  GROUP BY d.id
  ORDER BY d.full_name COLLATE NOCASE ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (like1, like2)).fetchall()
        return [dict(r) for r in rows]


def delete_doctor(doctor_id: int) -> int:
    """
    Удаляет врача (каскадно удалятся его referrals за счёт FK ON DELETE CASCADE).
    Возвращает количество удалённых строк в таблице doctors (0/1).
    """
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM doctors WHERE id = ?", (doctor_id,))
        return cur.rowcount


def list_unsettled_referrals_all() -> list[dict]:
    """
    Все НЕ рассчитанные направления со всеми деталями (для админа).
    """
    sql = """
    SELECT r.id           AS referral_id,
           r.created_at   AS created_at,
           p.full_name    AS patient_full_name,
           p.birth_date   AS patient_birth_date,
           d.id           AS doctor_id,
           d.full_name    AS doctor_full_name,
           d.tg_user_id   AS doctor_tg_user_id
      FROM referrals r
      JOIN patients  p ON p.id = r.patient_id
      JOIN doctors   d ON d.id = r.doctor_id
     WHERE r.settled = 0
  ORDER BY r.created_at ASC, r.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def list_unsettled_referrals_by_doctor(doctor_id: int) -> list[dict]:
    sql = """
    SELECT r.id           AS referral_id,
           r.created_at   AS created_at,
           p.full_name    AS patient_full_name,
           p.birth_date   AS patient_birth_date,
           d.id           AS doctor_id,
           d.full_name    AS doctor_full_name,
           d.tg_user_id   AS doctor_tg_user_id
      FROM referrals r
      JOIN patients  p ON p.id = r.patient_id
      JOIN doctors   d ON d.id = r.doctor_id
     WHERE r.settled = 0
       AND r.doctor_id = ?
  ORDER BY r.created_at ASC, r.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (doctor_id,)).fetchall()
        return [dict(r) for r in rows]


def settle_referrals(referral_ids: list[int]) -> int:
    """
    Отмечает направления как рассчитанные.
    Возвращает количество обновлённых записей.
    """
    if not referral_ids:
        return 0
    placeholders = ",".join(["?"] * len(referral_ids))
    sql = f"""
        UPDATE referrals
           SET settled = 1,
               settled_at = CURRENT_TIMESTAMP
         WHERE id IN ({placeholders})
           AND settled = 0
    """
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(referral_ids))
        return cur.rowcount


def get_referrals_details(referral_ids: list[int]) -> list[dict]:
    """
    Детали направлений по id — для уведомлений врачу.
    """
    if not referral_ids:
        return []
    placeholders = ",".join(["?"] * len(referral_ids))
    sql = f"""
    SELECT r.id           AS referral_id,
           r.created_at   AS created_at,
           p.full_name    AS patient_full_name,
           p.birth_date   AS patient_birth_date,
           d.id           AS doctor_id,
           d.full_name    AS doctor_full_name,
           d.tg_user_id   AS doctor_tg_user_id
      FROM referrals r
      JOIN patients  p ON p.id = r.patient_id
      JOIN doctors   d ON d.id = r.doctor_id
     WHERE r.id IN ({placeholders})
  ORDER BY r.created_at ASC, r.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(referral_ids)).fetchall()
        return [dict(r) for r in rows]


def list_settled_current_month() -> list[dict]:
    """
    Рассчитанные направления за текущий месяц (для админ-отчёта одним списком).
    """
    from datetime import date
    today = date.today()
    y, m = today.year, today.month
    sql = """
    SELECT r.id           AS referral_id,
           r.created_at   AS created_at,
           r.settled_at   AS settled_at,
           p.full_name    AS patient_full_name,
           p.birth_date   AS patient_birth_date,
           d.id           AS doctor_id,
           d.full_name    AS doctor_full_name
      FROM referrals r
      JOIN patients  p ON p.id = r.patient_id
      JOIN doctors   d ON d.id = r.doctor_id
     WHERE r.settled = 1
       AND strftime('%Y', r.settled_at) = ?
       AND strftime('%m', r.settled_at) = ?
  ORDER BY r.settled_at ASC, r.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (f"{y:04d}", f"{m:02d}")).fetchall()
        return [dict(r) for r in rows]

        # ===== EXPORT QUERIES =====

def export_doctors_overview() -> list[dict]:
    """
    Сводка по врачам: всего, рассчитано, не рассчитано.
    """
    sql = """
    SELECT d.id           AS doctor_id,
           d.full_name    AS doctor_full_name,
           d.tg_user_id   AS doctor_tg_user_id,
           COUNT(r.id)    AS total,
           SUM(CASE WHEN r.settled = 0 THEN 1 ELSE 0 END) AS unsettled,
           SUM(CASE WHEN r.settled = 1 THEN 1 ELSE 0 END) AS settled
      FROM doctors d
 LEFT JOIN referrals r ON r.doctor_id = d.id
  GROUP BY d.id
  ORDER BY d.full_name COLLATE NOCASE ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

def export_all_referrals() -> list[dict]:
    """
    Все направления с полями врача и пациента — для листа 'Все направления'.
    """
    sql = """
    SELECT r.id           AS referral_id,
           r.created_at   AS created_at,
           r.settled      AS settled,
           r.settled_at   AS settled_at,
           p.full_name    AS patient_full_name,
           p.birth_date   AS patient_birth_date,
           d.id           AS doctor_id,
           d.full_name    AS doctor_full_name
      FROM referrals r
      JOIN patients  p ON p.id = r.patient_id
      JOIN doctors   d ON d.id = r.doctor_id
  ORDER BY r.created_at ASC, r.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]