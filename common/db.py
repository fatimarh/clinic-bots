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
    sql = """
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS doctors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_user_id INTEGER UNIQUE,
        full_name  TEXT NOT NULL,
        full_name_lc TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name  TEXT NOT NULL,
        full_name_lc TEXT,
        birth_year INTEGER NOT NULL,
        birth_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doctor_id  INTEGER NOT NULL REFERENCES doctors(id)  ON DELETE CASCADE,
        patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        settled    INTEGER DEFAULT 0,
        settled_at TIMESTAMP NULL,
        visited    INTEGER DEFAULT 0,
        visited_at TIMESTAMP NULL
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

        # relax NOT NULL for patients.birth_year if present
        try:
            by_notnull = _col_notnull(conn, "patients", "birth_year")
        except Exception:
            by_notnull = False
        if by_notnull:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS patients_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name  TEXT NOT NULL,
                    full_name_lc TEXT,
                    birth_year INTEGER,
                    birth_date TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                """
                INSERT INTO patients_new(id, full_name, full_name_lc, birth_year, birth_date, created_at, updated_at)
                SELECT id, full_name, NULL, birth_year, birth_date, created_at, updated_at FROM patients
                """
            )
            conn.execute("DROP TABLE patients")
            conn.execute("ALTER TABLE patients_new RENAME TO patients")
            conn.commit()

        # ensure full_name_lc columns and indices
        if not _column_exists(conn, "patients", "full_name_lc"):
            conn.execute("ALTER TABLE patients ADD COLUMN full_name_lc TEXT")
            rows = conn.execute("SELECT id, full_name FROM patients").fetchall()
            for r in rows:
                conn.execute("UPDATE patients SET full_name_lc = ? WHERE id = ?", (str(r["full_name"]).lower(), r["id"]))
            conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_patients_fullname_lc ON patients(full_name_lc)")

        if not _column_exists(conn, "doctors", "full_name_lc"):
            conn.execute("ALTER TABLE doctors ADD COLUMN full_name_lc TEXT")
            rows = conn.execute("SELECT id, full_name FROM doctors").fetchall()
            for r in rows:
                conn.execute("UPDATE doctors SET full_name_lc = ? WHERE id = ?", (str(r["full_name"]).lower(), r["id"]))
            conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_doctors_fullname_lc ON doctors(full_name_lc)")

        # helper index
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_patients_fullname_birthdate ON patients(full_name, birth_date)"
        )

    log.info("Migrations applied")


# --- Admin auth ---

def admin_is_authorized(chat_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM admin_auth WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone()
        return row is not None


def admin_authorize(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO admin_auth(chat_id) VALUES (?)", (chat_id,))


def admin_unauthorize(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_auth WHERE chat_id = ?", (chat_id,))


# --- Doctors CRUD ---

def get_doctor_by_tg(tg_user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, tg_user_id, full_name, created_at, updated_at FROM doctors WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()
        return dict(row) if row else None


def upsert_doctor(tg_user_id: int, full_name: str) -> None:
    full_name_lc = (full_name or "").lower()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO doctors(tg_user_id, full_name, full_name_lc)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                full_name    = excluded.full_name,
                full_name_lc = excluded.full_name_lc,
                updated_at   = CURRENT_TIMESTAMP
            """,
            (tg_user_id, full_name, full_name_lc),
        )


def update_doctor_name(tg_user_id: int, full_name: str) -> None:
    full_name_lc = (full_name or "").lower()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE doctors
               SET full_name = ?, full_name_lc = ?, updated_at = CURRENT_TIMESTAMP
             WHERE tg_user_id = ?
            """,
            (full_name, full_name_lc, tg_user_id),
        )


def get_doctor_id_by_tg(tg_user_id: int) -> Optional[int]:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM doctors WHERE tg_user_id = ?", (tg_user_id,)).fetchone()
        return int(row["id"]) if row else None


def delete_doctor(doctor_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM doctors WHERE id = ?", (doctor_id,))
        return cur.rowcount


# --- Patients & Referrals ---

def get_or_create_patient(full_name: str, birth_date_iso: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM patients WHERE full_name = ? AND birth_date = ? LIMIT 1",
            (full_name, birth_date_iso),
        ).fetchone()
        if row:
            return int(row["id"])
        try:
            year_val = int(birth_date_iso[:4])
        except Exception:
            year_val = None
        cur = conn.execute(
            "INSERT INTO patients(full_name, full_name_lc, birth_date, birth_year) VALUES(?, ?, ?, ?)",
            (full_name, (full_name or "").lower(), birth_date_iso, year_val),
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
    doctor_id: int, year: Optional[int] = None, month: Optional[int] = None
) -> list[dict]:
    base_sql = """
    SELECT r.id           AS referral_id,
           p.full_name    AS patient_full_name,
           p.birth_date   AS patient_birth_date,
           r.created_at   AS created_at,
           r.settled      AS settled,
           r.settled_at   AS settled_at,
           r.visited      AS visited,
           r.visited_at   AS visited_at
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
    if not referral_ids:
        return 0
    placeholders = ",".join(["?"] * len(referral_ids))
    sql = f"DELETE FROM referrals WHERE doctor_id = ? AND id IN ({placeholders})"
    with get_conn() as conn:
        cur = conn.execute(sql, (doctor_id, *referral_ids))
        return cur.rowcount


# --- Search helpers (case-insensitive, from word start) ---

def _build_token_like_sql_from_word_start(column: str, tokens: list[str]) -> tuple[str, list[str]]:
    if not tokens:
        return "1=1", []
    parts = []
    args: list[str] = []
    for t in tokens:
        tl = t.lower()
        parts.append(f"({column} LIKE ? OR {column} LIKE ?)")
        args += [f"{tl}%", f"% {tl}%"]
    where = " AND ".join(parts)
    return where, args


# --- Aggregates / export basics ---

def list_doctors_with_counts() -> list[dict]:
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
    tokens = [t for t in (q or "").strip().split() if t]
    where, args = _build_token_like_sql_from_word_start("d.full_name_lc", tokens)
    sql = f"""
    SELECT d.id         AS doctor_id,
           d.full_name  AS full_name,
           d.tg_user_id AS tg_user_id,
           COUNT(r.id)  AS referrals_count
      FROM doctors d
 LEFT JOIN referrals r ON r.doctor_id = d.id
     WHERE {where}
  GROUP BY d.id
  ORDER BY d.full_name COLLATE NOCASE ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]


def export_doctors_overview() -> list[dict]:
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
    sql = """
    SELECT r.id           AS referral_id,
           r.created_at   AS created_at,
           r.settled      AS settled,
           r.settled_at   AS settled_at,
           r.visited      AS visited,
           r.visited_at   AS visited_at,
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


# --- TOPs ---

def top_doctors_by_referrals(year: int | None = None, month: int | None = None, limit: int = 50) -> list[dict]:
    base = """
    SELECT d.id AS doctor_id, d.full_name AS doctor_full_name, COUNT(r.id) AS cnt
      FROM doctors d
 LEFT JOIN referrals r ON r.doctor_id = d.id
    """
    where = ""
    args: list[str] = []
    if year is not None and month is not None:
        where = "WHERE r.id IS NOT NULL AND strftime('%Y', r.created_at)=? AND strftime('%m', r.created_at)=?"
        args = [f"{year:04d}", f"{month:02d}"]
    elif year is not None:
        where = "WHERE r.id IS NOT NULL AND strftime('%Y', r.created_at)=?"
        args = [f"{year:04d}"]
    group = " GROUP BY d.id ORDER BY cnt DESC, d.full_name COLLATE NOCASE ASC LIMIT ?"
    sql = base + (" " + where if where else "") + group
    with get_conn() as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
        return [dict(r) for r in rows]


def top_doctors_by_visits(year: int | None = None, month: int | None = None, limit: int = 50) -> list[dict]:
    base = """
    SELECT d.id AS doctor_id, d.full_name AS doctor_full_name,
           SUM(CASE WHEN r.visited = 1 THEN 1 ELSE 0 END) AS cnt
      FROM doctors d
 LEFT JOIN referrals r ON r.doctor_id = d.id
    """
    where = ""
    args: list[str] = []
    if year is not None and month is not None:
        where = "WHERE r.id IS NOT NULL AND strftime('%Y', r.created_at)=? AND strftime('%m', r.created_at)=?"
        args = [f"{year:04d}", f"{month:02d}"]
    elif year is not None:
        where = "WHERE r.id IS NOT NULL AND strftime('%Y', r.created_at)=?"
        args = [f"{year:04d}"]
    group = " GROUP BY d.id ORDER BY cnt DESC, d.full_name COLLATE NOCASE ASC LIMIT ?"
    sql = base + (" " + where if where else "") + group
    with get_conn() as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
        return [dict(r) for r in rows]


# --- Visits & Settlement ---

def list_all_patients() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, full_name, birth_date, created_at
              FROM patients
             ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_unvisited_all() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS referral_id, r.created_at, r.visited, r.visited_at,
                   p.full_name AS patient_full_name, p.birth_date AS patient_birth_date,
                   d.id AS doctor_id, d.full_name AS doctor_full_name, d.tg_user_id AS doctor_tg_user_id
              FROM referrals r
              JOIN patients  p ON p.id = r.patient_id
              JOIN doctors   d ON d.id = r.doctor_id
             WHERE r.visited = 0
             ORDER BY r.created_at ASC, r.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_unvisited_by_doctor(doctor_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS referral_id, r.created_at, r.visited, r.visited_at,
                   p.full_name AS patient_full_name, p.birth_date AS patient_birth_date,
                   d.id AS doctor_id, d.full_name AS doctor_full_name, d.tg_user_id AS doctor_tg_user_id
              FROM referrals r
              JOIN patients  p ON p.id = r.patient_id
              JOIN doctors   d ON d.id = r.doctor_id
             WHERE r.visited = 0
               AND r.doctor_id = ?
             ORDER BY r.created_at ASC, r.id ASC
            """,
            (doctor_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_referrals_visited(referral_ids: list[int]) -> int:
    if not referral_ids:
        return 0
    placeholders = ",".join(["?"] * len(referral_ids))
    sql = f"""
        UPDATE referrals
           SET visited = 1,
               visited_at = datetime(CURRENT_TIMESTAMP, '+3 hours')
         WHERE id IN ({placeholders})
           AND visited = 0
    """
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(referral_ids))
        return cur.rowcount


def list_ready_to_settle_all() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS referral_id, r.created_at, r.visited, r.visited_at,
                   r.settled, r.settled_at,
                   p.full_name AS patient_full_name, p.birth_date AS patient_birth_date,
                   d.id AS doctor_id, d.full_name AS doctor_full_name, d.tg_user_id AS doctor_tg_user_id
              FROM referrals r
              JOIN patients  p ON p.id = r.patient_id
              JOIN doctors   d ON d.id = r.doctor_id
             WHERE r.visited = 1 AND r.settled = 0
             ORDER BY r.created_at ASC, r.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_ready_to_settle_by_doctor(doctor_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS referral_id, r.created_at, r.visited, r.visited_at,
                   r.settled, r.settled_at,
                   p.full_name AS patient_full_name, p.birth_date AS patient_birth_date,
                   d.id AS doctor_id, d.full_name AS doctor_full_name, d.tg_user_id AS doctor_tg_user_id
              FROM referrals r
              JOIN patients  p ON p.id = r.patient_id
              JOIN doctors   d ON d.id = r.doctor_id
             WHERE r.visited = 1 AND r.settled = 0
               AND r.doctor_id = ?
             ORDER BY r.created_at ASC, r.id ASC
            """,
            (doctor_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def settle_referrals(referral_ids: list[int]) -> int:
    if not referral_ids:
        return 0
    placeholders = ",".join(["?"] * len(referral_ids))
    sql = f"""
        UPDATE referrals
           SET settled = 1,
               settled_at = datetime(CURRENT_TIMESTAMP, '+3 hours')
         WHERE id IN ({placeholders})
           AND settled = 0
    """
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(referral_ids))
        return cur.rowcount


def get_referrals_details(referral_ids: list[int]) -> list[dict]:
    if not referral_ids:
        return []
    placeholders = ",".join(["?"] * len(referral_ids))
    sql = f"""
    SELECT r.id           AS referral_id,
           r.created_at   AS created_at,
           r.settled      AS settled,
           r.settled_at   AS settled_at,
           r.visited      AS visited,
           r.visited_at   AS visited_at,
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


# --- Доп. утилиты для пациентов ---

def list_patients_with_ref_counts() -> list[dict]:
    sql = """
    SELECT p.id            AS patient_id,
           p.full_name     AS full_name,
           p.birth_date    AS birth_date,
           COUNT(r.id)     AS referrals_count
      FROM patients p
 LEFT JOIN referrals r ON r.patient_id = p.id
  GROUP BY p.id
  ORDER BY p.full_name COLLATE NOCASE ASC, p.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def delete_patients(patient_ids: list[int]) -> int:
    if not patient_ids:
        return 0
    placeholders = ",".join(["?"] * len(patient_ids))
    sql = f"DELETE FROM patients WHERE id IN ({placeholders})"
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(patient_ids))
        return cur.rowcount


def list_patients_by_doctor_distinct(doctor_id: int) -> list[dict]:
    sql = """
    SELECT DISTINCT p.id AS patient_id,
                    p.full_name AS full_name,
                    p.birth_date AS birth_date
      FROM referrals r
      JOIN patients  p ON p.id = r.patient_id
     WHERE r.doctor_id = ?
  ORDER BY p.full_name_lc ASC, p.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (doctor_id,)).fetchall()
        return [dict(r) for r in rows]


def search_patients_prefix(q: str) -> list[dict]:
    tokens = [t for t in (q or "").strip().split() if t]
    where, args = _build_token_like_sql_from_word_start("p.full_name_lc", tokens)
    sql = f"""
    SELECT p.id         AS patient_id,
           p.full_name  AS full_name,
           p.birth_date AS birth_date,
           SUM(CASE WHEN r.visited = 1 THEN 1 ELSE 0 END)  AS visits_count,
           SUM(CASE WHEN r.settled = 1 THEN 1 ELSE 0 END)  AS settled_count
      FROM patients p
 LEFT JOIN referrals r ON r.patient_id = p.id
     WHERE {where}
  GROUP BY p.id
  ORDER BY p.full_name COLLATE NOCASE ASC, p.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]


def list_referrals_for_patient(patient_id: int) -> list[dict]:
    sql = """
    SELECT r.id         AS referral_id,
           r.created_at AS created_at,
           r.visited    AS visited,
           r.visited_at AS visited_at,
           r.settled    AS settled,
           r.settled_at AS settled_at,
           d.full_name  AS doctor_full_name
      FROM referrals r
      JOIN doctors   d ON d.id = r.doctor_id
     WHERE r.patient_id = ?
  ORDER BY r.created_at ASC, r.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (patient_id,)).fetchall()
        return [dict(r) for r in rows]


def list_doctor_patients_visit_status(doctor_id: int) -> list[dict]:
    """
    Для врача: уникальные пациенты, признак визита и последняя дата визита.
    Сортировка: по последней дате направления (последние добавленные — внизу).
    """
    sql = """
    SELECT p.id          AS patient_id,
           p.full_name   AS full_name,
           p.birth_date  AS birth_date,
           MAX(CASE WHEN r.visited = 1 THEN 1 ELSE 0 END) AS visited_any,
           MAX(r.visited_at) AS last_visited_at,
           MAX(r.created_at) AS last_referral_at
      FROM referrals r
      JOIN patients  p ON p.id = r.patient_id
     WHERE r.doctor_id = ?
  GROUP BY p.id
  ORDER BY last_referral_at ASC, p.id ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (doctor_id,)).fetchall()
        return [dict(r) for r in rows]
