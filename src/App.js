import React, { useState } from "react";
import "./App.css";

function App() {
  const [file, setFile] = useState(null);
  const [result, setResult] = useState(null);
  const [ddlQuery, setDdlQuery] = useState("");
  const [loading, setLoading] = useState(false);

  // Database Connection States
  const [serverName, setServerName] = useState("");
  const [databaseName, setDatabaseName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  // Insert States
  const [insertTableName, setInsertTableName] = useState("");
  const [insertFile, setInsertFile] = useState(null);

  // Upload JSON
  const uploadFile = async () => {
    if (!file) {
      alert("Please select a JSON file");
      return;
    }

    const formData = new FormData();
    formData.append("file", file);

    try {
      setLoading(true);
      const response = await fetch("https://jsonsql-backend.onrender.com/upload", {
  method: "POST",
  body: formData,
});

   console.log("Status:", response.status);

const text = await response.text();
console.log("Response:", text);

const data = JSON.parse(text);
setLoading(false);

if (data.error) {
  alert(data.error);
  return;
}

setResult(data);
alert("JSON uploaded successfully! Schema generated.");
    } catch (error) {
      setLoading(false);
      alert("Error uploading JSON");
      console.error(error);
    }
  };

  // Generate DDL Query
  const generateDDL = () => {
    if (!result || !result.table_structure) {
      alert("No schema found. Upload a JSON file first.");
      return;
    }

    let query = `CREATE TABLE "${result.table_name}" (\n`;

    query += result.table_structure
      .map((col) => {
        const columnName = col.column_name?.trim() || "Column";
        const datatype = col.datatype || "VARCHAR(255)";
        return `  "${columnName}" ${datatype} NULL`;
      })
      .join(",\n");

    query += "\n);";

    setDdlQuery(query);
    alert("DDL Generated! Check below.");
  };

  // Create Table in Database
  const createTableInDB = async () => {
    if (!serverName || !databaseName || !ddlQuery) {
      alert("Please fill Server Name, Database Name and generate DDL first");
      return;
    }

    try {
      setLoading(true);
      const response = await fetch("https://jsonsql-backend.onrender.com/create-table", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server_name: serverName,
          database_name: databaseName,
          ddl_query: ddlQuery,
          username: username,
          password: password,
        }),
      });

      const data = await response.json();
      setLoading(false);
      alert(data.message || data.error || "Operation completed");
    } catch (error) {
      setLoading(false);
      alert("Error creating table");
      console.error(error);
    }
  };

  // Insert Data
  const insertData = async () => {
    if (!insertFile || !insertTableName) {
      alert("Please upload file and enter table name");
      return;
    }

    if (!serverName || !databaseName) {
      alert("Please fill in Host and Database Name in the Database Connection section first");
      return;
    }

    const formData = new FormData();
    formData.append("file", insertFile);
    formData.append("table_name", insertTableName);
    formData.append("server_name", serverName);
    formData.append("database_name", databaseName);
    formData.append("username", username);
    formData.append("password", password);

    try {
      setLoading(true);
      const response = await fetch("https://jsonsql-backend.onrender.com/insert-data", {
        method: "POST",
        body: formData,
      });

      const data = await response.json();
      setLoading(false);
      let msg = data.message || data.error || "Data inserted successfully";
      if (data.errors && data.errors.length > 0) {
        msg += "\n\nDetails:\n" + data.errors.join("\n");
      }
      alert(msg);
    } catch (error) {
      setLoading(false);
      alert("Error inserting data");
      console.error(error);
    }
  };

  return (
    <div className="container">
      <h1>JSONSQL - JSON to POSTGRESQL</h1>

      {loading && <h3>Loading...</h3>}

      {/* Upload Section */}
      <div className="section">
        <h2>1. Upload JSON File</h2>
        <input type="file" accept=".json" onChange={(e) => setFile(e.target.files[0])} />
        <br /><br />
        <button onClick={uploadFile}>Upload & Analyze JSON</button>
      </div>

      {/* Schema Editor */}
      {result && (
        <div className="section">
          <h2>2. Table Schema</h2>
          <p>Table Name: <strong>{result.table_name}</strong></p>

          <button onClick={generateDDL}>Generate CREATE TABLE Query</button>
        </div>
      )}

      {/* DDL Output */}
      {ddlQuery && (
        <div className="section">
          <h2>3. Generated DDL Query</h2>
          <pre style={{ background: "#f4f4f4", padding: "15px", overflowX: "auto" }}>
            {ddlQuery}
          </pre>
          <button onClick={() => navigator.clipboard.writeText(ddlQuery)}>Copy Query</button>
        </div>
      )}

      {/* Database Connection */}
      <div className="section">
        <h2>4. Database Connection</h2>
        <br /><br />

        <input type="text" placeholder="Host (e.g. localhost or db.xxxxx.supabase.co)" value={serverName} onChange={(e) => setServerName(e.target.value)} />
        <br /><br />

        <input type="text" placeholder="Database Name" value={databaseName} onChange={(e) => setDatabaseName(e.target.value)} />
        <br /><br />

          <>
            <input type="text" placeholder="Username" value={username} onChange={(e) => setUsername(e.target.value)} />
            <br /><br />
            <input type="password" placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </>
        <br />

        <button onClick={createTableInDB}>Create Table in Database</button>
      </div>

      {/* Insert Data */}
      <div className="section">
        <h2>5. Insert Data</h2>
        <input type="text" placeholder="Table Name" value={insertTableName} onChange={(e) => setInsertTableName(e.target.value)} />
        <br /><br />

        <input type="file" accept=".json" onChange={(e) => setInsertFile(e.target.files[0])} />
        <br /><br />

        <button onClick={insertData}>Insert JSON Data</button>
      </div>
    </div>
  );
}

export default App;
