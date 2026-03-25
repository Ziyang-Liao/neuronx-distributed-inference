"""Add city column to MySQL user_data and populate with sample data."""
import sys, pymysql

# Parse args manually for Python Shell
args = {}
for i, a in enumerate(sys.argv):
    if a.startswith('--') and i + 1 < len(sys.argv):
        args[a[2:]] = sys.argv[i + 1]

conn = pymysql.connect(host=args['DB_HOST'], user=args['DB_USER'], password=args['DB_PASS'], database='etl_source')
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='etl_source' AND table_name='user_data' AND column_name='city'")
if cur.fetchone()[0] == 0:
    cur.execute("ALTER TABLE user_data ADD COLUMN city VARCHAR(50) AFTER address")
    print("Added city column")

cities = {1: 'Beijing', 2: 'Shanghai', 3: 'Guangzhou', 4: 'Shenzhen', 5: 'Hangzhou'}
for uid, city in cities.items():
    cur.execute("UPDATE user_data SET city=%s, updated_at=NOW() WHERE id=%s", (city, uid))

conn.commit()
cur.execute("SELECT id, username, city FROM user_data")
for row in cur.fetchall():
    print(row)
cur.close()
conn.close()
print("Done")
