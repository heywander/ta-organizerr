import sqlite3
conn = sqlite3.connect('/mnt/user/tubearchives/TASTY/sqlite/ta.db')
rows = conn.execute("SELECT video_id, title, is_live, symlink FROM videos WHERE channel LIKE '%Javal%' LIMIT 5").fetchall()
for r in rows:
    print(r)
