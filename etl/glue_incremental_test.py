"""Insert new records + update existing records for incremental test"""
import pymysql

conn = pymysql.connect(host='<rds-endpoint>',
                       port=3306, user='admin', password='<your-password>', database='etl_source')
cur = conn.cursor()

print("=== BEFORE ===")
cur.execute("SELECT id, username, email, phone, address, updated_at FROM user_data ORDER BY id")
for r in cur.fetchall():
    print(f"  id={r[0]} user={r[1]} email={r[2]} updated={r[5]}")

# UPDATE existing: id=1 change address, id=5 change phone
cur.execute("UPDATE user_data SET address='100 Lake Shore Dr, Chicago, IL', phone='555-0000' WHERE id=1")
cur.execute("UPDATE user_data SET phone='555-9876', address='999 Mountain Rd, Boulder, CO' WHERE id=5")

# INSERT new records: id=13, 14, 15
cur.executemany("INSERT INTO user_data (username, email, phone, address) VALUES (%s,%s,%s,%s)", [
    ('henry_ford', 'henry@example.com', '555-1300', '500 Motor Ave, Detroit, MI'),
    ('iris_zhang', 'iris@example.com', '555-1400', '600 Tech Blvd, San Francisco, CA'),
    ('jack_ryan', 'jack@example.com', '555-1500', '700 Intel St, Langley, VA'),
])
conn.commit()

print("\n=== AFTER ===")
cur.execute("SELECT id, username, email, phone, address, updated_at FROM user_data ORDER BY id")
for r in cur.fetchall():
    print(f"  id={r[0]} user={r[1]:20s} email={r[2]:25s} phone={r[3]} updated={r[5]}")

cur.close()
conn.close()
