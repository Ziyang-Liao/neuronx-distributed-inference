"""Glue Python Shell: Initialize MySQL with sample data"""
import sys
import pymysql

conn = pymysql.connect(
    host='etl-mysql.cuf6dqeperny.us-east-1.rds.amazonaws.com',
    user='admin', password='admin123', database='etl_source', port=3306
)
cur = conn.cursor()

cur.execute("""CREATE TABLE IF NOT EXISTS user_data (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(256) NOT NULL,
    email VARCHAR(256) NOT NULL,
    phone VARCHAR(64),
    address VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_updated_at (updated_at)
)""")

cur.execute("SELECT COUNT(*) FROM user_data")
if cur.fetchone()[0] == 0:
    data = [
        ('john_doe','john@example.com','555-0101','123 Main St, Springfield, IL'),
        ('jane_smith','jane@example.com','555-0102','456 Oak Ave, Portland, OR'),
        ('bob_wilson','bob@example.com','555-0103','789 Pine Rd, Austin, TX'),
        ('alice_chen','alice@example.com','555-0104','321 Elm St, Seattle, WA'),
        ('charlie_brown','charlie@example.com','555-0105','654 Maple Dr, Denver, CO'),
        ('john_doe','john_v2@example.com','555-0101','123 Main St, Springfield, IL'),
        ('diana_prince','diana@example.com','555-0106','100 Hero Blvd, Washington, DC'),
        ('eve_taylor','eve@example.com','555-0107','200 Tech Park, San Jose, CA'),
        ('frank_miller','frank@example.com','555-0108','300 Art St, New York, NY'),
        ('grace_hopper','grace@example.com','555-0109','400 Navy Yard, Arlington, VA'),
    ]
    cur.executemany(
        "INSERT INTO user_data (username,email,phone,address) VALUES (%s,%s,%s,%s)", data
    )
    conn.commit()
    print(f"Inserted {len(data)} rows (including 1 duplicate username for dedup test)")
else:
    print("Data already exists, skipping insert")

cur.execute("SELECT COUNT(*) FROM user_data")
print(f"Total rows in MySQL: {cur.fetchone()[0]}")
cur.close()
conn.close()
