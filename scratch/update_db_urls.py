import sqlite3
import config

print("Loaded config.PUBLIC_BASE:", config.PUBLIC_BASE)

conn = sqlite3.connect("tasks.db")
cur = conn.cursor()
cur.execute(
    "UPDATE tasks SET video_url = replace(video_url, 'http://127.0.0.1:8000', ?) WHERE video_url LIKE '%127.0.0.1:8000%'",
    (config.PUBLIC_BASE,)
)
conn.commit()
print("Tasks updated:", cur.rowcount)

cur.execute("SELECT id, status, video_url FROM tasks WHERE video_url IS NOT NULL ORDER BY created_at DESC LIMIT 3")
for row in cur.fetchall():
    print("Row:", row)
conn.close()
