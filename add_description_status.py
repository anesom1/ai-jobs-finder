
import sqlite3

conn = sqlite3.connect("data/jobs.db")

columns = {
    row[1]
    for row in conn.execute("PRAGMA table_info(jobs)")
}

if "description_status" not in columns:
    conn.execute("""
        ALTER TABLE jobs
        ADD COLUMN description_status TEXT DEFAULT 'pending'
    """)

conn.commit()
conn.close()

print("description_status column added.")
