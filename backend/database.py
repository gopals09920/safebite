import csv
import io
import os
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from dotenv import load_dotenv
from contextlib import contextmanager

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = BASE_DIR / "safebite.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return DATABASE_URL.startswith(("postgres://", "postgresql://"))


def _normalize_pg_url(url: str) -> str:
    return "postgresql://" + url[len("postgres://"):] if url.startswith("postgres://") else url


def _resolve_data_dir() -> Path:
    raw = os.getenv("DATA_DIR", "data").strip().strip('"').strip("'").replace("\xa0", " ")
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return p

    candidates = [
        BASE_DIR / raw.lstrip("./\\"),
        PROJECT_ROOT / raw.lstrip("./\\"),
        BASE_DIR / "data",
        PROJECT_ROOT / "data",
    ]
    for c in candidates:
        if c.exists():
            return c
    return BASE_DIR / "data"


DATA_DIR = _resolve_data_dir()


@contextmanager
def get_connection():
    if is_postgres():
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL ke liye 'psycopg[binary]' install karein: pip install \"psycopg[binary]\""
            ) from exc

        conn = psycopg.connect(_normalize_pg_url(DATABASE_URL), row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def _resolve_diseases_csv() -> Path:
    primary = DATA_DIR / "diseases.csv"
    fallback = DATA_DIR / "disease.csv"
    return primary if primary.exists() else fallback


def _clean_cell(val: str) -> str:
    return str(val).replace("\x00", "").strip()


def _to_int_or_none(val: str) -> int | None:
    s = _clean_cell(val)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _try_extract_zip_or_xlsx_rows(raw_bytes: bytes) -> tuple[list[str], list[list[str]]]:
    """Agar file ZIP/XLSX archive nikle toh andar se rows nikalta hai."""
    if not raw_bytes.startswith(b"PK"):
        return [], []

    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names = zf.namelist()

            # 1. Agar andar koi .csv ya .txt file ho
            for name in names:
                if name.lower().endswith((".csv", ".txt")):
                    inner_text = zf.read(name).decode("utf-8", errors="replace").replace("\x00", "")
                    rows = list(csv.reader(inner_text.splitlines()))
                    if rows:
                        header = [re.sub(r"\W+", "_", _clean_cell(h)).strip("_").lower() for h in rows[0]]
                        header = [h if h else f"col_{i}" for i, h in enumerate(header)]
                        data = [[_clean_cell(c) for c in r] for r in rows[1:] if any(_clean_cell(c) for c in r)]
                        if header and data:
                            return header, data

            # 2. Agar .xlsx structure (xl/sharedStrings.xml + xl/worksheets/sheet1.xml) ho
            if "xl/worksheets/sheet1.xml" in names:
                shared_strings: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in ss_root.iter():
                        if si.tag.endswith("}t") or si.tag == "t":
                            shared_strings.append(si.text or "")

                sheet_root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
                parsed_rows: list[list[str]] = []
                for row_el in sheet_root.iter():
                    if row_el.tag.endswith("}row") or row_el.tag == "row":
                        current_row: list[str] = []
                        for c_el in row_el:
                            if c_el.tag.endswith("}c") or c_el.tag == "c":
                                c_type = c_el.attrib.get("t", "")
                                val_text = ""
                                for child in c_el:
                                    if child.tag.endswith("}v") or child.tag == "v":
                                        raw_v = child.text or ""
                                        if c_type == "s" and raw_v.isdigit():
                                            idx = int(raw_v)
                                            val_text = shared_strings[idx] if idx < len(shared_strings) else raw_v
                                        else:
                                            val_text = raw_v
                                    elif child.tag.endswith("}is") or child.tag == "is":
                                        parts = [t.text or "" for t in child.iter() if t.tag.endswith("}t") or t.tag == "t"]
                                        val_text = "".join(parts)
                                current_row.append(_clean_cell(val_text))
                        if any(current_row):
                            parsed_rows.append(current_row)

                if parsed_rows:
                    header = [re.sub(r"\W+", "_", _clean_cell(h)).strip("_").lower() for h in parsed_rows[0]]
                    header = [h if h else f"col_{i}" for i, h in enumerate(header)]
                    data = parsed_rows[1:]
                    return header, data
    except Exception:
        return [], []

    return [], []


def _read_rows(csv_path: Path) -> tuple[list[str], list[list[str]]]:
    if not csv_path.exists():
        return [], []

    raw_bytes = csv_path.read_bytes()

    # Agar file ZIP/XLSX hai toh pehle wahan se padhein
    if raw_bytes.startswith(b"PK"):
        z_cols, z_rows = _try_extract_zip_or_xlsx_rows(raw_bytes)
        if z_cols and z_rows:
            return z_cols, z_rows

    cleaned_bytes = raw_bytes.replace(b"\x00", b"")

    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            text = cleaned_bytes.decode(enc)
            lines = text.splitlines()
            if not lines:
                return [], []

            reader = csv.reader(lines)
            rows = list(reader)
            if not rows:
                return [], []

            header = [re.sub(r"\W+", "_", _clean_cell(h)).strip("_").lower() for h in rows[0]]
            header = [h if h else f"col_{idx}" for idx, h in enumerate(header)]
            expected_len = len(header)

            cleaned = []
            for r in rows[1:]:
                cells = [_clean_cell(c) for c in r]
                if not cells or all(c == "" for c in cells):
                    continue
                if len(cells) > expected_len:
                    cells = cells[: expected_len - 1] + [",".join(cells[expected_len - 1 :])]
                elif len(cells) < expected_len:
                    cells = cells + [""] * (expected_len - len(cells))
                cleaned.append(cells)

            return header, cleaned
        except UnicodeDecodeError:
            continue

    return [], []


def _load_diseases(cursor: sqlite3.Cursor, csv_path: Path, interactions_csv: Path) -> None:
    if cursor.execute("SELECT COUNT(*) FROM diseases").fetchone()[0] > 0:
        return

    cols, rows = _read_rows(csv_path)
    col_map = {c: i for i, c in enumerate(cols)} if cols else {}
    name_key = next((k for k in ("disease_name", "name", "disease", "condition") if k in col_map), None)

    loaded_any = False
    if name_key is not None and rows:
        cat_key = next((k for k in ("category", "group", "type") if k in col_map), None)
        notes_key = next((k for k in ("notes", "note", "description", "desc") if k in col_map), None)
        id_key = next((k for k in ("disease_id", "id") if k in col_map), None)

        for idx, r in enumerate(rows, start=1):
            d_name = r[col_map[name_key]].strip()
            if not d_name:
                continue
            d_id = _to_int_or_none(r[col_map[id_key]]) if id_key else idx
            if d_id is None:
                d_id = idx
            d_cat = r[col_map[cat_key]].strip() if cat_key else None
            d_notes = r[col_map[notes_key]].strip() if notes_key else None

            cursor.execute(
                """
                INSERT OR IGNORE INTO diseases (disease_id, disease_name, category, notes)
                VALUES (?, ?, ?, ?)
                """,
                (d_id, d_name, d_cat, d_notes),
            )
            loaded_any = True

    # Fallback: agar diseases.csv corrupt/ZIP thi toh interactions.csv se unique diseases nikaalein
    if not loaded_any and interactions_csv.exists():
        i_cols, i_rows = _read_rows(interactions_csv)
        i_map = {c: i for i, c in enumerate(i_cols)}
        if "disease_id" in i_map:
            seen_ids: set[int] = set()
            for r in i_rows:
                d_id = _to_int_or_none(r[i_map["disease_id"]])
                if d_id is None or d_id in seen_ids:
                    continue
                seen_ids.add(d_id)
                d_name = r[i_map["disease_name"]].strip() if "disease_name" in i_map else ""
                if not d_name:
                    d_name = f"Condition {d_id}"
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO diseases (disease_id, disease_name, category, notes)
                    VALUES (?, ?, ?, ?)
                    """,
                    (d_id, d_name, "General", "Loaded from interactions data"),
                )


def _load_items(cursor: sqlite3.Cursor, csv_path: Path) -> None:
    if cursor.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0:
        return
    cols, rows = _read_rows(csv_path)
    if not cols or not rows:
        return

    col_map = {c: i for i, c in enumerate(cols)}
    en_key = next(
        (k for k in ("name_en", "name_english", "item_name", "name", "title") if k in col_map),
        None,
    )
    if en_key is None:
        return

    hi_key = next((k for k in ("name_hi", "name_hindi", "hindi_name", "hi") if k in col_map), None)
    cat_key = next((k for k in ("category", "group", "type") if k in col_map), None)
    id_key = next((k for k in ("item_id", "id") if k in col_map), None)

    for idx, r in enumerate(rows, start=1):
        name_en = r[col_map[en_key]].strip()
        if not name_en:
            continue
        i_id = _to_int_or_none(r[col_map[id_key]]) if id_key else idx
        if i_id is None:
            i_id = idx
        name_hi = r[col_map[hi_key]].strip() if hi_key else None
        cat = r[col_map[cat_key]].strip() if cat_key else None

        cursor.execute(
            """
            INSERT OR IGNORE INTO items (item_id, name_en, name_hi, category)
            VALUES (?, ?, ?, ?)
            """,
            (i_id, name_en, name_hi, cat),
        )


def _load_interactions(cursor: sqlite3.Cursor, csv_path: Path) -> None:
    if cursor.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] > 0:
        return
    cols, rows = _read_rows(csv_path)
    if not cols or not rows:
        return

    col_map = {c: i for i, c in enumerate(cols)}
    d_id_key = next((k for k in ("disease_id",) if k in col_map), None)
    i_id_key = next((k for k in ("item_id",) if k in col_map), None)
    if d_id_key is None or i_id_key is None:
        return

    int_id_key = next((k for k in ("interaction_id", "id") if k in col_map), None)
    d_name_key = next((k for k in ("disease_name", "disease") if k in col_map), None)
    i_name_key = next((k for k in ("item_name", "name_en", "name_english", "item") if k in col_map), None)
    risk_key = next((k for k in ("risk_level", "risk", "severity") if k in col_map), None)
    gap_key = next((k for k in ("gap_minutes", "gap_min", "gap", "timing_gap_minutes") if k in col_map), None)
    advice_key = next((k for k in ("advice", "guidance", "recommendation", "notes") if k in col_map), None)
    source_key = next((k for k in ("source", "reference", "evidence") if k in col_map), None)

    d_lookup = {
        int(r["disease_id"]): r["disease_name"]
        for r in cursor.execute("SELECT disease_id, disease_name FROM diseases").fetchall()
    }
    i_lookup = {
        int(r["item_id"]): r["name_en"]
        for r in cursor.execute("SELECT item_id, name_en FROM items").fetchall()
    }

    batch = []
    for idx, r in enumerate(rows, start=1):
        d_id = _to_int_or_none(r[col_map[d_id_key]])
        i_id = _to_int_or_none(r[col_map[i_id_key]])
        if d_id is None or i_id is None:
            continue

        int_id = _to_int_or_none(r[col_map[int_id_key]]) if int_id_key else idx
        if int_id is None:
            int_id = idx

        d_name = (r[col_map[d_name_key]].strip() if d_name_key else "") or d_lookup.get(d_id, f"Disease {d_id}")
        i_name = (r[col_map[i_name_key]].strip() if i_name_key else "") or i_lookup.get(i_id, f"Item {i_id}")
        risk_level = (r[col_map[risk_key]].strip() if risk_key else "") or "Moderate"
        gap_val = _to_int_or_none(r[col_map[gap_key]]) if gap_key else 0
        gap_minutes = gap_val if gap_val is not None else 0
        advice = (r[col_map[advice_key]].strip() if advice_key else "") or "Consult a healthcare professional for personalized timing guidance."
        source = r[col_map[source_key]].strip() if source_key else None

        batch.append((int_id, d_id, d_name, i_id, i_name, risk_level, gap_minutes, advice, source))

    if batch:
        cursor.executemany(
            """
            INSERT OR IGNORE INTO interactions
            (interaction_id, disease_id, disease_name, item_id, item_name, risk_level, gap_minutes, advice, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )


def init_db() -> None:
    diseases_csv = _resolve_diseases_csv()
    items_csv = DATA_DIR / "items-v3.csv"
    interactions_csv = DATA_DIR / "interactions.csv"

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                full_name TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS diary_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                disease_id INTEGER,
                item_id INTEGER,
                meal_note TEXT NOT NULL,
                intake_time TEXT NOT NULL,
                gap_minutes INTEGER DEFAULT 0,
                symptom TEXT DEFAULT '',
                severity TEXT DEFAULT 'None',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (disease_id) REFERENCES diseases(disease_id),
                FOREIGN KEY (item_id) REFERENCES items(item_id)
            )
            """
        )
        try:
            conn.execute("ALTER TABLE diary_logs ADD COLUMN user_id INTEGER")
        except Exception:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS diseases (
                disease_id INTEGER PRIMARY KEY,
                disease_name TEXT NOT NULL UNIQUE,
                category TEXT,
                notes TEXT
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS items (
                item_id INTEGER PRIMARY KEY,
                name_en TEXT NOT NULL,
                name_hi TEXT,
                category TEXT
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS interactions (
                interaction_id INTEGER PRIMARY KEY,
                disease_id INTEGER NOT NULL,
                disease_name TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                gap_minutes INTEGER NOT NULL DEFAULT 0,
                advice TEXT NOT NULL,
                source TEXT,
                FOREIGN KEY (disease_id) REFERENCES diseases(disease_id),
                FOREIGN KEY (item_id) REFERENCES items(item_id)
            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_interactions_pair
            ON interactions (disease_id, item_id);
        """)

        _load_diseases(cursor, diseases_csv, interactions_csv)
        _load_items(cursor, items_csv)
        _load_interactions(cursor, interactions_csv)

        cursor.execute("PRAGMA foreign_keys = ON;")
        conn.commit()


if __name__ == "__main__":
    init_db()
    with get_connection() as conn:
        d_cnt = conn.execute("SELECT COUNT(*) FROM diseases").fetchone()[0]
        i_cnt = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        r_cnt = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    print(f"DB ready -> diseases: {d_cnt}, items: {i_cnt}, interactions: {r_cnt}")