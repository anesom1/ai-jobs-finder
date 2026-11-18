
import sqlite3

conn = sqlite3.connect("data/jobs.db")

columns = {
    "description": "TEXT",
    "employment_type": "TEXT",
    "weekly_hours": "REAL",
    "language": "TEXT",
    "seniority": "TEXT",
    "work_mode": "TEXT",
}

existing_columns = {
    row[1]
    for row in conn.execute("PRAGMA table_info(jobs)")
}

for column, data_type in columns.items():
    if column not in existing_columns:
        conn.execute(
            f"ALTER TABLE jobs ADD COLUMN {column} {data_type}"
        )

conn.commit()
conn.close()

print("Job details columns added successfully.")
