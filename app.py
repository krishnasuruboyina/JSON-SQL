from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
CORS(app)
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


def get_connection(host, database_name, username, password, port=5432):
    try:
        conn = psycopg2.connect(
            host=host,
            database=database_name,
            user=username,
            password=password,
            port=port
        )
        return conn
    except Exception as e:
        raise Exception(f"Database connection failed: {str(e)}")

# -----------------------------------
# UPLOAD JSON & RETURN SCHEMA
# -----------------------------------
@app.route('/upload', methods=['POST'])
def upload_json():
    try:
        file = request.files.get('file')
        if file is None:
            return jsonify({"error": "No file uploaded"}), 400

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
            datatype = "VARCHAR(255)"
            nullable = "NULL"
            max_length = 255

            if any(x in column_lower for x in ["guid", "rowident"]):
              datatype = "UUID"
            elif any(x in column_lower for x in ["id"]):
              datatype = "INTEGER"
            elif any(x in column_lower for x in ["phone", "mobile", "contact", "aadhaar", "pan", "pincode"]):
                max_length = max((len(str(v)
                                      ) for v in values), default=255)
                if max_length == 0: max_length = 255
                datatype = f"VARCHAR({max_length})"
            elif all(isinstance(v, bool) for v in values):
                datatype = "BOOLEAN"
            elif any(isinstance(v, float) for v in values):
                datatype = "NUMERIC(18,6)"
            elif all(isinstance(v, int) and not isinstance(v, bool) for v in values):
                max_num = max(values) if values else 0
                datatype = "BIGINT" if max_num > 2147483647 else "INTEGER"
            elif all(isinstance(v, str) for v in values):
                max_length = max((len(str(v)) for v in values), default=255)
                if max_length == 0: max_length = 255
                datatype = "TEXT" if max_length > 255 else f"VARCHAR({max_length})"

            table_structure.append({
                "column_name": key,
                "datatype": datatype,
                "nullable": nullable,
                "max_length": max_length
            })

        table_name = file.filename.split(".")[0].replace(" ", "_")

        return jsonify({
            "table_name": table_name,
            "table_structure": table_structure
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 400


# -----------------------------------
# CREATE TABLE
# -----------------------------------
@app.route('/create-table', methods=['POST'])
def create_table():
    try:
        data = request.json
        ddl_query = data.get("ddl_query")
        server_name = data.get("server_name")
        database_name = data.get("database_name")
        username = data.get("username")
        password = data.get("password")

        conn = get_connection(server_name, database_name, username, password)
        cursor = conn.cursor()
        cursor.execute(ddl_query)
        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"message": "Table created successfully"})

    except Exception as e:
        return jsonify({"error": str(e)}), 400


# -----------------------------------
# INSERT DATA
# -----------------------------------
@app.route('/insert-data', methods=['POST'])
def insert_data():
    try:
        file = request.files.get("file")
        if file is None:
            return jsonify({"error": "Please upload JSON file"}), 400

        table_name = request.form.get("table_name")
        if not table_name:
            return jsonify({"error": "Enter table name"}), 400

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

        conn = get_connection(server_name, database_name, username, password)
        cursor = conn.cursor()

        inserted_count = 0
        skipped_count = 0

        for row in unique_rows:
            columns = ", ".join([f'"{col}"' for col in row.keys()])
            placeholders = ", ".join(["%s"] * len(row))
            values = list(row.values())

            insert_query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"

            try:
                cursor.execute(insert_query, values)
                inserted_count += 1
            except Exception as e:
                print("insert Error: ", e)
                skipped_count += 1

        conn.commit()
        cursor.close()
        conn.close()

        message = f"{inserted_count} rows inserted"
        if skipped_count > 0:
            message += f", {skipped_count} skipped"

        return jsonify({"message": message})

    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    print(f"🚀 JSONSQL Backend running on http://localhost:{port}")
    app.run(debug=True, port=port)