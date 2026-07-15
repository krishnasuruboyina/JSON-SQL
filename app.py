from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json
import re
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
CORS(app)

# When running as a desktop app, desktop.py sets this env var before importing
# this module so Flask can also serve the bundled React build. On Render (or
# any normal web deployment) this stays unset, so behavior is unchanged.
FRONTEND_BUILD_DIR = os.environ.get("FRONTEND_BUILD_DIR")

# -----------------------------------
# SUPPORTED DATABASES
# -----------------------------------
SUPPORTED_DB_TYPES = ["postgresql", "mysql", "mssql", "oracle"]

DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "mssql": 1433,
    "oracle": 1521,
}


@app.route("/")
def home():
    if FRONTEND_BUILD_DIR and os.path.exists(os.path.join(FRONTEND_BUILD_DIR, "index.html")):
        return send_from_directory(FRONTEND_BUILD_DIR, "index.html")
    return "JSONSQL Backend is Running!"


@app.route("/<path:path>")
def serve_frontend_asset(path):
    # Only active in desktop mode (FRONTEND_BUILD_DIR set). On Render this
    # falls through to Flask's normal 404 handling for unmatched routes,
    # same as before.
    if FRONTEND_BUILD_DIR:
        full_path = os.path.join(FRONTEND_BUILD_DIR, path)
        if os.path.isfile(full_path):
            return send_from_directory(FRONTEND_BUILD_DIR, path)
        index_path = os.path.join(FRONTEND_BUILD_DIR, "index.html")
        if os.path.exists(index_path):
            return send_from_directory(FRONTEND_BUILD_DIR, "index.html")
    return jsonify({"error": "Not found"}), 404


# -----------------------------------
# NESTED JSON VALIDATION
# -----------------------------------
def is_nested_json(data):
    for value in data.values():
        if isinstance(value, (dict, list)):
            return True
    return False


def normalize_db_type(db_type):
    db_type = (db_type or "postgresql").strip().lower()
    aliases = {
        "postgres": "postgresql",
        "pg": "postgresql",
        "sqlserver": "mssql",
        "sql_server": "mssql",
        "ms-sql": "mssql",
        "oracledb": "oracle",
    }
    db_type = aliases.get(db_type, db_type)
    if db_type not in SUPPORTED_DB_TYPES:
        raise Exception(
            f"Unsupported database type '{db_type}'. Supported types: {', '.join(SUPPORTED_DB_TYPES)}"
        )
    return db_type


# -----------------------------------
# DB CONNECTION (per engine)
# -----------------------------------
def get_connection(db_type, host, database_name, username, password, port=None):
    db_type = normalize_db_type(db_type)
    port = int(port) if port else DEFAULT_PORTS[db_type]

    try:
        if db_type == "postgresql":
            import psycopg2
            return psycopg2.connect(
                host=host, database=database_name,
                user=username, password=password, port=port,
                sslmode="prefer"  # uses SSL if the server supports it, falls back otherwise
            )

        elif db_type == "mysql":
            import pymysql
            return pymysql.connect(
                host=host, database=database_name,
                user=username, password=password, port=port
            )

        elif db_type == "mssql":
            import pytds

            if host and "\\" in host:
                # Named instance (e.g. .\SQLEXPRESS)
                return pytds.connect(
                    dsn=host,
                    database=database_name,
                    user=username,
                    password=password
                )
            else:
                # Server + Port (e.g. localhost:1433)
                return pytds.connect(
                    server=host,
                    port=int(port) if port else 1433,
                    database=database_name,
                    user=username,
                    password=password
                )

        elif db_type == "oracle":
            import oracledb
            dsn = f"{host}:{port}/{database_name}"
            return oracledb.connect(user=username, password=password, dsn=dsn)

    except Exception as e:
        raise Exception(f"Database connection failed: {str(e)}")


# -----------------------------------
# IDENTIFIER QUOTING (per engine)
# -----------------------------------
def quote_identifier(db_type, name):
    if db_type == "mysql":
        return f"`{name}`"
    if db_type == "mssql":
        return f"[{name}]"
    # postgresql & oracle use double quotes
    return f'"{name}"'


# -----------------------------------
# PARAMETER PLACEHOLDERS (per engine)
# -----------------------------------
def build_placeholders(db_type, n):
    if db_type == "oracle":
        return ", ".join([f":{i + 1}" for i in range(n)])
    # postgresql (psycopg2), mysql (pymysql), mssql (python-tds) all accept %s
    return ", ".join(["%s"] * n)


# -----------------------------------
# DATATYPE MAPPING (per engine)
# -----------------------------------
def map_datatype(db_type, category, length=255, max_num=0):
    if category == "uuid":
        return {
            "postgresql": "UUID",
            "mysql": "CHAR(36)",
            "mssql": "UNIQUEIDENTIFIER",
            "oracle": "VARCHAR2(36)",
        }[db_type]

    if category == "integer":
        is_big = max_num > 2147483647
        return {
            "postgresql": "BIGINT" if is_big else "INTEGER",
            "mysql": "BIGINT" if is_big else "INT",
            "mssql": "BIGINT" if is_big else "INT",
            "oracle": "NUMBER(19)" if is_big else "NUMBER(10)",
        }[db_type]

    if category == "boolean":
        return {
            "postgresql": "BOOLEAN",
            "mysql": "TINYINT(1)",
            "mssql": "BIT",
            "oracle": "NUMBER(1)",
        }[db_type]

    if category == "numeric":
        return {
            "postgresql": "NUMERIC(18,6)",
            "mysql": "DECIMAL(18,6)",
            "mssql": "DECIMAL(18,6)",
            "oracle": "NUMBER(18,6)",
        }[db_type]

    if category == "text":
        return {
            "postgresql": "TEXT",
            "mysql": "TEXT" if length <= 65535 else "LONGTEXT",
            "mssql": "NVARCHAR(MAX)",
            "oracle": "CLOB",
        }[db_type]

    # default -> varchar-like
    return {
        "postgresql": f"VARCHAR({length})",
        "mysql": f"VARCHAR({length})",
        "mssql": f"NVARCHAR({length})",
        "oracle": f"VARCHAR2({length})",
    }[db_type]


# -----------------------------------
# DYNAMIC COLUMN WIDENING (per engine)
# -----------------------------------
VARIABLE_CHAR_TYPES = {"character varying", "varchar", "nvarchar", "varchar2"}


def get_string_column_info(cursor, db_type, table_name):
    """Return {column_name: (data_type, char_length)} for the table's columns.
    char_length is None/-1 when the column has no fixed limit (e.g. TEXT/CLOB/MAX)."""
    info = {}
    try:
        if db_type == "postgresql":
            cursor.execute(
                "SELECT column_name, data_type, character_maximum_length "
                "FROM information_schema.columns WHERE table_name = %s",
                (table_name,)
            )
        elif db_type == "mysql":
            cursor.execute(
                "SELECT column_name, data_type, character_maximum_length "
                "FROM information_schema.columns WHERE table_name = %s AND table_schema = DATABASE()",
                (table_name,)
            )
        elif db_type == "mssql":
            cursor.execute(
                "SELECT column_name, data_type, character_maximum_length "
                "FROM information_schema.columns WHERE table_name = %s",
                (table_name,)
            )
        elif db_type == "oracle":
            cursor.execute(
                "SELECT column_name, data_type, char_length FROM user_tab_columns WHERE table_name = :1",
                (table_name,)
            )
        for row in cursor.fetchall():
            col_name, data_type, char_len = row[0], row[1], row[2]
            info[col_name] = (data_type, char_len)
    except Exception:
        # If introspection isn't possible (permissions, unsupported catalog, etc.),
        # widening is skipped for this run and normal insert/skip behavior applies.
        pass
    return info


def widen_column(cursor, db_type, table_name, column_name, new_length):
    quoted_table = quote_identifier(db_type, table_name)
    quoted_col = quote_identifier(db_type, column_name)
    if db_type == "postgresql":
        cursor.execute(f'ALTER TABLE {quoted_table} ALTER COLUMN {quoted_col} TYPE VARCHAR({new_length})')
    elif db_type == "mysql":
        cursor.execute(f'ALTER TABLE {quoted_table} MODIFY COLUMN {quoted_col} VARCHAR({new_length})')
    elif db_type == "mssql":
        cursor.execute(f'ALTER TABLE {quoted_table} ALTER COLUMN {quoted_col} NVARCHAR({new_length}) NULL')
    elif db_type == "oracle":
        cursor.execute(f'ALTER TABLE {quoted_table} MODIFY {quoted_col} VARCHAR2({new_length})')


def auto_widen_columns(cursor, conn, db_type, table_name, rows):
    """Compare the longest string value per column against the table's current column
    size, and widen any variable-length character column that's too small.
    Commits after each successful ALTER so one column's failure can't roll back
    another column that was already widened in the same batch."""
    column_info = get_string_column_info(cursor, db_type, table_name)
    if not column_info:
        return {"widened": [], "failed": []}

    needed_lengths = {}
    for row in rows:
        for col, val in row.items():
            if isinstance(val, str):
                needed_lengths[col] = max(needed_lengths.get(col, 0), len(val))

    widened = []
    failed = []

    for col, needed in needed_lengths.items():
        data_type, current_len = column_info.get(col, (None, None))
        if data_type is None or data_type.lower() not in VARIABLE_CHAR_TYPES:
            continue  # not a resizable variable-length text column (e.g. UUID, INTEGER, TEXT/CLOB)
        if current_len is None or current_len == -1:
            continue  # already unlimited (e.g. NVARCHAR(MAX))
        if needed > current_len:
            new_length = needed + 50  # small buffer to reduce repeated ALTERs on future inserts
            try:
                widen_column(cursor, db_type, table_name, col, new_length)
                conn.commit()
                widened.append(col)
            except Exception as e:
                print(f"Could not widen column '{col}': {e}")
                conn.rollback()
                failed.append(col)

    return {"widened": widened, "failed": failed}


def get_existing_columns(cursor, db_type, table_name):
    """Return the set of column names that currently exist on the table."""
    existing = set()
    try:
        if db_type == "postgresql":
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table_name,)
            )
        elif db_type == "mysql":
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s AND table_schema = DATABASE()",
                (table_name,)
            )
        elif db_type == "mssql":
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table_name,)
            )
        elif db_type == "oracle":
            cursor.execute(
                "SELECT column_name FROM user_tab_columns WHERE table_name = :1",
                (table_name,)
            )
        for row in cursor.fetchall():
            existing.add(row[0])
    except Exception:
        pass
    return existing


def add_column(cursor, db_type, table_name, column_name, datatype):
    quoted_table = quote_identifier(db_type, table_name)
    quoted_col = quote_identifier(db_type, column_name)
    if db_type == "postgresql":
        cursor.execute(f'ALTER TABLE {quoted_table} ADD COLUMN {quoted_col} {datatype}')
    elif db_type == "mysql":
        cursor.execute(f'ALTER TABLE {quoted_table} ADD COLUMN {quoted_col} {datatype}')
    elif db_type == "mssql":
        cursor.execute(f'ALTER TABLE {quoted_table} ADD {quoted_col} {datatype}')
    elif db_type == "oracle":
        cursor.execute(f'ALTER TABLE {quoted_table} ADD ({quoted_col} {datatype})')


# -----------------------------------
# COLUMN NAME NORMALIZATION (matches emp_name / empName / Emp Name as the same column)
# -----------------------------------
def normalize_key(name):
    """Strip case, underscores, spaces, and hyphens so columns referring to the
    same field under different naming conventions compare equal
    (e.g. 'emp_name', 'empName', 'Emp Name' all normalize to 'empname')."""
    return re.sub(r'[^a-z0-9]', '', str(name).lower())


def derive_table_prefixes(table_name):
    """Normalized singular/plural guesses for the table name, used to strip a
    table-name prefix off columns like 'studentId' so it can match an existing
    generic column like 'id' on the 'students' table."""
    if not table_name:
        return set()
    base = normalize_key(table_name)
    prefixes = {base}
    if base.endswith('s') and len(base) > 1:
        prefixes.add(base[:-1])   # students -> student
    else:
        prefixes.add(base + 's')  # student -> students
    return {p for p in prefixes if p}


def build_normalized_column_map(existing_columns):
    """Map normalized_name -> actual existing column name (first match wins)."""
    norm_map = {}
    for col in existing_columns:
        norm = normalize_key(col)
        if norm not in norm_map:
            norm_map[norm] = col
    return norm_map


def resolve_existing_column(key, norm_map, table_prefixes):
    """Find the existing column a given incoming key refers to, trying:
    1. A direct normalized match (emp_name == empName)
    2. Stripping a table-name prefix from the key (studentId -> id)
    3. Adding a table-name prefix to the key (id -> studentId, if that's what exists)
    Returns the existing column name, or the original key if nothing matches."""
    norm_key = normalize_key(key)

    if norm_key in norm_map:
        return norm_map[norm_key]

    for prefix in table_prefixes:
        if norm_key.startswith(prefix) and len(norm_key) > len(prefix):
            stripped = norm_key[len(prefix):]
            if stripped in norm_map:
                return norm_map[stripped]

    for prefix in table_prefixes:
        prefixed = prefix + norm_key
        if prefixed in norm_map:
            return norm_map[prefixed]

    return key  # no match found -- this is a genuinely new column


def remap_rows_to_existing_columns(rows, existing_columns, table_name=None):
    """Rename incoming JSON keys to match an existing DB column when they refer
    to the same logical field under a different naming convention or a
    table-prefixed id pattern (e.g. 'empName' -> 'emp_name', 'studentId' -> 'id'
    on the 'students' table). Keys with no match are left as-is -- genuinely new fields."""
    norm_map = build_normalized_column_map(existing_columns)
    table_prefixes = derive_table_prefixes(table_name)

    remapped_rows = []
    for row in rows:
        new_row = {}
        for key, val in row.items():
            target_key = resolve_existing_column(key, norm_map, table_prefixes)
            # If two differently-named incoming keys map to the same existing
            # column, keep whichever value is non-null.
            if target_key in new_row and new_row[target_key] is not None:
                continue
            new_row[target_key] = val
        remapped_rows.append(new_row)
    return remapped_rows


def auto_add_missing_columns(cursor, conn, db_type, table_name, rows):
    """Detect JSON keys that don't exist as columns on the table yet, infer a
    datatype for each from the incoming data, and add them via ALTER TABLE.
    Commits after each successful ADD COLUMN so one column's failure can't
    roll back another column that was already added in the same batch."""
    existing_columns = get_existing_columns(cursor, db_type, table_name)
    if not existing_columns:
        return {"added": [], "failed": []}  # table introspection failed or table has no columns; skip safely

    all_keys = set()
    for row in rows:
        all_keys.update(row.keys())

    # resolve_existing_column() catches both naming-convention differences
    # (emp_name vs empName) AND table-prefixed id patterns (studentId -> id).
    # It returns the key unchanged only when no existing column matches.
    norm_map = build_normalized_column_map(existing_columns)
    table_prefixes = derive_table_prefixes(table_name)
    missing_keys = []
    seen_norms = set()
    for k in all_keys:
        resolved = resolve_existing_column(k, norm_map, table_prefixes)
        norm = normalize_key(k)
        if resolved == k and norm not in seen_norms:
            missing_keys.append(k)
            seen_norms.add(norm)

    added = []
    failed = []

    for key in missing_keys:
        values = [row.get(key) for row in rows if row.get(key) is not None]
        category, max_length, max_num = infer_column_category(key, values)
        datatype = map_datatype(db_type, category, length=max_length, max_num=max_num)
        try:
            add_column(cursor, db_type, table_name, key, datatype)
            conn.commit()
            added.append(key)
        except Exception as e:
            print(f"Could not add column '{key}': {e}")
            conn.rollback()
            failed.append(key)

    return {"added": added, "failed": failed}


# -----------------------------------
# COLUMN CATEGORY INFERENCE (shared by /upload and auto-add-columns)
# -----------------------------------
def infer_column_category(key, values):
    """Given a column name and its non-null values, return (category, max_length, max_num)."""
    column_lower = key.lower()
    category = "varchar"
    max_length = 255
    max_num = 0

    if any(x in column_lower for x in ["guid", "rowident"]):
        category = "uuid"
    elif any(x in column_lower for x in ["id"]):
        category = "integer"
        max_num = max([v for v in values if isinstance(v, int)], default=0)
    elif any(x in column_lower for x in ["phone", "mobile", "contact", "aadhaar", "pan", "pincode"]):
        max_length = max((len(str(v)) for v in values), default=255)
        if max_length == 0:
            max_length = 255
        category = "varchar"
    elif all(isinstance(v, bool) for v in values):
        category = "boolean"
    elif any(isinstance(v, float) for v in values):
        category = "numeric"
    elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        category = "integer"
        max_num = max(values) if values else 0
    elif all(isinstance(v, str) for v in values):
        max_length = max((len(str(v)) for v in values), default=255)
        if max_length == 0:
            max_length = 255
        category = "text" if max_length > 255 else "varchar"

    return category, max_length, max_num


# -----------------------------------
# UPLOAD JSON & RETURN SCHEMA
# -----------------------------------
@app.route('/upload', methods=['POST'])
def upload_json():
    try:
        file = request.files.get('file')
        if file is None:
            return jsonify({"error": "No file uploaded"}), 400

        db_type = normalize_db_type(request.form.get('db_type', 'postgresql'))

        data = json.load(file)

        if not isinstance(data, list):
            data = [data]

        table_structure = []
        all_keys = set()

        for row in data:
            all_keys.update(row.keys())

        for key in all_keys:
            values = [row.get(key) for row in data if row.get(key) is not None]

            category, max_length, max_num = infer_column_category(key, values)
            nullable = "NULL"
            datatype = map_datatype(db_type, category, length=max_length, max_num=max_num)

            table_structure.append({
                "column_name": key,
                "datatype": datatype,
                "nullable": nullable,
                "max_length": max_length
            })

        table_name = file.filename.split(".")[0].replace(" ", "_")

        return jsonify({
            "table_name": table_name,
            "table_structure": table_structure,
            "db_type": db_type
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 400


# -----------------------------------
# CREATE TABLE
# -----------------------------------
@app.route('/create-table', methods=['POST'])
def create_table():
    conn = None
    cursor = None
    try:
        data = request.json
        ddl_query = data.get("ddl_query")
        db_type = normalize_db_type(data.get("db_type", "postgresql"))
        server_name = data.get("server_name")
        database_name = data.get("database_name")
        username = data.get("username")
        password = data.get("password")
        port = data.get("port")

        conn = get_connection(db_type, server_name, database_name, username, password, port)
        cursor = conn.cursor()
        cursor.execute(ddl_query)
        conn.commit()

        return jsonify({"message": "Table created successfully"})

    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# -----------------------------------
# INSERT DATA
# -----------------------------------
@app.route('/insert-data', methods=['POST'])
def insert_data():
    conn = None
    cursor = None
    try:
        file = request.files.get("file")
        if file is None:
            return jsonify({"error": "Please upload JSON file"}), 400

        table_name = request.form.get("table_name")
        if not table_name:
            return jsonify({"error": "Enter table name"}), 400

        db_type = normalize_db_type(request.form.get("db_type", "postgresql"))

        json_data = json.load(file)
        if not isinstance(json_data, list):
            json_data = [json_data]

        for row in json_data:
            if is_nested_json(row):
                return jsonify({"error": "Nested JSON files are not allowed"}), 400

        server_name = request.form.get("server_name")
        database_name = request.form.get("database_name")
        username = request.form.get("username")
        password = request.form.get("password")
        port = request.form.get("port")

        conn = get_connection(db_type, server_name, database_name, username, password, port)
        cursor = conn.cursor()

        quoted_table = quote_identifier(db_type, table_name)

        # Rename incoming keys to match existing columns under a different naming
        # convention (e.g. 'empName' -> 'emp_name') BEFORE dedup/insert, so data
        # lands in the existing column instead of spawning a near-duplicate one.
        existing_columns_before = get_existing_columns(cursor, db_type, table_name)
        if existing_columns_before:
            json_data = remap_rows_to_existing_columns(json_data, existing_columns_before, table_name)

        # Remove duplicates
        unique_rows = []
        seen = set()
        for row in json_data:
            row_tuple = tuple(sorted(row.items()))
            if row_tuple not in seen:
                seen.add(row_tuple)
                unique_rows.append(row)

        # Add any columns present in the JSON but missing from the table,
        # instead of failing/skipping rows that have new keys.
        add_result = {"added": [], "failed": []}
        try:
            add_result = auto_add_missing_columns(cursor, conn, db_type, table_name, unique_rows)
        except Exception as add_col_error:
            print("Add column step failed:", add_col_error)
            conn.rollback()

        # Widen any text columns that are too small for the incoming data,
        # instead of silently skipping rows that don't fit.
        widen_result = {"widened": [], "failed": []}
        try:
            widen_result = auto_widen_columns(cursor, conn, db_type, table_name, unique_rows)
        except Exception as widen_error:
            print("Column widen step failed:", widen_error)
            conn.rollback()

        inserted_count = 0
        skipped_count = 0
        first_skip_reason = None

        for row in unique_rows:
            columns = ", ".join([quote_identifier(db_type, col) for col in row.keys()])
            placeholders = build_placeholders(db_type, len(row))
            values = list(row.values())

            insert_query = f"INSERT INTO {quoted_table} ({columns}) VALUES ({placeholders})"

            try:
                cursor.execute(insert_query, values)
                conn.commit()
                inserted_count += 1
            except Exception as e:
                print("insert Error: ", e)
                conn.rollback()
                if first_skip_reason is None:
                    first_skip_reason = str(e)
                skipped_count += 1

        message = f"{inserted_count} rows inserted"
        if skipped_count > 0:
            message += f", {skipped_count} skipped"
            if first_skip_reason:
                message += f" (e.g. {first_skip_reason})"

        if add_result["added"]:
            message += f". Added columns: {', '.join(add_result['added'])}"
        if add_result["failed"]:
            message += f". Could not add columns: {', '.join(add_result['failed'])}"
        if widen_result["widened"]:
            message += f". Widened columns: {', '.join(widen_result['widened'])}"

        return jsonify({"message": message})

    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    print(f"🚀 JSONSQL Backend running on http://localhost:{port}")
    app.run(debug=True, port=port)
