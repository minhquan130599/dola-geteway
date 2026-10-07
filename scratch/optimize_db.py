import sys
sys.path.insert(0, '.')
import sqlite3
import config
import os

print("Initial DB size:", os.path.getsize(config.DB_PATH) / 1024, "KB")
conn = sqlite3.connect(config.DB_PATH)
conn.execute("UPDATE tasks SET reference_images = '[]' WHERE status IN ('completed', 'failed') AND reference_images IS NOT NULL AND length(reference_images) > 1000")
conn.commit()
conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_account ON tasks(account)")
conn.commit()
conn.execute("VACUUM")
print("New DB size:", os.path.getsize(config.DB_PATH) / 1024, "KB")
