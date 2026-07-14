from flask import Flask, request, jsonify
from flask_cors import CORS
import json
from dotenv import load_dotenv
import os

load_dotenv()


app = Flask(__name__)
CORS(app)

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
    return "JSONSQL Backend is Running!"


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
                user=username, password=password, port=port
            )

        elif db_type == "mysql":
            import pymysql
            return pymysql.connect(
                host=host, database=database_name,
                user=username, password=password, port=port
            )

        elif db_type == "mssql":
            import pytds
            return pytds.connect(
                dsn=host, database=database_name,
                user=username, password=password, port=port
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

            column_lower = key.lower()
            category = "varchar"
            nullable = "NULL"
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

        # Remove duplicates
        unique_rows = []
        seen = set()
        for row in json_data:
            row_tuple = tuple(sorted(row.items()))
            if row_tuple not in seen:
                seen.add(row_tuple)
                unique_rows.append(row)

        server_name = request.form.get("server_name")
        database_name = request.form.get("database_name")
        username = request.form.get("username")
        password = request.form.get("password")
        port = request.form.get("port")

        conn = get_connection(db_type, server_name, database_name, username, password, port)
        cursor = conn.cursor()

        quoted_table = quote_identifier(db_type, table_name)

        inserted_count = 0
        skipped_count = 0

        for row in unique_rows:
            columns = ", ".join([quote_identifier(db_type, col) for col in row.keys()])
            placeholders = build_placeholders(db_type, len(row))
            values = list(row.values())

            insert_query = f"INSERT INTO {quoted_table} ({columns}) VALUES ({placeholders})"

            try:
                cursor.execute(insert_query, values)
                inserted_count += 1
            except Exception as e:
                print("insert Error: ", e)
                skipped_count += 1

        conn.commit()

        message = f"{inserted_count} rows inserted"
        if skipped_count > 0:
            message += f", {skipped_count} skipped"

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
