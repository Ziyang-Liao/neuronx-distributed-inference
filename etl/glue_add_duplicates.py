"""Insert duplicate + updated records into MySQL for dedup testing"""
import pymysql

conn = pymysql.connect(
    host='etl-mysql.cuf6dqeperny.us-east-1.rds.amazonaws.com',
    port=3306, user='admin', password='admin123', database='etl_source'
)
cur = conn.cursor()

# Duplicate existing records (same id via explicit insert)
cur.execute("SELECT COUNT(*) FROM user_data")
print(f"Before: {cur.fetchone()[0]} rows")

data = [
    # Duplicates of existing ids with UPDATED info (should overwrite)
    (1, 'john_doe_v3', 'john_new@example.com', '555-9999', '999 Updated St, Chicago, IL'),
    (2, 'jane_smith_v2', 'jane_new@example.com', '555-8888', '888 Changed Ave, Miami, FL'),
    (3, 'bob_wilson_v2', 'bob_new@example.com', '555-7777', '777 New Rd, Dallas, TX'),
    # More duplicates of id=1 (triple duplicate)
    (1, 'john_doe_v4', 'john_v4@example.com', '555-6666', '666 Triple St, Boston, MA'),
    (1, 'john_doe_v5', 'john_v5@example.com', '555-5555', '555 Quad Ave, Phoenix, AZ'),
    # New records
    (11, 'new_user_1', 'new1@example.com', '555-1111', '111 Fresh St, Atlanta, GA'),
    (12, 'new_user_2', 'new2@example.com', '555-2222', '222 Brand Ave, Houston, TX'),
]

cur.executemany(
    "INSERT INTO user_data (id, username, email, phone, address) VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE username=VALUES(username), email=VALUES(email), phone=VALUES(phone), address=VALUES(address)",
    data
)
conn.commit()

cur.execute("SELECT COUNT(*) FROM user_data")
print(f"After: {cur.fetchone()[0]} rows")
cur.execute("SELECT id, username, email FROM user_data ORDER BY id")
for row in cur.fetchall():
    print(f"  id={row[0]} username={row[1]} email={row[2]}")

cur.close()
conn.close()
